"""
用 LLM 复核规则定不准的两类新闻（批量分类，成本很低）：
  ① news_filtered_v2.json 里的 market 桶——里面混了"不在 universe 的公司财报"；
  ② news_dropped_v2.json 里的 off_topic 桶——规则未命中，但可能漏掉相关新闻。

只让模型回答四个值之一：sector / macro / market / off。每批 20 条，约 17 次调用。

输入：_yf_data/news_filtered_v2.json、_yf_data/news_dropped_v2.json
产出：_yf_data/scope_llm_v2.json          标题 -> LLM 判定
      _yf_data/news_filtered_v2_llm.json  复核后的最终语料

运行（key 走环境变量）：
  set OPENAI_API_KEY=sk-...
  python news_scope_llm.py
"""
import argparse
import collections
import json
import os
import re
import time

import openai

OUT_DIR = "_yf_data"
IN_FILTERED = os.path.join(OUT_DIR, "news_filtered_v2.json")
IN_DROPPED = os.path.join(OUT_DIR, "news_dropped_v2.json")
OUT_SCOPES = os.path.join(OUT_DIR, "scope_llm_v2.json")
OUT_FILTERED = os.path.join(OUT_DIR, "news_filtered_v2_llm.json")

parser = argparse.ArgumentParser()
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--batch", type=int, default=20)
args, _ = parser.parse_known_args()
if not args.api_key:
    raise SystemExit("need --api_key or OPENAI_API_KEY")
openai.api_key = args.api_key
openai.api_base = args.api_base

RULES = """你是金融新闻筛选员。下面是若干条带编号的英文新闻标题（附媒体名）。
请判断每条属于哪一类，只输出 JSON，不要解释。

分类定义：
- sector：与半导体、人工智能、科技、中国科技/互联网、黄金、原油、债券、美元等板块或产品线有关
- macro：宏观、政策、地缘（利率、通胀、关税、制裁、选举、战争、央行）
- market：大盘层面（指数点位、市场整体方向、资金流、市场评论或预测），但不是针对某家具体公司
- off：与以上都无关；或只是某家具体公司的个股消息（财报、评级、股价异动）而该公司不属于上述板块

注意：
1. 讲某家公司财报或股价的新闻，若该公司与上述板块产品线无关，判 off；
2. 讲整个市场、指数、资金流、市场情绪的，判 market；
3. 一条新闻只能有一个档位。

输入：
{lines}

只输出 JSON，例如：{"1": "sector", "2": "off"}"""


def parse_json(s):
    for f in (lambda x: json.loads(x),
              lambda x: json.loads(re.search(r"```(?:json)?\s*(.*?)\s*```", x, re.S).group(1)),
              lambda x: json.loads(re.search(r"\{.*\}", x, re.S).group(0))):
        try:
            return f(s)
        except Exception:
            pass
    return None


def classify_batch(lines):
    """lines: [(idx, title, publisher)] -> {idx: scope}"""
    body = "\n".join(f"{i}. {t}  ——{p}" for i, t, p in lines)
    prompt = RULES.replace("{lines}", body)
    for attempt in range(4):
        try:
            r = openai.ChatCompletion.create(
                model=args.model, messages=[{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=1500, thinking={"type": "disabled"})
            out = parse_json(r.choices[0].message.content)
            if out:
                return {int(k): str(v).strip().lower() for k, v in out.items() if str(k).isdigit()}
        except Exception as e:
            print(f"   [retry {attempt+1}] {type(e).__name__}: {str(e)[:70]}")
            time.sleep(2 * (attempt + 1))
    return {}


def main():
    with open(IN_FILTERED, encoding="utf-8") as f:
        filtered = json.load(f)
    with open(IN_DROPPED, encoding="utf-8") as f:
        dropped = json.load(f)

    market_items = [x for x in filtered["items"] if x["scope"] == "market"]
    off_items = list(dropped.get("off_topic") or [])
    candidates = market_items + off_items
    print(f"[INFO] 待复核：market {len(market_items)} 条 + 未命中 {len(off_items)} 条 = {len(candidates)} 条")

    decided = {}
    batches = (len(candidates) + args.batch - 1) // args.batch
    for b, start in enumerate(range(0, len(candidates), args.batch), 1):
        chunk = candidates[start:start + args.batch]
        lines = [(i + 1, c["title"], c.get("publisher", "")) for i, c in enumerate(chunk)]
        res = classify_batch(lines)
        for i, c in enumerate(chunk):
            decided[c["title"].strip()] = res.get(i + 1, "keep_rule")
        print(f"   批次 {b}/{batches}：{len(res)}/{len(chunk)} 条有结果")
        time.sleep(0.5)

    with open(OUT_SCOPES, "w", encoding="utf-8") as f:
        json.dump(decided, f, ensure_ascii=False, indent=2, sort_keys=True)

    # ---- 合并：market 桶按复核结果改写；未命中桶里被判为 sector/macro/market 的收回来 ----
    final, changed, restored, dropped_off = [], 0, 0, 0
    for x in filtered["items"]:
        if x["scope"] != "market":
            final.append(x)
            continue
        s = decided.get(x["title"].strip(), "market")
        if s == "off":
            dropped_off += 1
            continue
        if s in ("sector", "macro", "market") and s != x["scope"]:
            x = {**x, "scope": s, "scope_source": "llm"}
            changed += 1
        final.append(x)
    for x in off_items:
        s = decided.get(x["title"].strip(), "off")
        if s in ("sector", "macro", "market"):
            x = {**x, "scope": s, "scope_source": "llm", "restored": True}
            final.append(x)
            restored += 1

    final.sort(key=lambda y: (y.get("date") or "", y["title"]))
    for n, x in enumerate(final, 1):
        x["id"] = f"n{n:04d}"
    out = {"meta": {**filtered["meta"],
                    "llm_review": {"reviewed": len(candidates), "market_reclassified": changed,
                                   "market_dropped": dropped_off, "restored_from_off": restored}},
           "items": final}
    with open(OUT_FILTERED, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n[INFO] market 桶：改判 {changed} 条，剔除 {dropped_off} 条；未命中桶收回 {restored} 条")
    print(f"[INFO] 复核后语料 {len(final)} 条 -> {OUT_FILTERED}")
    print("   scope 分布:", dict(collections.Counter(x["scope"] for x in final)))


if __name__ == "__main__":
    main()

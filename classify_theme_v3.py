# -*- coding: utf-8 -*-
"""v3 主题层：把事件标成「概念 + 驱动」，取代 v2 的「自由主题 + LLM 归并」。

为什么要改
    v2 的做法是：抽取时让模型自由写一个主题名（1031 条事件产生 919 个原始主题），
    再让模型把它们归并成 41 个规范主题，最后把 41 个主题映射到概念。三个问题：
      1. 归并要反复调模型，结果还会漂，需要人工补丁去补
      2. 41 个主题是一维平铺的稀疏清单，遇到清单里没有的就只能强行归类
      3. 主题到概念是一对多，"驱动"和"落点"被混在同一个标签里

    v3 改成两个**各自封闭**的集合相乘：
      概念（12 个，由产品反推，产品层互斥） × 驱动（11 个，沿用 FinKario 的框架）
    事件的主题是组合的产物——不需要维护主题名词表，也不需要归并。
    新的组合自动就是一个新主题；两个集合都套不上时才进"待定"。

输出
    _yf_data/theme_assign_v3.json
      {"meta": {...},
       "events": {"event_001": {"concepts": ["ai", "semiconductor"], "driver": "Supply"}, ...}}
    concepts 允许为空（= 对我们的产品货架没有落点）。

用法
    python classify_theme_v3.py --limit 30 --api_key sk-...   # 先小样本验证提示词
    python classify_theme_v3.py --api_key sk-...              # 全量（已标注的跳过，可续跑）
"""

import argparse
import collections
import json
import os
import time

parser = argparse.ArgumentParser()
parser.add_argument("--events", default=os.path.join("_yf_data", "events_v2_all.json"))
parser.add_argument("--concepts", default=os.path.join("_yf_data", "concepts.json"))
parser.add_argument("--out", default=os.path.join("_yf_data", "theme_assign_v3.json"))
parser.add_argument("--events-out", default=os.path.join("_yf_data", "events_v3_all.json"),
                    help="把标注合并进事件文件，供 build_kg_yf.py --suffix _v3 读取")
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--batch", type=int, default=15, help="每次请求带多少条事件")
parser.add_argument("--workers", type=int, default=4)
parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
parser.add_argument("--max-concepts", type=int, default=3)
args = parser.parse_args()

if not args.api_key:
    raise SystemExit("需要 --api_key 或环境变量 OPENAI_API_KEY")

# 11 类驱动（沿用 FinKario 的 driver_category 框架），冒号后是给模型的判据
DRIVERS = {
    "Supply": "供给：产能、产量、供应链、原材料、库存、出口管制带来的供给变化",
    "Demand": "需求：订单、销量、渗透率、用户增长、消费意愿",
    "Revenue": "收入与盈利：营收、利润、定价、变现能力",
    "Efficiency/Cost": "效率与成本：单位成本、算力成本、降本增效",
    "Strategic Action": "战略动作：并购、重组、合作、资本开支、回购、管理层变动",
    "Technology/Innovation": "技术与创新：新产品、新架构、技术突破、产品发布",
    "Policy/Regulation": "政策与监管：立法、监管处罚、合规、牌照、政府政策",
    "Macro & FX": "宏观与汇率：利率、通胀、就业、经济增速、汇率、央行操作",
    "Geopolitics": "地缘政治：制裁、冲突、贸易摩擦、关税、国际关系",
    "Market Flow": "市场资金面：指数涨跌、资金流、估值、情绪、板块轮动",
    "Industry Cycle": "行业周期：景气度、供需周期、库存周期、行业展望",
}

PROMPT = """你在给金融新闻事件做标注，为每条事件判定两件事。

【一】它影响哪些「可投资概念」（最多 {max_c} 个，按相关性从高到低排序）
只能从下面这些 key 里选。如果这条新闻对我们的产品货架没有落点，返回空数组 []。
{concepts}

【二】它属于哪一类「驱动」（只能选一个）
{drivers}

【硬性要求】
1. concepts 和 driver 的取值必须严格来自上面的清单，不允许自创任何新值。
2. concepts 宁缺毋滥：确实影响到该概念才放进来，最多 {max_c} 个，没有就返回 []。
   一条新闻可以同时影响多个概念（例如"美联储加息"会同时影响债券和权益）。
3. driver 必须选一个，选最核心的那条因果。
4. theme_hint 是上一版给出的自由主题名，仅供参考，不要被它限制。
5. 只输出 JSON 数组，不要输出解释或 markdown 代码块。

【事件列表】
{payload}

【输出格式】
[{{"i": 1, "concepts": ["ai"], "driver": "Supply"}}, ...]"""


def build_concepts_text(cj):
    """概念清单：名字 + 锚定产品 + 代表成员，给模型当判据。"""
    lines = []
    for key, c in cj.items():
        funds = "、".join(f["ticker"] for f in c.get("anchor_funds", []))
        members = "、".join(c.get("members", [])[:8])
        tag = "（资产型概念）" if c.get("concept_type") == "asset" else ""
        lines.append("  - %s %s%s：对应产品 %s；代表成分 %s"
                     % (key, c["name"], tag, funds or "无", members or "无"))
    return "\n".join(lines)


def call_llm(prompt):
    # 惰性导入：只做离线动作（合并事件文件、统计）时不依赖 openai 库
    import openai
    openai.api_key = args.api_key
    openai.api_base = args.api_base
    last = None
    for attempt in range(3):
        try:
            resp = openai.ChatCompletion.create(
                model=args.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            return resp["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def parse_json_array(text):
    text = (text or "").strip()
    if "```" in text:
        text = text.split("```")[1]
        text = text[text.index("["):] if "[" in text else text
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j < 0:
        raise ValueError("回复里没有 JSON 数组")
    return json.loads(text[i:j + 1])


def main():
    events = json.load(open(args.events, encoding="utf-8"))
    if isinstance(events, dict):
        events = events["events"]
    if args.limit:
        events = events[:args.limit]

    cj = json.load(open(args.concepts, encoding="utf-8"))["concepts"]
    concept_keys = set(cj.keys())
    concepts_text = build_concepts_text(cj)
    drivers_text = "\n".join("  - %s %s" % (k, v) for k, v in DRIVERS.items())

    # 断点续跑：已经标好的不重复花钱
    done = {}
    if os.path.exists(args.out):
        done = json.load(open(args.out, encoding="utf-8")).get("events", {})
    todo = [(i, ev) for i, ev in enumerate(events, 1) if "event_%03d" % i not in done]
    print("总事件 %d 条 | 已标注 %d 条 | 本次待标注 %d 条" % (len(events), len(done), len(todo)))
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]

    def work(batch):
        payload = [{
            "i": i,
            "title": ev.get("event_type_summary") or ev.get("_title"),
            "type": ev.get("event_type"),
            "category": ev.get("event_category"),
            "polarity": ev.get("polarity"),
            "asset_class": ev.get("asset_class"),
            "rationale": (ev.get("rationale_text") or "")[:300],
            "companies": (ev.get("companies_kept") or ev.get("companies") or [])[:6],
            "theme_hint": ev.get("theme"),
        } for i, ev in batch]
        prompt = PROMPT.format(max_c=args.max_concepts, concepts=concepts_text,
                               drivers=drivers_text,
                               payload=json.dumps(payload, ensure_ascii=False))
        rows = parse_json_array(call_llm(prompt))
        by_i = {r["i"]: r for r in rows if isinstance(r, dict) and "i" in r}
        out = {}
        for i, ev in batch:
            r = by_i.get(i)
            if not r:
                raise ValueError("批次结果里缺少 i=%d" % i)
            cs = [c for c in (r.get("concepts") or []) if c in concept_keys]
            dr = r.get("driver")
            if dr not in DRIVERS:
                raise ValueError("驱动不在受控集合内: %r" % dr)
            out["event_%03d" % i] = {"concepts": cs[:args.max_concepts], "driver": dr}
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def save():
        """边跑边存：长任务中途失败不用从头再来（已标注的会被跳过）。"""
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"meta": {"source_events": os.path.basename(args.events),
                                "model": args.model, "max_concepts": args.max_concepts,
                                "n_events": len(done)},
                       "events": done}, f, ensure_ascii=False, indent=1)

    bad = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(work, b) for b in batches]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                done.update(fut.result())
            except Exception as e:
                bad += 1
                print("  [失败] 第 %d 批：%s" % (n, e))
            if n % 5 == 0:
                save()
            if n % 5 == 0 or n == len(batches):
                print("  进度 %d/%d 批" % (n, len(batches)))

    save()

    # 把 concepts / driver 合并进事件文件：v3 建图只读这一个文件，避免两处来源。
    # 只在全量运行时写，避免 --limit 的小样本把完整文件覆盖掉。
    if args.limit:
        print("（--limit 模式，跳过事件文件合并）")
        return
    merged = []
    for i, ev in enumerate(events, 1):
        a = done.get("event_%03d" % i)
        if not a:
            continue
        e = dict(ev)
        e["concepts"] = a["concepts"]
        e["driver"] = a["driver"]
        merged.append(e)
    with open(args.events_out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    print("合并后的事件文件: %s（%d 条）" % (args.events_out, len(merged)))

    nc = collections.Counter(len(v["concepts"]) for v in done.values())
    cd = collections.Counter(c for v in done.values() for c in v["concepts"])
    dd = collections.Counter(v["driver"] for v in done.values())
    pairs = {(c, v["driver"]) for v in done.values() for c in v["concepts"]}
    print()
    print("标注完成 %d 条，失败批次 %d" % (len(done), bad))
    print("每条事件的概念个数分布:", dict(sorted(nc.items())))
    print("驱动分布:", dict(dd.most_common()))
    print("概念被引用次数:", dict(cd.most_common()))
    print("用到的 (概念,驱动) 组合数: %d / %d" % (len(pairs), len(concept_keys) * len(DRIVERS)))
    print("输出:", args.out)


if __name__ == "__main__":
    main()

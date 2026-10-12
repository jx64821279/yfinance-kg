# -*- coding: utf-8 -*-
"""决策层：拿「客户子图」+「事实清单」让模型给出下一步沟通决策。

为什么单独一步
    简报（customer_brief.py）是**确定性的事实清单**：暴露多少、缺口在哪、价格怎么走，
    全部可复算可审计。这一步是**模型的判断**：该不该联系、聊什么、推什么。
    两者分开，才能分辨"数字错了"和"判断偏了"。

和前一条链路的关系
    检索层（kg_subgraph.py）已经取出了有界子图，里面除了汇总数字，还有**代表事件**
    （每个概念 top-k，正负两头对称取）。这一步就是把这些事件喂给模型，
    要求每条结论都挂到具体事件上——也就是 RAG 的最后一段。

输入
    --subgraph  子图 JSON（必需）：事实 + 证据事件
    --brief     简报 JSON（可选）：如果给了，用简报里的汇总数字，保证和事实清单一致

输出
    _yf_data/decisions/<customer>.json
      {"priority": "高|中|低", "summary": "...",
       "risks": [{"point": "...", "evidence": ["event_012"]}],
       "opportunities": [...], "actions": [...],
       "check": {...证据与产品是否越界的自检结果...}}

用法
    python customer_decision.py --subgraph _yf_data/subs/C009.json --api_key sk-...
    python customer_decision.py --subgraph _yf_data/subs/C009.json --api_key sk-... --dry
"""

import argparse
import collections
import json
import os
import time

parser = argparse.ArgumentParser()
parser.add_argument("--subgraph", default=None)
parser.add_argument("--subdir", default=None, help="批量：这个目录下所有子图各出一份决策")
parser.add_argument("--briefs", default=None, help="批量：简报目录（按 <客户>.json 匹配）")
parser.add_argument("--workers", type=int, default=4, help="批量时的并发数")
parser.add_argument("--recheck", action="store_true",
                    help="不调模型，用现有子图重跑自检（改了自检逻辑后复核用）")
parser.add_argument("--brief", default=None, help="简报 JSON（可选，用它的汇总数字）")
parser.add_argument("--outdir", default=os.path.join("_yf_data", "decisions"))
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--max-evidence", type=int, default=24, help="最多把几条证据事件喂给模型")
parser.add_argument("--dry", action="store_true", help="只打印要发给模型的 payload，不调模型")
args = parser.parse_args()

PROMPT = """你是资产管理顾问。基于下面的事实，为这位客户给出下一步的沟通决策。

【事实】
%s

【要求】
1. 只依据事实，不要编造任何数字或事件。
2. 每条结论都必须给 evidence，且只能引用 evidence_events 里出现过的 event_id。
3. 输出**严格的 JSON**（不要 markdown 代码块、不要多余文字），字段如下：
{"priority": "高|中|低",
 "summary": "一句话结论",
 "risks": [{"point": "……", "evidence": ["event_012"]}],
 "opportunities": [{"point": "……", "evidence": ["event_034"]}],
 "actions": [{"action": "……", "timing": "……", "reason": "……", "evidence": ["event_056"]}]}
4. risks / opportunities 各不超过 3 条，actions 不超过 3 条；action 要具体到
   "联系还是不联系、聊什么切入、推什么或不推什么"。
5. priority 判断依据：暴露强度、时机信号（到期/闲钱/冷却期）、有没有被拒过的产品。

【硬约束，违反即视为错误】
A. 只能提及 allowed_products 和 blocked_products 里的产品代码，不允许出现其他产品。
B. evidence 只能用 evidence_events 里的 event_id，不允许自己编号。
C. performance 是客户持仓近一周的**实际涨跌（后验）**，concept_exposure 是**事件面（前瞻）**；
   可以说两者互相印证或背离，但不要把价格涨跌说成某个事件造成的。
D. market_channel 是"大盘/情绪"层面的信号，只能作背景提示，不能说成对某个具体产品的推荐。
E. blocked_products 只能以"曾被拒绝、可换角度再谈"的方式提及。
F. 到期月份只精确到"月"，不要推算成具体天数。"""


def build_payload(sub, brief, max_evidence):
    c = sub["customer"]
    funds = sub["funds"]
    allowed = [f["ticker"] for f in funds]
    # 缺口：优先用简报里的（它带候选产品与被拒产品），子图的缺口只有目标本身
    gap_src = ((brief or {}).get("gaps") or sub.get("gaps") or [])
    blocked = sorted({d["ticker"] for gp in gap_src for d in (gp.get("blocked") or [])})
    held = {f["ticker"] for f in funds}

    # 证据事件：每概念 top-k（正负对称），按 |贡献| 排；同一事件在多个概念下只留一次
    ev, seen = [], set()
    for cname, items in (sub["selected_events"]["by_concept"] or {}).items():
        for it in items:
            if it["event_id"] in seen:
                continue
            seen.add(it["event_id"])
            ev.append({"event_id": it["event_id"], "date": it.get("date"), "concept": cname,
                       "driver": it.get("driver"), "polarity": it.get("polarity"),
                       "contribution": it.get("contribution"), "title": it.get("title")})
    for it in (sub["selected_events"].get("market") or []):
        if it["event_id"] in seen:
            continue
        seen.add(it["event_id"])
        ev.append({"event_id": it["event_id"], "date": it.get("date"), "concept": "（市场层面）",
                   "driver": it.get("driver"), "polarity": it.get("polarity"),
                   "contribution": it.get("contribution"), "title": it.get("title")})
    ev.sort(key=lambda x: -abs(x["contribution"] or 0))
    ev = ev[:max_evidence]

    expos = (brief or {}).get("exposure") or {}
    if expos:                                   # 有简报就用简报的汇总数字，保证一致
        concept_exposure = expos.get("top_concepts") or []
        market_channel = expos.get("market_channel") or []
        performance = expos.get("performance")
    else:                                       # 没给简报就退化成子图里的粗口径
        concept_exposure, market_channel, performance = [], [], None

    return {
        "customer": {k: c.get(k) for k in ("customer_id", "name", "age", "occupation", "city",
                                           "risk_level", "total_assets_cny", "idle_cash_cny",
                                           "time_deposit_cny", "deposit_maturity")},
        "holdings": [{"ticker": f["ticker"], "weight_pct": f["weight_pct"],
                      "concept": f["concept_name"]} for f in funds],
        "concept_exposure": [{k: t[k] for k in ("concept", "net", "n_events", "n_days", "by_driver")
                              if k in t} for t in (concept_exposure[:6] if concept_exposure
                                                   else _concept_fallback(sub))],
        "market_channel": market_channel,
        "performance": performance,
        "gaps": [{"target": gp.get("target"), "evidence": gp.get("evidence"),
                  "candidates": [x["ticker"] for x in (gp.get("candidates") or [])],
                  "blocked": [x["ticker"] for x in (gp.get("blocked") or [])]}
                 for gp in gap_src],
        "timing": (brief or {}).get("timing") or {"items": []},
        "evidence_events": ev,
        "allowed_products": allowed,
        "blocked_products": blocked,
        "note": {"performance": "持仓近一周实际涨跌（后验）",
                 "concept_exposure": "事件面（前瞻）", "held_products": sorted(held)},
    }


def _concept_fallback(sub):
    """没给简报时，用子图的选取结果粗算一下每个概念的净贡献。"""
    agg = collections.defaultdict(lambda: {"net": 0.0, "n_events": 0})
    for cname, items in (sub["selected_events"]["by_concept"] or {}).items():
        for it in items:
            agg[cname]["net"] += it.get("contribution") or 0.0
            agg[cname]["n_events"] += 1
    return [{"concept": k, "net": round(v["net"], 3), "n_events": v["n_events"]}
            for k, v in sorted(agg.items(), key=lambda x: -abs(x[1]["net"]))]


def call_llm(prompt):
    import openai
    openai.api_key = args.api_key
    openai.api_base = args.api_base
    last = None
    for attempt in range(3):
        try:
            r = openai.ChatCompletion.create(model=args.model,
                                            messages=[{"role": "user", "content": prompt}],
                                            temperature=0.2)
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def parse_json_obj(text):
    text = (text or "").strip()
    if "```" in text:
        text = text.split("```")[1]
        text = text[text.index("{"):] if "{" in text else text
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("回复里没有 JSON 对象")
    return json.loads(text[i:j + 1])


# 产品货架（13 只 ETF）。只在它们里面查"越界推荐"：
# 之前用宽泛的大写正则，把货币代码（CNY）之类的也当成了产品，属于误报。
UNIVERSE = {"SMH", "SOXX", "AIQ", "BOTZ", "KWEB", "CQQQ", "QQQ",
            "XLK", "TLT", "AGG", "GLD", "USO", "UUP"}


def check(decision, payload):
    """自检：引用的 event_id 和产品代码是不是都在允许范围内。

    模型会编东西（之前就造过不在受控集合里的产品），所以这一步必须跑。
    """
    import re
    ok_ids = {e["event_id"] for e in payload["evidence_events"]}
    ok_prod = set(payload["allowed_products"]) | set(payload["blocked_products"])
    bad_ids, bad_prod = [], []
    for sec in ("risks", "opportunities", "actions"):
        for item in decision.get(sec) or []:
            for e in item.get("evidence") or []:
                if e not in ok_ids:
                    bad_ids.append(e)
    blob = json.dumps(decision, ensure_ascii=False)
    for t in sorted(UNIVERSE):
        if re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % t, blob) and t not in ok_prod:
            bad_prod.append(t)
    return {"bad_evidence_ids": sorted(set(bad_ids)), "products_out_of_scope": sorted(set(bad_prod)),
            "n_evidence_events": len(ok_ids)}


def main():
    if args.recheck:
        recheck()
        return
    if args.subdir:
        batch()
        return
    if not args.subgraph:
        raise SystemExit("请给 --subgraph 单个客户，或用 --subdir 批量")
    decide_one(args.subgraph, args.brief, verbose=True)


def recheck():
    """不调模型，用现有的子图 + 简报重跑一遍自检（改了自检逻辑后用来复核）。"""
    import glob
    rows, bad = [], 0
    for p in sorted(glob.glob(os.path.join(args.outdir, "*.json"))):
        d = json.load(open(p, encoding="utf-8"))
        cid = os.path.basename(p)[:-5]
        sp = os.path.join(args.subdir, "%s.json" % cid) if args.subdir else None
        bp = os.path.join(args.briefs, "%s.json" % cid) if args.briefs else None
        if not sp or not os.path.exists(sp):
            continue
        sub = json.load(open(sp, encoding="utf-8"))
        brief = json.load(open(bp, encoding="utf-8")) if bp and os.path.exists(bp) else None
        payload = build_payload(sub, brief, args.max_evidence)
        d["_check"] = check(d, payload)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        n_bad = len(d["_check"]["bad_evidence_ids"]) + len(d["_check"]["products_out_of_scope"])
        bad += 1 if n_bad else 0
        rows.append((cid, d.get("priority"), n_bad, d["_check"]))
    print("复核 %d 份，自检未过 %d 份" % (len(rows), bad))
    for cid, pr, n, chk in rows:
        if n:
            print("  %s 优先级%s 越界事件%s 越界产品%s"
                  % (cid, pr, chk["bad_evidence_ids"], chk["products_out_of_scope"]))


def decide_one(sub_path, brief_path, verbose=False):
    sub = json.load(open(sub_path, encoding="utf-8"))
    brief = json.load(open(brief_path, encoding="utf-8")) if brief_path else None
    payload = build_payload(sub, brief, args.max_evidence)
    body = json.dumps(payload, ensure_ascii=False, indent=1)

    if args.dry:
        print(body)
        print("\n[DRY] payload 约 %d 字符，证据事件 %d 条"
              % (len(body), len(payload["evidence_events"])))
        return None
    if not args.api_key:
        raise SystemExit("需要 --api_key 或环境变量 OPENAI_API_KEY")

    decision = parse_json_obj(call_llm(PROMPT % body))
    decision["_check"] = check(decision, payload)
    decision["_meta"] = {"customer_id": payload["customer"]["customer_id"],
                         "model": args.model, "subgraph": os.path.basename(sub_path),
                         "brief": os.path.basename(brief_path) if brief_path else None}

    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "%s.json" % payload["customer"]["customer_id"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(decision, f, ensure_ascii=False, indent=1)

    if not verbose:
        return decision
    c = payload["customer"]
    print("%s %s | 优先级 %s" % (c["customer_id"], c["name"], decision.get("priority")))
    print("  结论：%s" % decision.get("summary"))
    for sec, name in (("risks", "风险"), ("opportunities", "机会"), ("actions", "行动")):
        for item in decision.get(sec) or []:
            ev = "、".join(item.get("evidence") or [])
            print("  [%s] %s%s" % (name, item.get("point") or item.get("action") or "",
                                   ("  ← %s" % ev) if ev else ""))
            if sec == "actions":
                print("        时机：%s｜理由：%s" % (item.get("timing"), item.get("reason")))
    chk = decision["_check"]
    print("  自检：引用事件 %d 条，越界事件 %s，越界产品 %s"
          % (chk["n_evidence_events"], chk["bad_evidence_ids"] or "无",
             chk["products_out_of_scope"] or "无"))
    print("  -> %s" % path)
    return decision


def batch():
    """批量：一个目录下所有子图各出一份决策。"""
    import glob
    from concurrent.futures import ThreadPoolExecutor, as_completed

    subs = sorted(glob.glob(os.path.join(args.subdir, "*.json")))
    if not subs:
        raise SystemExit("目录里没有子图：%s" % args.subdir)
    if not args.api_key and not args.dry:
        raise SystemExit("需要 --api_key 或环境变量 OPENAI_API_KEY")

    def run(p):
        cid = os.path.basename(p)[:-5]
        bp = os.path.join(args.briefs, "%s.json" % cid) if args.briefs else None
        if bp and not os.path.exists(bp):
            bp = None
        return cid, decide_one(p, bp)

    print("批量决策：%d 个客户，并发 %d" % (len(subs), args.workers))
    out, bad = {}, []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(run, p) for p in subs]
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                cid, d = fut.result()
                if d:
                    out[cid] = d
            except Exception as e:
                bad.append(str(e))
            if n % 5 == 0 or n == len(subs):
                print("  进度 %d/%d" % (n, len(subs)))

    if out:
        rank = {"高": 0, "中": 1, "低": 2}
        print("\n优先级汇总：")
        for cid, d in sorted(out.items(), key=lambda x: rank.get(x[1].get("priority"), 9)):
            chk = d["_check"]
            flag = "" if not (chk["bad_evidence_ids"] or chk["products_out_of_scope"]) else "  ⚠自检未过"
            print("  %-6s %-4s %s%s" % (cid, d.get("priority") or "?", (d.get("summary") or "")[:52], flag))
        n_bad = sum(1 for d in out.values()
                    if d["_check"]["bad_evidence_ids"] or d["_check"]["products_out_of_scope"])
        print("\n共 %d 份，自检未过 %d 份；生成失败 %d 份" % (len(out), n_bad, len(bad)))
    for b in bad:
        print("  [失败] %s" % b)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""客户视角简报：以“客户”为主线，算他的暴露、缺口、时机，产出行动建议。

和“新闻 → 产品 → 客户”的营销式推送相反，这里是：

    一个客户 → 他持有什么 / 最近关心什么 → 这些暴露在哪些事件下 → 对他意味着什么

三层分工：
    知识层（图，共享）    Event / Theme / Concept / Stock / Fund / AssetClass
    客户层（表，私有）    持仓 / App 行为 / 客户经理交流 / offer
    决策层（本脚本）      暴露地图 + 需求缺口 + 时机与避雷 → 行动建议

暴露、缺口、时机全部是**确定性计算**（可复算、可审计），
只有最后把事实写成给人看的建议那一步才用 LLM（--llm 打开）。

用法：
    python customer_brief.py --customer C009
    python customer_brief.py --all --outdir data/customer_demo/briefs
    python customer_brief.py --customer C009 --llm --api_key sk-xxx
"""

import argparse
import collections
import csv
import json
import os
from datetime import date

parser = argparse.ArgumentParser()
parser.add_argument("--kg", default=os.path.join("src", "kg", "kg_yf_v2.json"))
parser.add_argument("--exposure", default=os.path.join("_yf_data", "exposure_v2.json"))
parser.add_argument("--events", default=os.path.join("_yf_data", "events_v2_all.json"))
parser.add_argument("--customers", default=os.path.join("data", "customer_demo", "customers_all_in_one.csv"))
parser.add_argument("--customer", default=None, help="只出一份简报，例如 C009")
parser.add_argument("--all", action="store_true", help="给所有客户各出一份")
parser.add_argument("--outdir", default=None, help="把简报写成 JSON 到这个目录")
parser.add_argument("--top-themes", type=int, default=6)
parser.add_argument("--rank", default="net", choices=["net", "damp", "avg"],
                    help="主题排序口径：net=净暴露总量（默认），damp=净暴露/sqrt(条数)，avg=每条均值")
parser.add_argument("--cooldown-days", type=int, default=7, help="最近 N 天内联系过的，提示冷却")
parser.add_argument("--maturity-days", type=int, default=120, help="定期到期在 N 天内算时机信号")
parser.add_argument("--llm", action="store_true", help="调 LLM 把事实写成行动建议")
parser.add_argument("--workers", type=int, default=4, help="--llm 时的并发数")
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
args = parser.parse_args()

# ---------------------------------------------------------------- 口径参数

# 事件的“实质性”权重：泛泛的大盘综述不该和公司事件同权重。
# 口径 1（下调打击面）：权重只做温和折扣，最轻的档也有 0.36，
# 避免把“大盘回调”这类对权益客户真正重要的风险压没。
W_CATEGORY = {
    "CorporateEvents": 1.0,
    "CommodityEvents": 1.0,
    "PolicyEvents": 0.9,
    "MacroEvents": 0.8,
    "MarketEvents": 0.6,
}
W_SCOPE = {"entity": 1.0, "sector": 0.9, "macro": 0.8, "market": 0.7, "weak": 0.6}

# 口径 2（去重复堆积）：同一主题反复报道，信息量不是线性叠加的。
# 默认按“净暴露总量”排序（反映真实敞口），同时给出覆盖天数 n_days 和
# 每条均值 avg_per_event 供人判断；想要阻尼排序可以 --rank damp。
RANK_DAMP = "default: abs(net); optional: abs(net)/sqrt(n_events) 或 abs(avg_per_event)"

# 产品的最低风险等级（C1 最保守 → C5 最激进）。
# 注意：这张表是为了让 demo 自洽、按现有客户持仓倒推出来的，
# 真实业务里应该由合规口径给出。
PRODUCT_RISK_FLOOR = {
    "AGG": "C1", "TLT": "C1", "GLD": "C1", "UUP": "C1",
    "QQQ": "C2", "AIQ": "C2",
    "XLK": "C3", "BOTZ": "C3",
    "SMH": "C4", "SOXX": "C4", "KWEB": "C4", "CQQQ": "C4", "USO": "C4",
}


def risk_rank(level):
    """C1 < C2 < ... < C5"""
    return int(str(level).strip().lstrip("Cc") or 0)


# ---------------------------------------------------------------- 数据加载


def load_graph(path):
    kg = json.load(open(path, encoding="utf-8"))
    g = {
        "entities": {e["id"]: e for e in kg["entities"]},
        "fund_by_ticker": {e["ticker"]: e["id"] for e in kg["entities"]
                           if e["type"] == "Fund" and e.get("ticker")},
        "concept_by_name": {e["name"]: e["id"] for e in kg["entities"] if e["type"] == "Concept"},
        "fund_focus": collections.defaultdict(set),
        "concept_anchor_funds": collections.defaultdict(set),
        "fund_asset_class": {},
        "fund_stocks": collections.defaultdict(dict),
        "concept_members": collections.defaultdict(set),
    }
    for e in kg["entities"]:
        if e["type"] == "Fund":
            g["fund_asset_class"][e["id"]] = "asset_class_" + str(e.get("asset_class"))
    for ed in kg["edges"]:
        s, r, t = ed["source"], ed["relation"], ed["target"]
        if r == "FOCUSES_ON":
            g["fund_focus"][s].add(t)
            g["concept_anchor_funds"][t].add(s)
        elif r == "CONSTITUENT":
            g["fund_stocks"][s][t] = float(ed.get("weight") or 0.0)
        elif r == "INCLUDES":
            g["concept_members"][s].add(t)
    return g


def load_exposure(path):
    return json.load(open(path, encoding="utf-8"))["events"]


def load_event_meta(path):
    """事件日期 / 类别 / 范围。events_v2_all.json 与 KG 的 event 顺序一一对应（已校验 1031/1031）。"""
    d = json.load(open(path, encoding="utf-8"))
    lst = d["events"] if isinstance(d, dict) and "events" in d else d
    meta = {}
    for i, e in enumerate(lst):
        meta["event_%03d" % (i + 1)] = {
            "date": e.get("_date"),
            "category": e.get("event_category"),
            "scope": e.get("_scope"),
            "title": e.get("_title"),
            "publisher": e.get("_publisher"),
        }
    return meta


def _split_items(cell):
    return [x for x in (cell or "").split(";") if x]


def load_customers(path):
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            held = {}
            for item in _split_items(r["holdings"]):
                t, w = item.split(":")
                held[t] = int(w.rstrip("%"))
            behaviors = []
            for item in _split_items(r["app_events"]):
                d, b, tt, g = item.split("|")
                behaviors.append({"date": d, "behavior": b, "target_type": tt, "target": g})
            advisors = []
            for item in _split_items(r["advisor_interactions"]):
                d, ch, s = item.split("|")
                advisors.append({"date": d, "channel": ch, "summary": s})
            offers = []
            for item in _split_items(r["offers"]):
                d, tk, reason, st = item.split("|")
                offers.append({"date": d, "ticker": tk, "reason": reason, "status": st})
            out.append({
                "id": r["customer_id"], "name": r["name"], "age": int(r["age"]),
                "city": r["city"], "occupation": r["occupation"],
                "income": int(r["annual_income_cny"]), "risk": r["risk_level"],
                "invest_years": int(r["invest_years"]),
                "total_assets": int(r["total_assets_cny"]),
                "idle_cash": int(r["idle_cash_cny"]),
                "time_deposit": int(r["time_deposit_cny"]),
                "maturity": r["deposit_maturity"], "advisor_id": r["advisor_id"],
                "holdings": held, "behaviors": behaviors,
                "advisors": advisors, "offers": offers,
            })
    return out


def days_between(d1, d2):
    return (date.fromisoformat(d2) - date.fromisoformat(d1)).days


# ---------------------------------------------------------------- 决策层


def materiality(ev_meta):
    cat = W_CATEGORY.get((ev_meta or {}).get("category"), 0.5)
    scp = W_SCOPE.get((ev_meta or {}).get("scope"), 0.5)
    return round(cat * scp, 3)


def exposure_map(cust, g, exposure, meta, rank_mode="damp"):
    """按主题聚合：这个客户在各个主题上的正向 / 负向暴露。"""
    import math
    fid = g["fund_by_ticker"]
    my_funds = {fid[t]: w for t, w in cust["holdings"].items() if t in fid}

    themes = collections.defaultdict(lambda: {"pos": 0.0, "neg": 0.0, "n": 0, "days": set(),
                                              "category": collections.Counter(),
                                              "drivers": collections.defaultdict(float)})
    for eid, ev in exposure.items():
        raw = 0.0
        contributions = []
        for f in ev.get("funds", []):
            if f["fund"] in my_funds:
                c = f["score"] * my_funds[f["fund"]] / 100.0
                raw += c
                contributions.append((f["ticker"], c))
        if abs(raw) < 1e-9:
            continue
        m = materiality(meta.get(eid))
        weighted = raw * m
        theme = g["entities"][eid].get("theme") or "（未分类）"
        slot = themes[theme]
        slot["pos" if weighted > 0 else "neg"] += abs(weighted)
        slot["n"] += 1
        for ticker, c in contributions:
            slot["drivers"][ticker] += c * m
        if (meta.get(eid) or {}).get("date"):
            slot["days"].add(meta[eid]["date"])
        slot["category"][(meta.get(eid) or {}).get("category", "?")] += 1

    rows = []
    for th, v in themes.items():
        net = v["pos"] - v["neg"]
        drivers = sorted(({"ticker": t, "net": round(c, 3)} for t, c in v["drivers"].items()),
                         key=lambda d: -abs(d["net"]))[:4]
        rows.append({
            "theme": th,
            "n_events": v["n"],
            "n_days": len(v["days"]),
            "pos": round(v["pos"], 3),
            "neg": round(v["neg"], 3),
            "net": round(net, 3),
            "avg_per_event": round(net / v["n"], 4) if v["n"] else 0.0,
            "rank_score": round(net / math.sqrt(v["n"]), 4) if v["n"] else 0.0,
            "drivers": drivers,
            "by_category": dict(v["category"]),
        })
    key = {"damp": "rank_score", "net": "net", "avg": "avg_per_event"}[rank_mode]
    rows.sort(key=lambda r: -abs(r[key]))
    return rows


def concentration(cust, g):
    total = sum(cust["holdings"].values()) or 1
    by_class = collections.Counter()
    for t, w in cust["holdings"].items():
        f = g["fund_by_ticker"].get(t)
        ac = g["fund_asset_class"].get(f, "?")
        by_class[str(ac).replace("asset_class_", "")] += w
    top = max(cust["holdings"].items(), key=lambda x: x[1]) if cust["holdings"] else (None, 0)
    return {
        "by_asset_class_pct": {k: round(v * 100.0 / total, 1) for k, v in by_class.items()},
        "top_holding": {"ticker": top[0], "weight_pct": top[1]},
    }


def find_gaps(cust, g):
    """需求缺口：关注过 / 咨询过，但持仓里没有覆盖到。"""
    fid = g["fund_by_ticker"]
    held_funds = [fid[t] for t in cust["holdings"] if t in fid]
    covered_concepts = {c for f in held_funds for c in g["fund_focus"].get(f, set())}
    covered_classes = {g["fund_asset_class"].get(f) for f in held_funds}

    gaps, seen = [], set()
    for b in cust["behaviors"]:
        strong = b["behavior"] in ("咨询客服", "service_chat")
        if b["target_type"] == "concept":
            cid = g["concept_by_name"].get(b["target"])
            if not cid or cid in covered_concepts or ("c", cid) in seen:
                continue
            seen.add(("c", cid))
            gaps.append({"kind": "concept", "target": b["target"], "target_id": cid,
                         "evidence": "%s %s了「%s」" % (b["date"], b["behavior"], b["target"]),
                         "strength": "strong" if strong else "normal"})
        elif b["target_type"] == "asset_class":
            acid = "asset_class_" + b["target"]
            if acid in covered_classes or ("a", acid) in seen:
                continue
            seen.add(("a", acid))
            gaps.append({"kind": "asset_class", "target": b["target"], "target_id": acid,
                         "evidence": "%s %s了「%s」类资产" % (b["date"], b["behavior"], b["target"]),
                         "strength": "strong" if strong else "normal"})
    return gaps


def candidate_products(gap, cust, g):
    """给一个缺口找候选产品。

    口径 3（别把“换形态”说成“没方案”）：
      可选的放 available；被拒过的单独放 blocked 并说明原因（换个角度还能谈）；
      风险等级不够的只记个数，不列出来，避免噪声。
    """
    held = set(cust["holdings"])
    rejected = {o["ticker"] for o in cust["offers"] if o["status"] == "已拒绝"}
    me = risk_rank(cust["risk"])

    if gap["kind"] == "asset_class":
        # 资产类别缺口：直接找同属这个资产类别的产品
        anchors = set()
        funds = {f for f, ac in g["fund_asset_class"].items() if ac == gap["target_id"]}
    else:
        anchors = g["concept_anchor_funds"].get(gap["target_id"], set())
        members = g["concept_members"].get(gap["target_id"], set())
        funds = set(anchors)
        for f, stocks in g["fund_stocks"].items():
            if members and sum(stocks.get(m, 0.0) for m in members) >= 0.15:
                funds.add(f)

    available, blocked, risk_filtered = [], [], 0
    for f in funds:
        t = g["entities"][f].get("ticker")
        if not t or t in held:
            continue
        floor = PRODUCT_RISK_FLOOR.get(t, "C3")
        if risk_rank(floor) > me:
            risk_filtered += 1
            continue
        item = {"ticker": t, "name": g["entities"][f]["name"],
                "anchor": f in anchors, "risk_floor": floor}
        offer = next((o for o in cust["offers"] if o["ticker"] == t), None)
        if t in rejected:
            item["reason"] = (offer or {}).get("reason", "")
            item["rejected_on"] = (offer or {}).get("date", "")
            blocked.append(item)
        else:
            available.append(item)
    available.sort(key=lambda x: (not x["anchor"], x["ticker"]))
    blocked.sort(key=lambda x: (not x["anchor"], x["ticker"]))
    return available[:3], blocked[:2], risk_filtered


def timing_signals(cust, ref_date, cooldown_days, maturity_days):
    """时机信号。

    口径修正（问题二）：客户的定期到期只精确到“月”，所以不再按月初算天数
    （那样会得出“剩余 2 天”这种假精确），改成按月表述。
    """
    sig = {"ref_date": ref_date, "items": []}
    my, mm = int(cust["maturity"][:4]), int(cust["maturity"][5:7])
    ry, rm = int(ref_date[:4]), int(ref_date[5:7])
    months = (my - ry) * 12 + (mm - rm)
    max_months = -(-maturity_days // 30)          # 向上取整
    if months <= 0:
        when = "本月内到期"
    elif months == 1:
        when = "下个月到期"
    else:
        when = "约 %d 个月后到期" % months
    if months <= max_months:
        sig["items"].append({
            "kind": "deposit_maturity", "month": cust["maturity"], "months": months,
            "note": "定期 %s 元将于 %s 到期（%s；数据只到月份，无具体日期）"
                    % (format(cust["time_deposit"], ","), cust["maturity"], when)})
    idle_ratio = cust["idle_cash"] * 1.0 / (cust["total_assets"] or 1)
    if idle_ratio >= 0.08:
        sig["items"].append({"kind": "idle_cash", "ratio": round(idle_ratio, 3),
                             "note": "闲钱 %s 元，占总资产 %.1f%%" % (format(cust["idle_cash"], ","), idle_ratio * 100)})
    if cust["advisors"]:
        last = max(cust["advisors"], key=lambda a: a["date"])
        gap = days_between(last["date"], ref_date)
        sig["last_contact"] = {"date": last["date"], "days_ago": gap,
                               "channel": last["channel"], "summary": last["summary"]}
        if gap <= cooldown_days:
            sig["cooldown"] = True
            sig["items"].append({"kind": "cooldown", "days_ago": gap,
                                 "note": "%d 天前刚联系过（%s），不建议短期重复触达" % (gap, last["channel"])})
    rejected = [o for o in cust["offers"] if o["status"] == "已拒绝"]
    if rejected:
        sig["rejected_offers"] = rejected
        sig["items"].append({"kind": "rejected_offer",
                             "note": "曾拒绝过 %s（%s）" % ("、".join(o["ticker"] for o in rejected),
                                                        rejected[0]["reason"])})
    return sig


def build_brief(cust, g, exposure, meta, ref_date, opts):
    themes = exposure_map(cust, g, exposure, meta, opts.get("rank", "damp"))
    gaps = find_gaps(cust, g)
    for gap in gaps:
        avail, blocked, risk_filtered = candidate_products(gap, cust, g)
        gap["candidates"] = avail
        gap["blocked"] = blocked
        gap["risk_filtered_n"] = risk_filtered
    return {
        "customer": {
            "id": cust["id"], "name": cust["name"], "age": cust["age"], "city": cust["city"],
            "occupation": cust["occupation"], "risk_level": cust["risk"],
            "total_assets": cust["total_assets"], "idle_cash": cust["idle_cash"],
            "time_deposit": cust["time_deposit"], "deposit_maturity": cust["maturity"],
            "holdings": cust["holdings"],
        },
        "concentration": concentration(cust, g),
        "exposure": {
            "top_themes": themes[:opts["top_themes"]],
            "risk_themes": sorted([t for t in themes if t["net"] < 0], key=lambda t: t["net"])[:3],
            "opportunity_themes": sorted([t for t in themes if t["net"] > 0], key=lambda t: -t["net"])[:3],
        },
        "gaps": gaps,
        "timing": timing_signals(cust, ref_date, opts["cooldown_days"], opts["maturity_days"]),
        "meta": {"w_category": W_CATEGORY, "w_scope": W_SCOPE, "risk_floor": PRODUCT_RISK_FLOOR,
                 "rank_mode": opts.get("rank", "damp"), "rank_rule": RANK_DAMP,
                 "source": "kg_yf_v2 + exposure_v2 + customers_all_in_one"},
    }


def brief_to_text(b):
    c = b["customer"]
    L = ["%s %s（%s，%s，风险 %s）" % (c["id"], c["name"], c["occupation"], c["city"], c["risk_level"]),
         "  总资产 %s | 闲钱 %s | 定期 %s（%s 到期）"
         % (format(c["total_assets"], ","), format(c["idle_cash"], ","),
            format(c["time_deposit"], ","), c["deposit_maturity"]),
         "  持仓：" + "、".join("%s %d%%" % (t, w) for t, w in
                            sorted(c["holdings"].items(), key=lambda x: -x[1]))]
    cc = b["concentration"]
    L.append("  资产类别：" + "、".join("%s %.1f%%" % (k, v) for k, v in cc["by_asset_class_pct"].items())
             + "（最大单一持仓 %s %d%%）" % (cc["top_holding"]["ticker"], cc["top_holding"]["weight_pct"]))
    L.append("  暴露主题（按影响排序）：")
    for t in b["exposure"]["top_themes"]:
        L.append("    %-24s 事件%4d条/%3d天  净%+7.2f  均值%+6.3f  排序分%+6.3f"
                 % (t["theme"][:24], t["n_events"], t["n_days"], t["net"],
                    t["avg_per_event"], t["rank_score"]))
        if t.get("drivers"):
            L.append("        驱动持仓：" + "、".join("%s %+.2f" % (d["ticker"], d["net"])
                                                  for d in t["drivers"]))
    if b["gaps"]:
        L.append("  需求缺口：")
        for gp in b["gaps"]:
            cand = "、".join(x["ticker"] for x in gp["candidates"]) or "（本 universe 内无可用产品）"
            L.append("    %s（%s）→ 候选 %s" % (gp["target"], gp["evidence"], cand))
            for blk in gp.get("blocked", []):
                L.append("        曾被拒：%s（%s，%s）→ 可换角度再谈"
                         % (blk["ticker"], blk.get("reason", ""), blk.get("rejected_on", "")))
            if gp.get("risk_filtered_n"):
                L.append("        另有 %d 只同类产品因超出客户风险等级被排除" % gp["risk_filtered_n"])
    else:
        L.append("  需求缺口：无")
    L.append("  时机与避雷：")
    if b["timing"]["items"]:
        for it in b["timing"]["items"]:
            L.append("    - " + it["note"])
    else:
        L.append("    - （无特别时机信号：无临近到期、无常规闲钱、不在冷却期、无被拒记录）")
    return "\n".join(L)


# ---------------------------------------------------------------- LLM 那一层

ADVICE_PROMPT = """你是一名财富管理顾问，下面是一位客户的结构化事实。请写一段给客户经理看的沟通建议。

要求：
1. 3~5 条，每条一句话，先说结论再说依据。
2. 必须引用具体数字（主题名、暴露方向、持仓权重、金额、日期）。
3. 风险提示和机会分开写。
4. 如果客户最近刚被联系过，或之前拒绝过某产品，要在建议里体现出来。
5. 只依据下面的事实，不要编造未出现的信息。

硬约束（违反即视为错误）：
A. 只能提及 allowed_products 和 blocked_products 里的产品代码，绝不允许出现其他产品。
   blocked_products 里的产品只能以“曾被拒绝、可换角度再谈”的方式提及。
B. 讲某个主题时，必须用该主题 drivers 字段里列出的持仓来解释——是哪些持仓让它变正/变负。
   如果 drivers 里没有某个持仓，不要说这个持仓受该主题影响。
C. 数据的到期月份只精确到“月”，不要推算成具体天数。

客户事实（JSON）：
%s
"""


def llm_payload(brief):
    """给模型看的事实：去掉完整产品表，只暴露与该客户相关的产品。"""
    b = json.loads(json.dumps(brief, ensure_ascii=False))
    held = list(b["customer"]["holdings"].keys())
    cand = sorted({c["ticker"] for g in b["gaps"] for c in g.get("candidates", [])})
    blocked = sorted({c["ticker"] for g in b["gaps"] for c in g.get("blocked", [])})
    b["meta"] = {"allowed_products": held + cand, "blocked_products": blocked}
    return b


def llm_advice(brief):
    try:
        import openai
    except ImportError:
        return "（未安装 openai 库，跳过 LLM 建议）"
    openai.api_key = args.api_key
    openai.api_base = args.api_base
    payload = json.dumps(llm_payload(brief), ensure_ascii=False, indent=1)
    resp = openai.ChatCompletion.create(
        model=args.model,
        messages=[{"role": "user", "content": ADVICE_PROMPT % payload}],
        temperature=0.2,
    )
    return resp["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------- main


def main():
    g = load_graph(args.kg)
    exposure = load_exposure(args.exposure)
    meta = load_event_meta(args.events)
    customers = load_customers(args.customers)

    # 参考“今天”：取所有数据里最新的一天，保证任何时候重跑结果一致
    ref_date = max(m["date"] for m in meta.values() if m.get("date"))
    opts = {"top_themes": args.top_themes, "cooldown_days": args.cooldown_days,
            "maturity_days": args.maturity_days, "rank": args.rank}

    if args.customer:
        todo = [c for c in customers if c["id"] == args.customer]
        if not todo:
            raise SystemExit("没有这个客户：%s" % args.customer)
    elif args.all:
        todo = customers
    else:
        raise SystemExit("请指定 --customer C009 或 --all")

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    print("参考日期（数据里最新的一天）：%s\n" % ref_date)
    briefs = {c["id"]: build_brief(c, g, exposure, meta, ref_date, opts) for c in todo}

    advice = {}
    if args.llm:
        if not args.api_key:
            raise SystemExit("--llm 需要 --api_key 或环境变量 OPENAI_API_KEY")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(llm_advice, b): cid for cid, b in briefs.items()}
            for fut, cid in futures.items():
                try:
                    text = fut.result()
                except Exception as e:                       # 单条失败不影响整体
                    text = "（生成失败：%s）" % e
                # 存成一行一条的数组，直接打开 JSON 也能读
                advice[cid] = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for cust in todo:
        brief = briefs[cust["id"]]
        if cust["id"] in advice:
            brief["advice"] = advice[cust["id"]]
        print(brief_to_text(brief))
        if cust["id"] in advice:
            print("  ---- 行动建议（LLM）----")
            for line in advice[cust["id"]]:
                print("  " + line)
        print()
        if args.outdir:
            with open(os.path.join(args.outdir, "%s.json" % cust["id"]), "w", encoding="utf-8") as f:
                json.dump(brief, f, ensure_ascii=False, indent=1)
    if args.outdir:
        print("已写出 %d 份简报到 %s" % (len(todo), args.outdir))


if __name__ == "__main__":
    main()

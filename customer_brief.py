# -*- coding: utf-8 -*-
"""客户视角简报：以“客户”为主线，算他的暴露、缺口、时机，产出行动建议。

和“新闻 → 产品 → 客户”的营销式推送相反，这里是：

    一个客户 → 他持有什么 / 最近关心什么 → 这些暴露在哪些事件下 → 对他意味着什么

三层分工：
    知识层（图，共享）    Event / Theme / Concept / Stock / Fund / AssetClass
    客户层（表，私有）    持仓 / App 行为 / 客户经理交流 / offer
    决策层（本脚本）      暴露地图 + 需求缺口 + 时机与避雷 → 行动建议

取数有两种方式（算的口径完全一样，已逐客户核对过结果一致）：
    ① 直接读文件（默认）    KG json + exposure json + events json + 客户 CSV
    ② 走「客户子图」       --subgraph <file.json>，文件由 kg_subgraph.py 从库里取出

归桶口径：**从产品出发**——每只产品的暴露归入它自己所属的概念
（产品 ↔ 概念一对一，所以既不重复计数，也不会像按主题名归桶那样被打散）。

这一层**只做确定性计算**（暴露、缺口、时机、持仓表现），可复算、可审计；
"该不该联系、聊什么、推什么"那种判断交给决策层（customer_decision.py），
它会把子图里的代表事件作为证据喂给模型，并要求每条结论挂上引用。

用法：
    python customer_brief.py --customer C009
    python customer_brief.py --all --outdir data/customer_demo/briefs
    python customer_brief.py --subgraph _yf_data/subs/C009.json --outdir <目录>
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
parser.add_argument("--subgraph", default=None,
                    help="改为从子图 JSON 取数（由 kg_subgraph.py 生成），不再读上面的文件")
parser.add_argument("--prices", default=None,
                    help="价格 JSON（yf_prices.py 产出）；走子图时不需要，价格在子图里")
parser.add_argument("--customer", default=None, help="只出一份简报，例如 C009")
parser.add_argument("--all", action="store_true", help="给所有客户各出一份")
parser.add_argument("--outdir", default=None, help="把简报写成 JSON 到这个目录")
parser.add_argument("--top-concepts", "--top-themes", dest="top_themes", type=int, default=6,
                    help="简报里列出几个概念（--top-themes 是兼容旧名）")
parser.add_argument("--rank", default="net", choices=["net", "damp", "avg"],
                    help="主题排序口径：net=净暴露总量（默认），damp=净暴露/sqrt(条数)，avg=每条均值")
parser.add_argument("--cooldown-days", type=int, default=7, help="最近 N 天内联系过的，提示冷却")
parser.add_argument("--maturity-days", type=int, default=120, help="定期到期在 N 天内算时机信号")
args = parser.parse_args()

# ---------------------------------------------------------------- 口径参数
# 实质性权重与方向符号放在 kg_common.py，检索层选事件时用的是同一套
from kg_common import SIGN, W_CATEGORY, W_SCOPE, materiality

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
        "fund_concept": {},          # fund_id -> (concept_id, 概念名)，用来"从产品归桶"
        "event_themes": collections.defaultdict(list),   # event_id -> [theme_id]
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
        elif r == "HAS_THEME":
            g["event_themes"][s].append(t)
    # 产品 → 它所属的概念：v3 里"概念 = 产品分组"，每只产品恰好归一概念
    for f, cs in g["fund_focus"].items():
        cid = sorted(cs)[0] if cs else None
        g["fund_concept"][f] = (cid, (g["entities"].get(cid) or {}).get("name") or "（未归类）")
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


def load_subgraph_inputs(path):
    """把「客户子图」翻译成简报原本期待的几个结构。

    检索层负责"把相关的东西取出来"，决策层（本文件）负责"怎么算"。
    所以这里只做形状翻译，算的口径一行都不改。
    """
    sub = json.load(open(path, encoding="utf-8"))
    c = sub["customer"]
    cust = {
        "id": c["customer_id"], "name": c["name"], "age": c.get("age"), "gender": c.get("gender"),
        "city": c.get("city"), "occupation": c.get("occupation"),
        "income": c.get("annual_income_cny"), "risk": c.get("risk_level"),
        "invest_years": c.get("invest_years"), "total_assets": c.get("total_assets_cny"),
        "idle_cash": c.get("idle_cash_cny"), "time_deposit": c.get("time_deposit_cny"),
        "maturity": c.get("deposit_maturity"), "advisor_id": c.get("advisor_id"),
        "holdings": {f["ticker"]: f["weight_pct"] for f in sub["funds"]},
        "behaviors": sub.get("behaviors") or [],
        "advisors": sub.get("advisors") or [],
        "offers": sub.get("offers") or [],
    }

    cs = sub.get("concept_slice") or {}
    funds_all = cs.get("funds_all") or []
    g = {
        "entities": {},
        "fund_by_ticker": {f["ticker"]: f["fund_id"] for f in funds_all},
        "concept_by_name": {v: k for k, v in (cs.get("concept_names") or {}).items()},
        "fund_focus": collections.defaultdict(set),
        "concept_anchor_funds": {k: set(v) for k, v in
                                 (cs.get("concept_anchor_funds") or {}).items()},
        "fund_asset_class": {f["fund_id"]: "asset_class_" + str(f["asset_class"])
                             for f in funds_all if f.get("asset_class")},
        "fund_stocks": {k: {s: float(w or 0.0) for s, w in v.items()}
                        for k, v in (cs.get("fund_stocks") or {}).items()},
        "concept_members": {k: set(v) for k, v in (cs.get("concept_members") or {}).items()},
        "fund_concept": {},
        "event_themes": collections.defaultdict(list),
    }
    for f in funds_all:
        g["entities"][f["fund_id"]] = {"id": f["fund_id"], "type": "Fund",
                                       "name": f["name"], "ticker": f["ticker"]}
        if f.get("concept_id"):
            g["fund_concept"][f["fund_id"]] = (f["concept_id"], f["concept_name"])
            g["fund_focus"][f["fund_id"]].add(f["concept_id"])

    exposure, meta = {}, {}

    def put(eid, polarity, driver, channel, asset_class, date, category, title,
            scope=None, publisher=None):
        e = exposure.setdefault(eid, {"polarity": polarity, "funds": []})
        if eid not in meta:
            meta[eid] = {"date": date, "category": category, "scope": scope,
                         "title": title, "publisher": publisher}
        g["entities"][eid] = {"id": eid, "type": "Event", "name": title or eid,
                              "driver_category": driver, "asset_class": asset_class,
                              "channel": channel}

    for r in sub["exposure_rows"]:
        put(r["event_id"], r.get("polarity"), r.get("driver"), r.get("channel"),
            r.get("asset_class"), r.get("date"), r.get("category"), r.get("title"),
            scope=r.get("scope"), publisher=r.get("publisher"))
        exposure[r["event_id"]]["funds"].append(
            {"fund": r["fund_id"], "ticker": r["ticker"], "score": r["score"]})
    for r in sub.get("market_events") or []:
        put(r["event_id"], r.get("polarity"), r.get("driver"), "market",
            r.get("asset_class"), r.get("date"), r.get("category"), r.get("title"),
            scope=r.get("scope"))

    gaps = []
    for gp in sub.get("gaps") or []:
        kind = gp.get("kind")
        tid = (g["concept_by_name"].get(gp["target"]) if kind == "concept"
               else "asset_class_" + gp["target"])
        gaps.append({"kind": kind, "target": gp["target"], "target_id": tid,
                     "evidence": gp.get("evidence"), "strength": gp.get("strength", "normal")})
    return {"cust": cust, "g": g, "exposure": exposure, "meta": meta, "gaps": gaps,
            "ref_date": (sub.get("meta") or {}).get("ref_date"),
            "prices": sub.get("prices") or {}}


def load_prices(path):
    """价格文件（yf_prices.py 产出）→ {ticker: {pct_5d, pct_1d, ...}}。"""
    if not path or not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8")).get("tickers") or {}


# ---------------------------------------------------------------- 决策层


def concept_exposure(cust, g, exposure, meta, rank_mode="net"):
    """按概念聚合：这个客户在各个概念上的正向 / 负向暴露。

    归桶口径：**从产品出发**——每只产品的暴露归入它自己所属的那个概念。
    因为「产品 → 概念」是一对一，每笔暴露只会进一个桶：既不重复计数
    （v3 下一个事件可以同时落多个概念），也不会把桶打碎
    （v3 里多概念事件的主题名是拼接串，按主题名归桶会被拆成好几行）。
    驱动（driver_category）作为二级展开保留。
    """
    import math
    fid = g["fund_by_ticker"]
    my_funds = {fid[t]: w for t, w in cust["holdings"].items() if t in fid}
    buckets = collections.defaultdict(
        lambda: {"pos": 0.0, "neg": 0.0, "events": set(), "days": set(),
                 "drivers": collections.defaultdict(float),
                 "by_driver": collections.defaultdict(float),
                 "category": collections.Counter()})
    for eid, ev in exposure.items():
        m = materiality(meta.get(eid))
        if m <= 0:
            continue
        em = meta.get(eid) or {}
        # v3 的驱动挂在事件上；v2 挂在主题上，这里回退一下
        driver = (g["entities"].get(eid) or {}).get("driver_category")
        if not driver:
            for t in g["event_themes"].get(eid, []):
                driver = (g["entities"].get(t) or {}).get("driver_category")
                if driver:
                    break
        driver = driver or "（未分类）"
        for f in ev.get("funds", []):
            if f["fund"] not in my_funds:
                continue
            w = f["score"] * my_funds[f["fund"]] / 100.0 * m
            if abs(w) < 1e-9:
                continue
            _, cname = g["fund_concept"].get(f["fund"], (None, "（未归类）"))
            b = buckets[cname]
            b["pos" if w > 0 else "neg"] += abs(w)
            b["events"].add(eid)
            b["drivers"][f["ticker"]] += w
            b["by_driver"][driver] += w
            if em.get("date"):
                b["days"].add(em["date"])
            if em.get("category"):
                b["category"][em["category"]] += 1

    rows = []
    for name, v in buckets.items():
        n = len(v["events"])
        net = v["pos"] - v["neg"]
        rows.append({
            "concept": name,
            "n_events": n,
            "n_days": len(v["days"]),
            "pos": round(v["pos"], 3),
            "neg": round(v["neg"], 3),
            "net": round(net, 3),
            "avg_per_event": round(net / n, 4) if n else 0.0,
            "rank_score": round(net / math.sqrt(n), 4) if n else 0.0,
            "drivers": sorted(({"ticker": t, "net": round(c, 3)} for t, c in v["drivers"].items()),
                              key=lambda d: -abs(d["net"]))[:4],
            "by_driver": {k: round(c, 3) for k, c in
                          sorted(v["by_driver"].items(), key=lambda x: -abs(x[1]))},
            "by_category": dict(v["category"]),
        })
    key = {"damp": "rank_score", "net": "net", "avg": "avg_per_event"}[rank_mode]
    rows.sort(key=lambda r: -abs(r[key]))
    return rows


def market_channel(cust, g, exposure, meta, top=5):
    """大盘 / 情绪通道（v3 的 channel=market）。

    这类事件落不到具体产品，但落在客户持有的资产类别上，所以不该直接丢掉。
    口径：事件方向 × 实质性权重 × 该资产类别在客户组合中的占比。
    注：v2 的图里没有 channel 字段，所以这段在 v2 上返回空列表。
    """
    ac_pct = concentration(cust, g)["by_asset_class_pct"]
    buckets = collections.defaultdict(lambda: {"pos": 0.0, "neg": 0.0, "events": set(),
                                               "by_driver": collections.defaultdict(float)})
    for eid, ev in exposure.items():
        node = g["entities"].get(eid) or {}
        if node.get("channel") != "market":
            continue
        w_ac = (ac_pct.get(node.get("asset_class")) or 0.0) / 100.0
        sgn = SIGN.get(ev.get("polarity"), 0.0)
        if w_ac <= 0 or sgn == 0:
            continue
        v = sgn * materiality(meta.get(eid)) * w_ac
        b = buckets[node["asset_class"]]
        b["pos" if v > 0 else "neg"] += abs(v)
        b["events"].add(eid)
        b["by_driver"][node.get("driver_category") or "（未分类）"] += v
    rows = []
    for ac, v in buckets.items():
        rows.append({"asset_class": ac, "n_events": len(v["events"]),
                     "net": round(v["pos"] - v["neg"], 3),
                     "by_driver": {k: round(c, 3) for k, c in
                                   sorted(v["by_driver"].items(), key=lambda x: -abs(x[1]))}})
    rows.sort(key=lambda r: -abs(r["net"]))
    return rows[:top]


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
    """（时机信号：定期到期 / 闲钱 / 冷却期 / 被拒记录）"""
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


def holding_performance(cust, prices, n=5):
    """持仓近一周表现：按持仓权重加权。

    这是"后验"——和事件暴露（前瞻）是两个维度，放在一起客户经理才看得全：
    事件面偏负、价格也确实跌了，说明传导是有效的；两者背离就值得追问原因。
    """
    if not prices:
        return None
    rows, tot, covered = [], 0.0, 0.0
    # 同权重时按代码排序，保证两条取数路径的顺序一致
    for t, w in sorted(cust["holdings"].items(), key=lambda x: (-x[1], x[0])):
        p = prices.get(t) or {}
        pct = p.get("pct_%dd" % n)
        rows.append({"ticker": t, "weight_pct": w, "pct_5d": pct, "pct_1d": p.get("pct_1d"),
                     "last_date": p.get("last_date"), "last_close": p.get("last_close")})
        if pct is not None:
            tot += pct * w / 100.0
            covered += w
    return {"window_days": n, "as_of": next((r["last_date"] for r in rows if r["last_date"]), None),
            "portfolio_pct": round(tot, 6), "coverage_pct": round(covered, 1),
            "holdings": rows}


def build_brief(cust, g, exposure, meta, ref_date, opts, gaps=None, prices=None):
    concepts = concept_exposure(cust, g, exposure, meta, opts.get("rank", "net"))
    market = market_channel(cust, g, exposure, meta)
    perf = holding_performance(cust, prices)
    if gaps is None:
        gaps = find_gaps(cust, g)          # 直接读文件时，缺口在这里算
    else:
        gaps = [dict(gp) for gp in gaps]   # 走子图时，缺口由检索层给出
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
            "top_concepts": concepts[:opts["top_themes"]],
            "risk_concepts": sorted([t for t in concepts if t["net"] < 0], key=lambda t: t["net"])[:3],
            "opportunity_concepts": sorted([t for t in concepts if t["net"] > 0], key=lambda t: -t["net"])[:3],
            "market_channel": market,
            "performance": perf,
        },
        "gaps": gaps,
        "timing": timing_signals(cust, ref_date, opts["cooldown_days"], opts["maturity_days"]),
        "meta": {"w_category": W_CATEGORY, "w_scope": W_SCOPE, "risk_floor": PRODUCT_RISK_FLOOR,
                 "rank_mode": opts.get("rank", "net"), "rank_rule": RANK_DAMP,
                 "bucket_rule": "从产品出发：每只产品的暴露归入它自己所属的概念",
                 "source": opts.get("source", "")},
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
    L.append("  概念暴露（按影响排序）：")
    for t in b["exposure"]["top_concepts"]:
        L.append("    %-22s 事件%4d条/%3d天  净%+7.2f  均值%+6.3f  排序分%+6.3f"
                 % (t["concept"][:22], t["n_events"], t["n_days"], t["net"],
                    t["avg_per_event"], t["rank_score"]))
        if t.get("drivers"):
            L.append("        持仓驱动：" + "、".join("%s %+.2f" % (d["ticker"], d["net"])
                                                   for d in t["drivers"]))
        if t.get("by_driver"):
            L.append("        事件驱动：" + "、".join("%s %+.2f" % (k, v) for k, v in
                                                   list(t["by_driver"].items())[:3]))
    mc = b["exposure"].get("market_channel") or []
    if mc:
        L.append("  市场层面（大盘/情绪，落不到具体产品）：")
        for m in mc:
            drv = "、".join("%s %+.2f" % (k, v) for k, v in list(m["by_driver"].items())[:3])
            L.append("    %-10s 事件%3d条  净%+6.2f  驱动：%s"
                     % (m["asset_class"], m["n_events"], m["net"], drv))
    pf = b["exposure"].get("performance")
    if pf:
        L.append("  持仓近一周表现（截至 %s，%d 个交易日）：组合 %+.2f%%"
                 % (pf["as_of"], pf["window_days"], pf["portfolio_pct"] * 100))
        for r in pf["holdings"]:
            pct = "—" if r["pct_5d"] is None else "%+.2f%%" % (r["pct_5d"] * 100)
            d1 = "—" if r["pct_1d"] is None else "%+.2f%%" % (r["pct_1d"] * 100)
            L.append("    %-6s 权重%2d%%  近一周 %7s  最近一日 %7s" % (r["ticker"], r["weight_pct"], pct, d1))
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


# ---------------------------------------------------------------- main


def main():
    precomputed_gaps = {}
    prices = {}
    if args.subgraph:
        # 取数走「客户子图」：检索层（kg_subgraph.py）已经把相关内容取好了
        src = load_subgraph_inputs(args.subgraph)
        g, exposure, meta = src["g"], src["exposure"], src["meta"]
        customers = [src["cust"]]
        precomputed_gaps[src["cust"]["id"]] = src["gaps"]
        prices = src["prices"]
        # 参考"今天"：以子图里记录的最新一天为准
        ref_date = src["ref_date"] or max(m["date"] for m in meta.values() if m.get("date"))
        source = os.path.basename(args.subgraph)
    else:
        g = load_graph(args.kg)
        exposure = load_exposure(args.exposure)
        meta = load_event_meta(args.events)
        customers = load_customers(args.customers)
        prices = load_prices(args.prices)
        # 参考"今天"：取所有数据里最新的一天，保证任何时候重跑结果一致
        ref_date = max(m["date"] for m in meta.values() if m.get("date"))
        source = "%s + %s + %s" % (os.path.basename(args.kg),
                                   os.path.basename(args.exposure),
                                   os.path.basename(args.customers))
        if prices:
            source += " + %s" % os.path.basename(args.prices)

    opts = {"top_themes": args.top_themes, "cooldown_days": args.cooldown_days,
            "maturity_days": args.maturity_days, "rank": args.rank, "source": source}

    if args.subgraph:
        todo = customers
    elif args.customer:
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
    briefs = {c["id"]: build_brief(c, g, exposure, meta, ref_date, opts,
                                   gaps=precomputed_gaps.get(c["id"]),
                                   prices=prices) for c in todo}

    for cust in todo:
        brief = briefs[cust["id"]]
        print(brief_to_text(brief))
        print()
        if args.outdir:
            with open(os.path.join(args.outdir, "%s.json" % cust["id"]), "w", encoding="utf-8") as f:
                json.dump(brief, f, ensure_ascii=False, indent=1)
    if args.outdir:
        print("已写出 %d 份简报到 %s" % (len(todo), args.outdir))


if __name__ == "__main__":
    main()

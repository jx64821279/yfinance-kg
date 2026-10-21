# -*- coding: utf-8 -*-
"""客户侧信号：事件 → 产品 → 直接持有客户 + 关联客户。

这里放的是**可复用逻辑**，不解析命令行：
  - affected_products()   事件 → 受影响产品
  - affected_customers()  受影响产品 → 直接持有客户
  - related_customers()   关注未持有 / offer / 近期卖出

news_impact.py（事件入口）和 customer_actions.py（客户入口）共用同一套口径。
"""

import collections
import datetime
import sqlite3

from exposure_v2 import ExposureModel
from kg_common import materiality


ACTION_W = {"浏览": 0.30, "点击": 0.50, "搜索": 0.70,
            "咨询客服": 1.00, "service_chat": 1.00}
DEFAULT_ACTION_W = 0.40
KIND_W = {"product": 1.0, "concept": 1.0, "asset_class": 0.6}
INTEREST_CAP = 2.0


def _parse_day(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _days_between(start, end):
    a, b = _parse_day(start), _parse_day(end)
    return (b - a).days if a and b else None


def _ref_date(con, ev):
    """算"相对哪一天"。优先事件日期，其次价格快照的 as_of。"""
    if ev.get("date"):
        return ev["date"][:10]
    r = con.execute("SELECT value FROM meta WHERE key='price_as_of'").fetchone()
    return (r[0] if r and r[0] else "")[:10] or None


def _concept_ids(con, ev):
    """事件里的概念 key → 图里的 concept id；只保留图上存在的概念。"""
    ids = set()
    for c in ev.get("concepts") or []:
        c = str(c or "")
        if not c:
            continue
        ids.add(c if c.startswith("concept_") else "concept_" + c)
    known = {r[0] for r in con.execute("SELECT id FROM node WHERE id IN (%s)"
                                       % ",".join("?" * len(ids)), list(ids))} if ids else set()
    return ids & known if ids else set()


def affected_products(con, ev, in_graph=True, model=None):
    """事件 → 受影响产品。预览事件走 concepts/affected_tickers，入图事件走图上的边。"""
    model = model or ExposureModel.from_db(con)
    if in_graph:
        return model.score_event(ev["id"], ev.get("polarity"))
    affects = ["fund_" + t for t in (ev.get("affected_products") or [])]
    affects += ["stock_" + t for t in (ev.get("affected_tickers") or [])]
    return model.score_concepts(ev.get("concepts") or [], affects, ev.get("polarity"))


def affected_customers(con, rows, ev):
    """受影响产品 → 持有它们的客户（只算直接持有），按影响贡献排序。"""
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    if not tickers:
        return []
    con.row_factory = sqlite3.Row
    info = {r["customer_id"]: dict(r) for r in con.execute(
        "SELECT customer_id, name, risk_level FROM customer")}
    q = ("SELECT customer_id, ticker, weight_pct FROM customer_holding WHERE ticker IN (%s)"
         % ",".join("?" * len(tickers)))
    by_cust = collections.defaultdict(list)
    for r in con.execute(q, tickers):
        by_cust[r["customer_id"]].append((r["ticker"], r["weight_pct"]))

    m = materiality({"category": ev.get("category"), "scope": ev.get("scope")})
    by_ticker = {r["ticker"]: r for r in rows}
    out = []
    for cid, holds in by_cust.items():
        contrib, detail = 0.0, []
        for tk, w in holds:
            r = by_ticker[tk]
            c = r["score"] * (w or 0) / 100.0 * m
            contrib += c
            detail.append({"ticker": tk, "weight_pct": w, "score": r["score"],
                           "contribution": round(c, 6)})
        detail.sort(key=lambda d: -abs(d["contribution"]))
        out.append({"customer_id": cid, "name": (info.get(cid) or {}).get("name"),
                    "risk_level": (info.get(cid) or {}).get("risk_level"),
                    "contribution": round(contrib, 6), "holdings": detail})
    out.sort(key=lambda r: -abs(r["contribution"]))
    return out


def related_customers(con, rows, ev, held_ids, interest_half_life=30.0,
                      min_interest=0.15, sold_days=120):
    """没直接持有受影响产品、但有关联的客户，分四类返回。

    1) watched        关注过但没持有
    2) pending_offer  offer 待定（推荐产品尚未决策）
    3) rejected_offer offer 已拒绝（不再重复推同一产品）
    4) recent_sale    近期卖出过、现在没持有

    只看事件日之前的行为，避免用"未来"的客户数据解释过去的事件。
    """
    con.row_factory = sqlite3.Row
    ref = _ref_date(con, ev)
    if not ref:
        return {"watched": [], "pending_offer": [], "rejected_offer": [], "recent_sale": []}

    info = {r["customer_id"]: dict(r) for r in con.execute(
        "SELECT customer_id, name, risk_level FROM customer")}
    held = collections.defaultdict(dict)
    for r in con.execute("SELECT customer_id, ticker FROM customer_holding"):
        held[r["customer_id"]][r["ticker"]] = True

    fund_id = {r[0]: r[1] for r in con.execute(
        "SELECT ticker, id FROM node WHERE type='Fund' AND ticker IS NOT NULL")}

    # 客户已覆盖的概念 / 资产类别：只看持有产品的锚定概念与资产类别。
    covered_concepts = collections.defaultdict(set)
    covered_classes = collections.defaultdict(set)
    for cid, tickers in held.items():
        fund_ids = [fund_id[t] for t in tickers if t in fund_id]
        if not fund_ids:
            continue
        q = "SELECT rel, dst FROM edge WHERE src IN (%s) AND rel IN ('FOCUSES_ON','IS_CLASS')" \
            % ",".join("?" * len(fund_ids))
        for r in con.execute(q, fund_ids):
            if r["rel"] == "FOCUSES_ON":
                covered_concepts[cid].add(r["dst"])
            elif r["rel"] == "IS_CLASS":
                covered_classes[cid].add(r["dst"])

    event_concepts = _concept_ids(con, ev)
    concept_name = {r[0]: r[1] for r in con.execute(
        "SELECT id, name FROM node WHERE type='Concept'")}
    concept_id = {v: k for k, v in concept_name.items()}
    event_class = ("asset_class_" + ev["asset_class"]) if ev.get("asset_class") else None
    affected_tickers = {r.get("ticker") for r in rows if r.get("ticker")}

    # ---- 1) 关注过但没持有
    interest = collections.defaultdict(lambda: {"score": 0.0, "items": []})
    for b in con.execute("SELECT customer_id, date, behavior, target_type, target "
                         "FROM customer_behavior ORDER BY customer_id, date"):
        cid, ttype, target = b["customer_id"], b["target_type"], b["target"]
        days = _days_between(b["date"], ref)
        if days is None or days < 0:
            continue
        if ttype == "product":
            if target not in affected_tickers or target in held[cid]:
                continue
            label = target
        elif ttype == "concept":
            cid_target = target if target.startswith("concept_") else concept_id.get(target)
            if not cid_target or cid_target not in event_concepts:
                continue
            if cid_target in covered_concepts[cid]:
                continue
            label = concept_name.get(cid_target, target)
        elif ttype == "asset_class":
            if not event_class or ("asset_class_" + target) != event_class:
                continue
            if event_class in covered_classes[cid]:
                continue
            label = target
        else:
            continue

        w = (ACTION_W.get(b["behavior"], DEFAULT_ACTION_W)
             * (0.5 ** (days / max(interest_half_life, 1.0)))
             * KIND_W.get(ttype, 0.5))
        interest[cid]["score"] += w
        interest[cid]["items"].append({
            "kind": ttype, "target": label, "behavior": b["behavior"],
            "date": b["date"], "days_ago": days, "weight": round(w, 4),
        })

    watched = []
    for cid, acc in interest.items():
        if cid in held_ids:
            continue
        score = min(acc["score"], INTEREST_CAP)
        if score < min_interest:
            continue
        acc["items"].sort(key=lambda x: (-x["weight"], x["date"]))
        top = acc["items"][0]
        watched.append({
            "customer_id": cid, "name": (info.get(cid) or {}).get("name"),
            "risk_level": (info.get(cid) or {}).get("risk_level"),
            "score": round(score, 4), "items": acc["items"][:3],
            "evidence": "%s %s了「%s」" % (top["date"], top["behavior"], top["target"]),
        })
    watched.sort(key=lambda r: -r["score"])

    # ---- 2/3) offer 待定 / 已拒绝
    pending, rejected = [], []
    if affected_tickers:
        q = ("SELECT customer_id, date, ticker, status, reason FROM customer_offer "
             "WHERE ticker IN (%s) ORDER BY customer_id, date"
             % ",".join("?" * len(affected_tickers)))
        for o in con.execute(q, sorted(affected_tickers)):
            cid, tk = o["customer_id"], o["ticker"]
            days = _days_between(o["date"], ref)
            if days is None or days < 0:
                continue
            item = {"customer_id": cid, "name": (info.get(cid) or {}).get("name"),
                    "risk_level": (info.get(cid) or {}).get("risk_level"),
                    "ticker": tk, "date": o["date"], "days_ago": days,
                    "status": o["status"], "reason": o["reason"],
                    "also_held": bool(set(held[cid]) & affected_tickers)}
            st = o["status"] or ""
            if "拒绝" in st or "reject" in st.lower():
                item["suppress"] = True
                rejected.append(item)
            elif "待定" in st or "pending" in st.lower():
                pending.append(item)
    pending.sort(key=lambda r: (r["days_ago"], r["customer_id"]))
    rejected.sort(key=lambda r: (r["days_ago"], r["customer_id"]))

    # 同一个客户可能既关注了这条事件、又拒绝过其中某只产品。
    no_push = collections.defaultdict(list)
    for r in rejected:
        if r.get("suppress"):
            no_push[r["customer_id"]].append(r["ticker"])
    for w in watched:
        if no_push.get(w["customer_id"]):
            w["suppress_tickers"] = sorted(set(no_push[w["customer_id"]]))

    # ---- 4) 近期卖出
    sales = []
    if affected_tickers:
        q = ("SELECT customer_id, date, action, ticker, amount_cny, channel "
             "FROM customer_trade WHERE ticker IN (%s) ORDER BY customer_id, date"
             % ",".join("?" * len(affected_tickers)))
        for t in con.execute(q, sorted(affected_tickers)):
            cid, tk = t["customer_id"], t["ticker"]
            if "卖" not in (t["action"] or "") or tk in held[cid]:
                continue
            days = _days_between(t["date"], ref)
            if days is None or days < 0 or days > sold_days:
                continue
            sales.append({"customer_id": cid, "name": (info.get(cid) or {}).get("name"),
                          "risk_level": (info.get(cid) or {}).get("risk_level"),
                          "ticker": tk, "date": t["date"], "days_ago": days,
                          "amount_cny": t["amount_cny"], "channel": t["channel"],
                          "also_held": bool(set(held[cid]) & affected_tickers)})
    sales.sort(key=lambda r: (r["days_ago"], r["customer_id"]))

    return {"watched": watched, "pending_offer": pending,
            "rejected_offer": rejected, "recent_sale": sales}

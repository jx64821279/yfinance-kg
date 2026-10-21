# -*- coding: utf-8 -*-
"""客户级聚合：把「事件 → 客户」的名单翻转成「客户 → 相关事件与信号」。

输入
    一个时间窗口（默认最近 7 天）内的 KG 事件。

输出
    每个客户一条记录：
      - 持仓影响：事件 × 他持有的产品 × 贡献
      - 关注未持有 / offer 待定 / 已拒绝 / 近期卖出
      - 一个可解释的相关性排序和触发原因

不调用 LLM，不写数据库；只读 KG 和客户表。

用法
    python customer_actions.py
    python customer_actions.py --date 2026-09-29 --days 7
    python customer_actions.py --customer C006
    python customer_actions.py --out _yf_data/customer_actions.json
"""

import argparse
import collections
import json
import os
import sqlite3
import math
from datetime import date, timedelta

from customer_signals import affected_customers, affected_products, related_customers
from exposure_v2 import ExposureModel


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join("data", "kg_v3.db"))
    ap.add_argument("--date", default=None, help="参考日期；默认取 meta.price_as_of")
    ap.add_argument("--days", type=int, default=7, help="回看天数（默认 7）")
    ap.add_argument("--customer", default=None, help="只输出一个客户，例如 C006")
    ap.add_argument("--top-events", type=int, default=5, help="每个客户最多保留几条持仓事件")
    ap.add_argument("--interest-half-life", type=float, default=30.0,
                    help="关注行为的时间半衰期（默认 30 天）")
    ap.add_argument("--min-interest", type=float, default=0.15,
                    help="关注度低于这个值不列出（默认 0.15）")
    ap.add_argument("--sold-days", type=int, default=120,
                    help="卖出后多少天内算近期卖出（默认 120 天）")
    ap.add_argument("--out", default=None, help="写出 JSON（可选）")
    return ap.parse_args()


def ref_date(con, value):
    if value:
        return value[:10]
    r = con.execute("SELECT value FROM meta WHERE key='price_as_of'").fetchone()
    return (r[0] if r and r[0] else "")[:10]


def event_rows(con, ref, days):
    """窗口内的事件，按日期从新到旧。"""
    start = (date.fromisoformat(ref) - timedelta(days=days)).isoformat()
    out = []
    for r in con.execute("SELECT id, props FROM node WHERE type='Event'"):
        p = json.loads(r[1] or "{}")
        d = (p.get("pub_date") or "")[:10]
        if not d or d < start or d > ref:
            continue
        out.append({
            "id": r[0], "date": d, "title": p.get("news_title") or "",
            "category": p.get("event_category"), "scope": p.get("scope"),
            "polarity": p.get("polarity"), "driver": p.get("driver_category"),
            "concepts": p.get("concepts") or [], "asset_class": p.get("asset_class"),
            "channel": p.get("channel"), "link": p.get("link"),
        })
    out.sort(key=lambda e: (e["date"], e["id"]), reverse=True)
    return out


def load_customers(con):
    con.row_factory = sqlite3.Row
    info = {}
    for r in con.execute(
            "SELECT customer_id, name, risk_level, total_assets_cny, idle_cash_cny, "
            "time_deposit_cny FROM customer"):
        d = dict(r)
        d["portfolio_value_cny"] = (d["total_assets_cny"] - d["idle_cash_cny"]
                                    - d["time_deposit_cny"])
        info[d["customer_id"]] = d
    holdings = collections.defaultdict(dict)
    for r in con.execute("SELECT customer_id, ticker, weight_pct FROM customer_holding"):
        holdings[r["customer_id"]][r["ticker"]] = r["weight_pct"]
    return info, holdings


def priority_of(state):
    """优先级只做展示排序，不是概率。

    持仓影响取“最大一条 + 其余事件的一小部分”，避免同一客户被很多弱事件堆高；
    关注/待定/卖出只做小幅加分，保持它们在证据里可见，但不盖过真实持仓影响。
    """
    vals = sorted((abs(i["contribution"]) for i in state["impacts"]), reverse=True)
    max_impact = vals[0] if vals else 0.0
    rest = sum(vals[1:]) * 0.2
    watch = max((w["score"] for w in state["watched"]), default=0.0) * 0.2
    pending = 0.10 * min(len(state["pending"]), 2)
    sales = 0.02 * min(len(state["sales"]), 3)
    score = max_impact + rest + watch + pending + sales

    return round(score, 4)


def summarize(state, top_events):
    # 同一天、同概念集合、同方向的事件归成一组，避免同一主题的 5 条新闻
    # 在客户聚合结果里重复出现 5 次。每组保留贡献最大的一条作代表。
    groups = {}
    for i in state["impacts"]:
        key = (i["date"], i["category"], i["polarity"],
               tuple(sorted(i.get("concepts") or [])))
        cur = groups.get(key)
        if cur is None:
            groups[key] = dict(i, n_events=1, event_ids=[i["event_id"]])
        else:
            cur["n_events"] += 1
            if i["event_id"] not in cur["event_ids"] and len(cur["event_ids"]) < 5:
                cur["event_ids"].append(i["event_id"])
            if abs(i["contribution"]) > abs(cur["contribution"]):
                ids = cur["event_ids"]
                groups[key] = dict(i, n_events=cur["n_events"], event_ids=ids)

    impacts = sorted(groups.values(), key=lambda i: -abs(i["contribution"]))
    state["n_impacts_all"] = len(state["impacts"])
    state["n_clusters"] = len(impacts)
    state["net_contribution"] = round(sum(i["contribution"] for i in impacts), 6)
    state["max_abs_impact"] = round(max((abs(i["contribution"]) for i in impacts), default=0), 6)
    state["impacts"] = impacts[:top_events]

    # offer / 卖出按产品去重，保留最近的一条。
    state["pending"] = _dedupe_recent(state["pending"], "ticker")
    state["rejected"] = _dedupe_recent(state["rejected"], "ticker")
    state["sales"] = _dedupe_recent(state["sales"], "ticker")
    state["priority_score"] = priority_of(state)
    state["suppress_tickers"] = sorted({r["ticker"] for r in state["rejected"]})
    return state


def _dedupe_recent(items, key):
    best = {}
    for it in items:
        k = it.get(key)
        if k not in best or it.get("days_ago", 10**9) < best[k].get("days_ago", 10**9):
            best[k] = it
    return sorted(best.values(), key=lambda x: (x.get("days_ago", 10**9), x.get("ticker", "")))


def build(con, ref, days, top_events=5, interest_half_life=30.0,
          min_interest=0.15, sold_days=120):
    info, holdings = load_customers(con)
    state = {
        cid: {"customer_id": cid, "name": info[cid]["name"],
              "risk_level": info[cid]["risk_level"],
              "portfolio_value_cny": info[cid]["portfolio_value_cny"],
              "impacts": [], "watched": [], "pending": [], "rejected": [],
              "sales": []}
        for cid in info
    }

    model = ExposureModel.from_db(con)
    events = event_rows(con, ref, days)
    for ev in events:
        rows = affected_products(con, ev, in_graph=True, model=model)
        custs = affected_customers(con, rows, ev)
        direct_ids = {c["customer_id"] for c in custs}
        by_customer = {c["customer_id"]: c for c in custs}

        # 直接持仓影响
        for cid, hit in by_customer.items():
            state[cid]["impacts"].append({
                "event_id": ev["id"], "date": ev["date"], "title": ev["title"],
                "category": ev["category"], "polarity": ev["polarity"],
                "concepts": ev.get("concepts") or [], "driver": ev.get("driver"),
                "contribution": hit["contribution"],
                "products": hit["holdings"],
            })

        # 关注 / offer / 近期卖出
        rel = related_customers(con, rows, ev, direct_ids,
                                interest_half_life=interest_half_life,
                                min_interest=min_interest,
                                sold_days=sold_days)
        for c in rel["watched"]:
            state[c["customer_id"]]["watched"].append({
                "event_id": ev["id"], "date": ev["date"], "title": ev["title"],
                "score": c["score"], "evidence": c["evidence"],
                "items": c["items"], "suppress_tickers": c.get("suppress_tickers") or [],
            })
        for c in rel["pending_offer"]:
            state[c["customer_id"]]["pending"].append({
                "event_id": ev["id"], "date": ev["date"], "title": ev["title"],
                "ticker": c["ticker"], "days_ago": c["days_ago"],
                "reason": c["reason"], "also_held": c["also_held"],
            })
        for c in rel["rejected_offer"]:
            state[c["customer_id"]]["rejected"].append({
                "event_id": ev["id"], "date": ev["date"], "title": ev["title"],
                "ticker": c["ticker"], "days_ago": c["days_ago"],
                "reason": c["reason"], "also_held": c["also_held"],
            })
        for c in rel["recent_sale"]:
            state[c["customer_id"]]["sales"].append({
                "event_id": ev["id"], "date": ev["date"], "title": ev["title"],
                "ticker": c["ticker"], "days_ago": c["days_ago"],
                "channel": c["channel"], "also_held": c["also_held"],
            })

    out = [summarize(s, top_events) for s in state.values()]
    out.sort(key=lambda s: (-s["priority_score"], s["customer_id"]))
    # 优先级是本次运行内的相对排序，不是绝对风险等级。
    n_high = max(1, math.ceil(len(out) * 0.25))
    n_mid = max(n_high + 1, math.ceil(len(out) * 0.60))
    for i, s in enumerate(out):
        s["priority"] = "高" if i < n_high else ("中" if i < n_mid else "低")
    return {"meta": {"db": os.path.basename(os.path.abspath(con.execute("PRAGMA database_list").fetchone()[2])),
                     "ref_date": ref, "days": days, "n_events": len(events),
                     "n_customers": len(out),
                     "note": "客户级聚合；持仓影响使用 ExposureModel v3，关注/offer/卖出不并入持仓分数"},
            "customers": out}


def print_one(s, top=5):
    print("[%s] %-5s %-5s 优先级 %.4f  组合 %s"
          % (s["priority"], s["customer_id"], s["name"] or "", s["priority_score"],
             ("%s 元" % s["portfolio_value_cny"]) if s["portfolio_value_cny"] else "-"))
    print("  持仓影响 %d 条 → 归并 %d 组（展示 %d），净贡献 %+.4f，最大单条 %.4f"
          % (s["n_impacts_all"], s["n_clusters"], len(s["impacts"]),
             s["net_contribution"], s["max_abs_impact"]))
    for i in s["impacts"][:top]:
        extra = ("（同类 %d 条）" % i["n_events"]) if i.get("n_events", 1) > 1 else ""
        prods = "；".join("%s %d%%→%+.4f" % (p["ticker"], p["weight_pct"], p["contribution"])
                          for p in i["products"][:3])
        print("    %s %s %+0.4f%s  %s"
              % (i["date"], i["event_id"], i["contribution"], extra, prods))
        print("      %s" % (i["title"][:86] or "-"))
    if s["watched"]:
        w = s["watched"][0]
        tail = ("（勿推 %s）" % "、".join(w["suppress_tickers"])) if w["suppress_tickers"] else ""
        print("  关注：%s 关注度 %.3f%s" % (w["evidence"], w["score"], tail))
    if s["pending"]:
        p = s["pending"][0]
        print("  offer 待定：%s %d 天  %s" % (p["ticker"], p["days_ago"], p["reason"] or ""))
    if s["sales"]:
        q = s["sales"][0]
        print("  近期卖出：%s %d 天前" % (q["ticker"], q["days_ago"]))
    if s["suppress_tickers"]:
        print("  勿推：%s" % "、".join(s["suppress_tickers"]))


def main():
    args = parse_args()
    con = sqlite3.connect(args.db)
    ref = ref_date(con, args.date)
    if not ref:
        raise SystemExit("没有参考日期：请给 --date，或确认 meta.price_as_of 存在")
    data = build(con, ref, args.days, top_events=args.top_events,
                 interest_half_life=args.interest_half_life,
                 min_interest=args.min_interest, sold_days=args.sold_days)
    customers = data["customers"]
    if args.customer:
        customers = [s for s in customers if s["customer_id"] == args.customer]
        if not customers:
            raise SystemExit("没有这个客户：%s" % args.customer)
    print("[INFO] %s ~ %s，事件 %d 条，客户 %d 位"
          % (ref, (date.fromisoformat(ref) - timedelta(days=args.days)).isoformat(),
             data["meta"]["n_events"], len(customers)))
    for s in customers:
        print()
        print_one(s)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"meta": data["meta"], "customers": customers},
                      f, ensure_ascii=False, indent=1)
        print("\n结果已写出：%s" % args.out)


if __name__ == "__main__":
    main()

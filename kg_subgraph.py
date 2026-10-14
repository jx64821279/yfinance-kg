# -*- coding: utf-8 -*-
"""从知识图谱库里取「一个客户的子图」（检索层）。

这是客户简报的新取数方式：简报不再自己全量扫文件，而是拿这一份子图去算。

子图包含什么
    customer            客户主表（画像 / 资产 / 到期 / 客户经理）
    funds               他持有的产品 + 每只产品所属的概念
    exposure_rows       事件 × 他持有的产品 的曝光分（带事件的日期/类别/范围/通道/驱动）
    gaps                关注过但持仓没覆盖到的概念或资产类别
    selected_events     有界选取：每个概念只留 top-k 条（细粒度检索的"k"），
                        市场层面单独一批；正负两头对称取，避免只留同方向的信号

曝光度怎么来的
    库里**不存**分数（存了就会随参数漂移）。这里用 exposure_v2.py 现算：
    只装这个客户持有的产品（ExposureModel.from_db(fund_ids=…)），
    别人的产品根本不参与计算，所以"以客户为入口"是算出来的，不是筛出来的。

用法
    python kg_subgraph.py --db data/kg_v3.db --customer C009
    python kg_subgraph.py --db data/kg_v3.db --customer C009 --out _yf_data/sub_C009.json
"""

import argparse
import collections
import json
import os
import sqlite3
from datetime import datetime

from exposure_v2 import ExposureModel
from kg_common import SIGN, materiality, pct_change

parser = argparse.ArgumentParser()
parser.add_argument("--db", default=os.path.join("data", "kg_v3.db"))
parser.add_argument("--customer", default=None)
parser.add_argument("--all", action="store_true", help="给所有客户各出一份")
parser.add_argument("--out", default=None)
parser.add_argument("--outdir", default=None)
parser.add_argument("--top-events", type=int, default=3, help="每个概念保留几条代表事件")
parser.add_argument("--top-market", type=int, default=5, help="市场层面保留几条")
parser.add_argument("--pick", default="balanced", choices=["balanced", "strength"],
                    help="选取策略：balanced=正负两头各取一半；strength=按强度取前 k 条")
parser.add_argument("--max-events", type=int, default=60, help="子图里事件总数上限（0=不限）")
args = parser.parse_args()


def pick_events(items, k, strategy):
    """items: [(event_id, contribution)]，返回选中的若干条。

    balanced 的用意：只按强度取，容易全取到同方向的事件（比如清一色利好），
    提示词里就看不到另一面的信号了。
    """
    if k <= 0 or not items:
        return []
    if strategy == "strength":
        return sorted(items, key=lambda x: -abs(x[1]))[:k]
    pos = sorted([x for x in items if x[1] > 0], key=lambda x: -x[1])
    neg = sorted([x for x in items if x[1] < 0], key=lambda x: x[1])
    half = (k + 1) // 2
    out = pos[:half] + neg[:k - len(pos[:half])]
    if len(out) < k:                       # 某一侧不够，用另一侧补齐
        out += [x for x in (pos + neg) if x not in out][:k - len(out)]
    return out


def props_of(row):
    """sqlite3.Row 没有 .get()，这里单独兜一下。"""
    try:
        v = row["props"]
    except (KeyError, IndexError, TypeError):
        return {}
    return json.loads(v) if v else {}


def build_subgraph(con, customer_id, top_events=3):
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cust = cur.execute("SELECT * FROM customer WHERE customer_id=?", (customer_id,)).fetchone()
    if not cust:
        raise SystemExit("没有这个客户：%s" % customer_id)
    cust = dict(cust)

    # 持仓 + 每只产品所属的概念
    funds = []
    for r in cur.execute("""
        SELECT h.ticker, h.weight_pct, h.market_value_cny,
               f.id AS fund_id, f.name AS product_name,
               json_extract(f.props, '$.asset_class') AS asset_class,
               c.id AS concept_id, c.name AS concept_name
        FROM customer_holding h
        JOIN node f ON f.ticker = h.ticker AND f.type = 'Fund'
        LEFT JOIN edge e ON e.src = f.id AND e.rel = 'FOCUSES_ON'
        LEFT JOIN node c ON c.id = e.dst
        WHERE h.customer_id = ?
        ORDER BY h.weight_pct DESC
    """, (customer_id,)):
        funds.append(dict(r))
    tickers = [f["ticker"] for f in funds]

    # 时机信号要用的：App 行为 / 客户经理交流 / offer 历史
    behaviors = [dict(r) for r in cur.execute(
        """SELECT date, behavior AS behavior, target_type, target
           FROM customer_behavior WHERE customer_id=?""", (customer_id,))]
    advisors = [dict(r) for r in cur.execute(
        """SELECT date, channel, summary FROM customer_advisor WHERE customer_id=?""",
        (customer_id,))]
    offers = [dict(r) for r in cur.execute(
        """SELECT date, ticker, reason, status FROM customer_offer WHERE customer_id=?""",
        (customer_id,))]

    # 概念/产品这一小片图（候选产品、缺口判定要用；总共不到 200 条边，不用按客户裁剪）
    concept_members = collections.defaultdict(list)
    concept_anchor_funds = collections.defaultdict(list)
    fund_stocks = collections.defaultdict(dict)
    for r in cur.execute("SELECT src, dst FROM edge WHERE rel='INCLUDES'"):
        concept_members[r["src"]].append(r["dst"])
    for r in cur.execute("SELECT src, dst FROM edge WHERE rel='FOCUSES_ON'"):
        concept_anchor_funds[r["dst"]].append(r["src"])
    for r in cur.execute(
            "SELECT src, dst, json_extract(props,'$.weight') AS w FROM edge WHERE rel='CONSTITUENT'"):
        fund_stocks[r["src"]][r["dst"]] = r["w"] or 0.0
    concept_names = {r["id"]: r["name"] for r in cur.execute(
        "SELECT id, name FROM node WHERE type='Concept'")}
    # 全部产品（不只客户持有的）：候选产品要能推荐他没买过的
    funds_all = []
    for r in cur.execute("""
        SELECT f.id AS fund_id, f.ticker, f.name,
               json_extract(f.props,'$.asset_class') AS asset_class,
               c.id AS concept_id, c.name AS concept_name
        FROM node f
        LEFT JOIN edge e ON e.src = f.id AND e.rel = 'FOCUSES_ON'
        LEFT JOIN node c ON c.id = e.dst
        WHERE f.type = 'Fund' ORDER BY f.ticker"""):
        funds_all.append(dict(r))

    # 事件 × 他持有的产品 的曝光分：现算，且只装他持有的产品
    fund_ids = [f["fund_id"] for f in funds if f.get("fund_id")]
    rows = []
    if fund_ids:
        model = ExposureModel.from_db(con, fund_ids=fund_ids)
        held = set(fund_ids)
        events_meta = []
        for r in cur.execute("SELECT id, props FROM node WHERE type='Event'"):
            p = props_of(r)
            events_meta.append({
                "id": r["id"], "title": p.get("news_title"), "date": p.get("pub_date"),
                "category": p.get("event_category"), "scope": p.get("scope"),
                "channel": p.get("channel"), "polarity": p.get("polarity"),
                "driver": p.get("driver_category"), "asset_class": p.get("asset_class"),
                "publisher": p.get("news_source"), "link": p.get("link"),
            })
        for em in events_meta:
            for sc in model.score_event(em["id"], em["polarity"], funds=held):
                if not sc["score"]:
                    continue      # 中性事件不带方向，不进明细（和旧口径一致）
                rows.append({"event_id": em["id"], "fund_id": sc["fund"], "ticker": sc["ticker"],
                             "score": sc["score"], "title": em["title"], "date": em["date"],
                             "category": em["category"], "scope": em["scope"],
                             "channel": em["channel"], "polarity": em["polarity"],
                             "driver": em["driver"], "asset_class": em["asset_class"],
                             "publisher": em["publisher"], "link": em["link"]})

    # 关注过、但持仓没覆盖到的（需求缺口）
    gaps = []
    seen = set()
    # 先把外层结果取出来：下面要用同一个游标跑内层查询，边遍历边执行会把迭代冲掉
    behav_rows = list(cur.execute("""SELECT date, behavior, target_type, target
                                     FROM customer_behavior WHERE customer_id=?""", (customer_id,)))
    for b in behav_rows:
        # 口径与 customer_brief.find_gaps() 对齐：措辞一致、强度按行为类型分档
        strong = b["behavior"] in ("咨询客服", "service_chat")
        if b["target_type"] == "concept":
            hit = cur.execute("""
                SELECT 1 FROM customer_holding h
                JOIN edge e ON e.src = 'fund_' || h.ticker AND e.rel = 'FOCUSES_ON'
                JOIN node cn ON cn.id = e.dst AND cn.name = ?
                WHERE h.customer_id = ? LIMIT 1""", (b["target"], customer_id)).fetchone()
            key = ("concept", b["target"])
            evidence = "%s %s了「%s」" % (b["date"], b["behavior"], b["target"])
        elif b["target_type"] == "asset_class":
            hit = cur.execute("""
                SELECT 1 FROM customer_holding h
                JOIN node f ON f.id = 'fund_' || h.ticker
                WHERE h.customer_id = ?
                  AND json_extract(f.props, '$.asset_class') = ? LIMIT 1""",
                              (customer_id, b["target"])).fetchone()
            key = ("asset_class", b["target"])
            evidence = "%s %s了「%s」类资产" % (b["date"], b["behavior"], b["target"])
        else:
            continue
        if hit or key in seen:
            continue
        seen.add(key)
        gaps.append({"kind": key[0], "target": b["target"],
                     "evidence": evidence,
                     "strength": "strong" if strong else "normal"})

    # 市场层面的事件：落不到具体产品（没有分数），但落在资产类别上。
    # 简报的"市场层面"通道要用全量，选取也在这批里挑。
    market_events = []
    for r in cur.execute("""SELECT id, props FROM node
                            WHERE type='Event' AND json_extract(props,'$.channel')='market'"""):
        p = props_of(r)
        market_events.append({"event_id": r["id"], "date": p.get("pub_date"),
                              "polarity": p.get("polarity"), "driver": p.get("driver_category"),
                              "asset_class": p.get("asset_class"), "scope": p.get("scope"),
                              "category": p.get("event_category"), "title": p.get("news_title"),
                              "link": p.get("link")})

    # ---- 细粒度检索的"k"：每个概念只留 top-k 条事件 ----
    # 贡献度 = 分数 × 持仓权重 × 实质性权重（和决策层聚合用的是同一套）
    w_by_fund = {f["fund_id"]: (f["weight_pct"] or 0) for f in funds}
    name_by_fund = {f["fund_id"]: f["concept_name"] for f in funds}
    row_by_event = {}
    per_concept = collections.defaultdict(dict)
    for r in rows:
        row_by_event.setdefault(r["event_id"], r)
        cname = name_by_fund.get(r["fund_id"])
        if not cname:
            continue
        c = r["score"] * w_by_fund.get(r["fund_id"], 0) / 100.0 * materiality(r)
        per_concept[cname][r["event_id"]] = per_concept[cname].get(r["event_id"], 0.0) + c

    selected = {}
    for cname, evmap in per_concept.items():
        chosen = pick_events(list(evmap.items()), top_events, args.pick)
        selected[cname] = [{"event_id": e, "contribution": round(c, 3),
                            "date": row_by_event[e]["date"], "polarity": row_by_event[e]["polarity"],
                            "driver": row_by_event[e]["driver"],
                            "link": row_by_event[e].get("link"),
                            "title": (row_by_event[e]["title"] or "")[:90]} for e, c in chosen]

    # 市场层面：只留与这个客户资产类别相关的事件
    ac_weight = collections.defaultdict(float)
    for f in funds:
        ac_weight[f.get("asset_class")] += (f.get("weight_pct") or 0)
    mkt = []
    for r in market_events:
        w = ac_weight.get(r.get("asset_class"), 0.0)
        if w <= 0:
            continue
        v = (SIGN.get(r.get("polarity"), 0.0)
             * materiality({"category": r.get("category"), "scope": r.get("scope")})
             * w / 100.0)
        if abs(v) < 1e-9:
            continue
        mkt.append((r["event_id"], v))
    mkt_by_id = {r["event_id"]: r for r in market_events}
    selected_market = [{"event_id": e, "contribution": round(c, 3),
                        "date": mkt_by_id[e]["date"], "polarity": mkt_by_id[e]["polarity"],
                        "driver": mkt_by_id[e]["driver"], "asset_class": mkt_by_id[e]["asset_class"],
                        "link": mkt_by_id[e].get("link"),
                        "title": (mkt_by_id[e]["title"] or "")[:90]}
                       for e, c in pick_events(mkt, args.top_market, args.pick)]

    # 总数上限：超了就按强度裁掉最弱的
    if args.max_events:
        flat = [(abs(d["contribution"]), cname, d) for cname, items in selected.items() for d in items]
        flat += [(abs(d["contribution"]), "（市场层面）", d) for d in selected_market]
        if len(flat) > args.max_events:
            keep = {id(d) for _, _, d in sorted(flat, key=lambda x: -x[0])[:args.max_events]}
            selected = {k: [d for d in v if id(d) in keep] for k, v in selected.items()}
            selected = {k: v for k, v in selected.items() if v}
            selected_market = [d for d in selected_market if id(d) in keep]

    n_sel = sum(len(v) for v in selected.values()) + len(selected_market)
    sel_stats = {"n_candidates": len(rows), "n_selected": n_sel,
                 "n_unique_events": len({d["event_id"] for v in selected.values() for d in v}
                                        | {d["event_id"] for d in selected_market}),
                 "pick": args.pick, "top_per_concept": top_events,
                 "top_market": args.top_market, "max_events": args.max_events}

    # 持仓的日线价格（截至价格表的 as_of）：用于"持仓近一周表现"
    prices, price_as_of = {}, None
    for t in tickers:
        price_rows = [dict(r) for r in cur.execute(
            "SELECT date, close, adj_close, volume FROM price WHERE ticker=? ORDER BY date", (t,))]
        if not price_rows:
            continue
        price_as_of = price_as_of or price_rows[-1]["date"]
        prices[t] = {"last_date": price_rows[-1]["date"], "last_close": price_rows[-1]["close"],
                     "pct_1d": pct_change(price_rows, 1), "pct_5d": pct_change(price_rows, 5),
                     "window": price_rows}

    ref_date = cur.execute("""SELECT MAX(json_extract(props,'$.pub_date')) AS d
                              FROM node WHERE type='Event'""").fetchone()["d"]

    return {
        "meta": {"db": os.path.basename(args.db), "customer_id": customer_id,
                 "ref_date": ref_date, "n_exposure_rows": len(rows),
                 "exposure_mode": "按需现算（exposure_v2.ExposureModel，仅本客户持有的产品）",
                 "n_concepts": len({r["concept_name"] for r in funds if r["concept_name"]}),
                 "price_as_of": price_as_of, "n_price_tickers": len(prices),
                 "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        "customer": cust,
        "funds": funds,
        "exposure_rows": rows,
        "gaps": gaps,
        "selected_events": {"by_concept": selected, "market": selected_market, "stats": sel_stats},
        "market_events": market_events,
        "prices": prices,
        "behaviors": behaviors,
        "advisors": advisors,
        "offers": offers,
        "concept_slice": {
            "concept_names": concept_names,
            "concept_members": dict(concept_members),
            "concept_anchor_funds": dict(concept_anchor_funds),
            "fund_stocks": dict(fund_stocks),
            "funds_all": funds_all,
        },
    }


def main():
    con = sqlite3.connect(args.db)
    ids = []
    if args.customer:
        ids = [args.customer]
    elif args.all:
        ids = [r[0] for r in con.execute("SELECT customer_id FROM customer ORDER BY customer_id")]
    else:
        raise SystemExit("请指定 --customer C009 或 --all")

    for cid in ids:
        sub = build_subgraph(con, cid, args.top_events)
        path = None
        if args.out and len(ids) == 1:
            path = args.out
        elif args.outdir:
            os.makedirs(args.outdir, exist_ok=True)
            path = os.path.join(args.outdir, "%s.json" % cid)
        if path:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)   # --out 的父目录可能还不存在
            with open(path, "w", encoding="utf-8") as f:
                json.dump(sub, f, ensure_ascii=False, indent=1)

        if len(ids) == 1:
            m = sub["meta"]
            print("客户 %s %s" % (cid, sub["customer"]["name"]))
            print("  参考日 %s | 曝光明细 %d 行 | 涉及 %d 个概念"
                  % (m["ref_date"], m["n_exposure_rows"], m["n_concepts"]))
            print("  持仓 → 概念：")
            for f in sub["funds"]:
                print("    %-6s %-4s %2d%%  %s" % (f["ticker"], f["concept_name"] or "?",
                                                   f["weight_pct"], (f["product_name"] or "")[:40]))
            print("  需求缺口：%s" % ("；".join(g["target"] for g in sub["gaps"]) or "无"))
            st = sub["selected_events"]["stats"]
            print("  选取（策略 %s）：候选 %d 行 → 选中 %d 条（去重后 %d 条事件）"
                  % (st["pick"], st["n_candidates"], st["n_selected"], st["n_unique_events"]))
            for cname, evs in list(sub["selected_events"]["by_concept"].items())[:4]:
                print("    【%s】" % cname)
                for e in evs:
                    print("      %s %+6.3f %-20s %s" % (e["date"], e["contribution"],
                                                        (e["driver"] or "")[:20], (e["title"] or "")[:52]))
            if sub["selected_events"]["market"]:
                print("    【市场层面】")
                for e in sub["selected_events"]["market"][:3]:
                    print("      %s %+6.3f %-20s %s" % (e["date"], e["contribution"],
                                                        (e["driver"] or "")[:20], (e["title"] or "")[:52]))
            if path:
                print("  子图已写出：%s" % path)
    con.close()


if __name__ == "__main__":
    main()

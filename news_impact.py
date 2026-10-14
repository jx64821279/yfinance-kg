# -*- coding: utf-8 -*-
"""新闻 → 受影响的客户（事件入口，只算"直接影响"）。支持单条和批量。

方向
    和"以客户为入口"（客户 → 持仓 → 事件）相反，这里是**事件入口**：

        新闻（→ 事件）→ 受影响的产品 → 持有这些产品的客户

    "关注过这个概念但没持有"的客户**不算在内**（那是另一半）。
    客户不进图：产品 → 客户 这一步是拿 customer_holding 表 join 出来的。

两种入口
    1) 事件已经在 KG 里：   --event event_17aee2fa
    2) 新新闻：             --title / --date / --publisher / --url（单条）
                            或 --news <文件>（一批，文件里有多少条就处理多少条）
       走的是**同一条管线**，只是把批量语料换成你给的这几条：
         news_filter.py       判 scope + 去重（完全不沾边的直接丢掉，不进图谱）
         news_to_event_v2.py  抽事件（每条 1 次模型调用）
         classify_theme_v3.py 标「概念 × 驱动」（每 15 条 1 次模型调用）
       **默认直接入库**：事件落进 `_yf_data/events_v3_extra.json`，随后重建一次图与库
       （确定性的，约 1.5 秒，不吃模型）。想先看看影响、不写文件，加 --preview。
       已经在语料里的事件（链接相同）会被按 id 去重，重复加不会变成两条。

批量
    一次给 3 条和一次给 30 条走的是同一个流程：抽取逐条调模型，主题标注按 15 条一批，
    图与库**只重建一次**。为避免手滑把一个上千条的文件丢进来，条数超过 20 时会拦一下，
    要真跑就显式给 --limit N 或 --yes。

用法
    python news_impact.py --event event_17aee2fa
    python news_impact.py --title "Nvidia slides as US tightens chip export rules" \
        --date 2026-09-30 --publisher "Reuters" --api_key sk-...
    python news_impact.py --news _yf_data/news_batch.json --api_key sk-... [--preview] [--out x.json]
    python news_impact.py --event event_17aee2fa --out _yf_data/impact_event_17aee2fa.json
"""

import argparse
import collections
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

from event_ids import event_id_for
from exposure_v2 import ExposureModel
from kg_common import materiality

PY = sys.executable
EXTRA_EVENTS = os.path.join("_yf_data", "events_v3_extra.json")
BATCH_GUARD = 20          # 超过这个条数要显式 --limit 或 --yes
CLASSIFY_BATCH = 15       # classify_theme_v3.py 每批多少条

parser = argparse.ArgumentParser()
parser.add_argument("--event", default=None, help="事件 id（已经在 KG 里）")
parser.add_argument("--news", default=None,
                    help="一个新闻 JSON 文件：items 格式或纯数组；文件里几条就处理几条")
parser.add_argument("--title", default=None)
parser.add_argument("--date", default=None)
parser.add_argument("--publisher", default=None)
parser.add_argument("--url", default=None, help="新闻链接（事件 id 由它哈希而来）")
parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
parser.add_argument("--yes", action="store_true", help="条数超过 %d 时，确认要跑" % BATCH_GUARD)
parser.add_argument("--db", default=os.path.join("data", "kg_v3.db"))
parser.add_argument("--preview", action="store_true",
                    help="只算影响、不写任何文件（默认会把事件加进 KG 并重建图与库）")
# 旧用法：--commit 曾经是"入库"的开关，现在默认就入库，留个口子免得老命令报错
parser.add_argument("--commit", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--out", default=None, help="把结果写成 JSON")
parser.add_argument("--top", type=int, default=8,
                    help="单条时打印最多几条产品和客户（0=全部）；批量时每条新闻只打印前 3 位客户")
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
args = parser.parse_args()


def run_step(cmd):
    """新闻进图谱的每一步都调现成脚本，不另写一份口径。"""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit("这一步失败了：%s\n%s\n%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r.stdout or ""


def events_from_news(items):
    """一批新闻 → 事件列表（含 concepts / driver / _link）。返回 (事件列表, 被丢掉的条数)。"""
    if not args.api_key:
        raise SystemExit("走新新闻这条路需要 --api_key")
    raw = {"meta": {"fetched_at": "news_impact", "window": "given",
                    "note": "news_impact.py 送进来的新闻"},
           "wide": {"news_impact": items}}
    tmp = tempfile.mkdtemp(prefix="news_impact_")
    p_raw = os.path.join(tmp, "raw.json")
    p_filt = os.path.join(tmp, "filtered.json")
    p_rep = os.path.join(tmp, "report.json")
    p_drop = os.path.join(tmp, "dropped.json")
    p_ev = os.path.join(tmp, "events.json")
    p_assign = os.path.join(tmp, "assign.json")
    p_ev3 = os.path.join(tmp, "events_v3.json")
    with open(p_raw, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)

    # ① 判 scope + 去重：完全不沾边的新闻在这里就被挡掉，不进图谱
    # --dropped 必须指到临时目录：它的默认值会覆盖语料那一份丢弃明细
    run_step([PY, "news_filter.py", "--raw", p_raw, "--out", p_filt, "--report", p_rep,
              "--dropped", p_drop])
    kept = json.load(open(p_filt, encoding="utf-8")).get("items") or []
    if not kept:
        return [], len(items)

    # ② 抽事件 ③ 标概念 × 驱动
    cmd = [PY, "news_to_event_v2.py", "--news", p_filt, "--out", p_ev,
           "--api_key", args.api_key, "--api_base", args.api_base, "--model", args.model]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    run_step(cmd)
    run_step([PY, "classify_theme_v3.py", "--events", p_ev, "--out", p_assign,
              "--events-out", p_ev3, "--news", p_filt, "--batch", str(CLASSIFY_BATCH),
              "--api_key", args.api_key, "--api_base", args.api_base, "--model", args.model])
    events = json.load(open(p_ev3, encoding="utf-8"))
    out = []
    for ev in events:
        if "error" in ev:
            continue
        ev["id"] = event_id_for(ev)
        out.append(normalize_event(ev))
    return out, len(items) - len(out)


def event_from_db(con, eid):
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT id, props FROM node WHERE id=? AND type='Event'", (eid,)).fetchone()
    if not r:
        raise SystemExit("库里没有这条事件：%s（新新闻请用 --title/--news）" % eid)
    p = json.loads(r["props"] or "{}")
    return {"id": r["id"], "polarity": p.get("polarity"), "concepts": p.get("concepts") or [],
            "driver": p.get("driver_category"), "channel": p.get("channel"),
            "category": p.get("event_category"), "scope": p.get("scope"),
            "asset_class": p.get("asset_class"), "date": p.get("pub_date"),
            "title": p.get("news_title"), "publisher": p.get("news_source"),
            "link": p.get("link")}


def normalize_event(ev):
    """抽取出来的事件用的是 _title / _date / _scope 这套字段名，这里统一一下，
    并且补上 channel（图里由 build_kg_yf.py 判定，预览时按同样的规则推）。
    """
    ev = dict(ev)
    ev["title"] = ev.get("title") or ev.get("_title")
    ev["date"] = ev.get("date") or ev.get("_date")
    ev["publisher"] = ev.get("publisher") or ev.get("_publisher")
    ev["scope"] = ev.get("scope") or ev.get("_scope")
    ev["category"] = ev.get("category") or ev.get("event_category")
    if not ev.get("channel"):
        from build_kg_yf import MARKET_DRIVERS
        if ev.get("concepts"):
            ev["channel"] = "concept"
        elif ev.get("driver") in MARKET_DRIVERS:
            ev["channel"] = "market"
        else:
            ev["channel"] = "gap"
    return ev


def affected_products(con, ev, in_graph):
    """事件 → 受影响产品。在图里的事件用图上的边算；还没进图的用 concepts/点名标的算
    （两条路的分数经核对完全一致，所以"先预览、再入库"不会对不上数）。"""
    model = ExposureModel.from_db(con)
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


def commit_events(events):
    """把事件落进增量文件（按 id 去重），再重建一次图与库。"""
    data = []
    if os.path.exists(EXTRA_EVENTS):
        data = json.load(open(EXTRA_EVENTS, encoding="utf-8"))
    have = {event_id_for(e) for e in data}
    new = [e for e in events if event_id_for(e) not in have]
    data += new
    with open(EXTRA_EVENTS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("[落库] %d 条事件写入 %s（其中 %d 条之前已有，跳过；现共 %d 条增量事件）"
          % (len(new), EXTRA_EVENTS, len(events) - len(new), len(data)))
    run_step([PY, "build_kg_yf.py", "--suffix", "_v3"])
    run_step([PY, "build_kg_db.py", "--suffix", "_v3", "--quiet"])
    con = sqlite3.connect(args.db)
    n = con.execute("SELECT COUNT(*) FROM node WHERE id IN (%s)"
                    % ",".join("?" * len(events)), [e["id"] for e in events]).fetchone()[0]
    con.close()
    print("[落库] 图与库已重建，这 %d 条事件里 %d 条已在库里" % (len(events), n))


def load_items():
    """命令行上的这批新闻（--title 一条，或 --news 一个文件）。"""
    if not args.news:
        return [{"title": args.title, "link": args.url or "", "date": args.date,
                 "publisher": args.publisher or "", "channel": "wide", "query": "news_impact"}]
    d = json.load(open(args.news, encoding="utf-8"))
    items = d["items"] if isinstance(d, dict) and "items" in d else d
    out = []
    for it in items:
        out.append({"title": it.get("title"), "link": it.get("link") or "",
                    "date": it.get("date"), "publisher": it.get("publisher") or "",
                    "channel": it.get("channel") or "wide", "query": "news_impact"})
    return out


def print_detail(ev, rows, custs):
    """单条新闻的完整输出：产品（含打分路径）+ 客户（含贡献拆解）。"""
    print("事件 %s | %s | %s | %s | 驱动 %s"
          % (ev["id"], ev.get("date"), ev.get("polarity"), ev.get("category"), ev.get("driver")))
    print("  %s" % (ev.get("title") or "")[:100])
    print("  概念：%s   通道：%s   scope：%s"
          % ("、".join(ev.get("concepts") or []) or "（无落点）", ev.get("channel") or "-",
             ev.get("scope") or "-"))
    if ev.get("link"):
        print("  原文：%s" % ev["link"])
    print()

    cut = args.top or len(rows)
    print("受影响的产品 %d 只：" % len(rows))
    for r in rows[:cut]:
        paths = " + ".join(p["type"] + (("(%s)" % p.get("via")) if p.get("via") else "")
                           for p in r["paths"])
        print("  %-5s %+8.4f  %s" % (r["ticker"], r["score"], paths))
    if not rows:
        print("  （没有产品被这条事件打到：它落在市场层面 / 没有落点通道）")
    print()

    print("受影响客户 %d 位（只算持有上述产品的）：" % len(custs))
    for c in custs[:cut]:
        detail = "；".join("%s %d%%→%+.4f" % (d["ticker"], d["weight_pct"], d["contribution"])
                           for d in c["holdings"])
        print("  %-5s %-6s %-3s 贡献 %+8.4f   %s"
              % (c["customer_id"], c["name"] or "", c["risk_level"] or "", c["contribution"], detail))
    if not custs:
        print("  （没有客户持有受影响的产品）")


def print_summary(results):
    """多条新闻：一条新闻一行汇总 + 前 3 位客户。"""
    touched = set()
    for i, (ev, rows, custs) in enumerate(results, 1):
        touched |= {c["customer_id"] for c in custs}
        top_prod = "、".join("%s %+.2f" % (r["ticker"], r["score"]) for r in rows[:3]) or "（无产品）"
        top_cust = "｜".join("%s %+.4f" % (c["customer_id"], c["contribution"]) for c in custs[:3]) \
            or "（无客户）"
        print("[%d] %s  %s  %s/%s  概念 %s"
              % (i, ev["id"], ev.get("date") or "-", ev.get("category") or "-",
                 ev.get("driver") or "-", "、".join(ev.get("concepts") or []) or "（无落点）"))
        print("    %s" % (ev.get("title") or "")[:88])
        print("    产品 %d 只：%s" % (len(rows), top_prod))
        print("    客户 %d 位：%s" % (len(custs), top_cust))
    print("\n合计：%d 条新闻，波及 %d 位客户（去重）" % (len(results), len(touched)))


def main():
    wrote_files = False
    if args.event:
        con = sqlite3.connect(args.db)
        events = [event_from_db(con, args.event)]
        in_graph = True
    else:
        if not args.news and not (args.title and args.date):
            raise SystemExit("要给 --event，或者 --title + --date，或者 --news <文件>")
        items = load_items()
        if args.limit:
            items = items[:args.limit]
        if len(items) > BATCH_GUARD and not args.yes:
            raise SystemExit("这批有 %d 条，超过 %d 条先拦一下：确认要跑就加 --yes，"
                             "或者用 --limit N 只跑前 N 条" % (len(items), BATCH_GUARD))
        n_calls = len(items) + (len(items) + CLASSIFY_BATCH - 1) // CLASSIFY_BATCH
        print("[管线] %d 条新闻 → 判 scope → 抽事件 → 标概念/驱动（最多约 %d 次模型调用，"
              "被过滤挡掉的不花）"
              % (len(items), n_calls))
        events, dropped = events_from_news(items)
        if dropped:
            print("[管线] %d 条被过滤阶段丢掉（与产品货架无关 / 重复），不进图谱" % dropped)
        if not events:
            print("没有留下任何事件，结束。")
            return
        if args.preview:
            print("[预览] 只算影响，不写任何文件（要入库去掉 --preview）\n")
            in_graph = False
            con = sqlite3.connect(args.db)
        else:
            commit_events(events)
            con = sqlite3.connect(args.db)
            events = [event_from_db(con, e["id"]) for e in events]
            in_graph = True
            wrote_files = True

    results = []
    for ev in events:
        rows = affected_products(con, ev, in_graph)
        results.append((ev, rows, affected_customers(con, rows, ev)))

    if len(results) == 1:
        print()
        print_detail(*results[0])
    else:
        print()
        print_summary(results)

    print()
    if wrote_files:
        print("（已入库：%d 条事件写进 %s，图与库已重建）" % (len(results), EXTRA_EVENTS))
    elif args.preview:
        print("（预览：没有写任何文件；要入库去掉 --preview）")

    if args.out:
        json.dump({"meta": {"in_graph": in_graph, "db": os.path.basename(args.db),
                            "wrote_files": wrote_files, "n_events": len(results),
                            "note": "只算直接持有受影响产品的客户"},
                   "events": [{"event": ev, "products": rows, "customers": custs}
                              for ev, rows, custs in results]},
                  open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\n结果已写出：%s" % args.out)


if __name__ == "__main__":
    main()

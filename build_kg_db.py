# -*- coding: utf-8 -*-
"""把知识图谱与客户数据装进一个可查询的数据库（SQLite）。

为什么用 SQLite
    这一步要做的是"按关系筛选和聚合"（某客户持有什么、某条事件影响哪些产品、
    某概念的敞口落在哪些客户身上），不是图算法，所以不需要图数据库。
    SQLite 单文件、免安装、可直接用 DB Browser 打开；schema 用的是标准的
    节点表 + 边表，将来若要换 Neo4j，改连接层即可，数据不用重做。

    参考：FinKario 原项目并没有数据库，它用 LightRAG 的默认存储
    （图存 GraphML、向量存 nano-vectordb、其余存 JSON KV），而且是按周
    切成互不相通的快照。我们这张图是累积的，所以更适合放在库里查。

表结构
    知识层：node / edge（含若干查询用的视图）
    客户层：customer / customer_holding / customer_behavior
            / customer_advisor / customer_offer / customer_trade
    元信息：meta

不存什么
    **曝光度（事件 → 产品打分）不入库**。它是策略输出：系数一调、口径一改就要全量重算，
    而"库里存一份派生分数"迟早会和图里的持仓权重对不上。检索层（kg_subgraph.py）
    用到时用 exposure_v2.py 现算，只算这个客户持有的那几只产品。

用法
    python build_kg_db.py                 # 重建 data/kg.db
    python build_kg_db.py --db data/kg.db --kg src/kg/kg_yf_v2.json
"""

import argparse
import csv
import json
import os
import sqlite3
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--suffix", default="_v2", help="如 _v3：默认读 kg_yf_v3.json / exposure_v3.json，写 kg_v3.db")
parser.add_argument("--kg", default=None)
parser.add_argument("--prices", default=None)
parser.add_argument("--customers", default=os.path.join("data", "customer_demo", "customers_all_in_one.csv"))
parser.add_argument("--db", default=None)
parser.add_argument("--quiet", action="store_true")
args = parser.parse_args()

SUF = args.suffix or ""
args.kg = args.kg or os.path.join("src", "kg", "kg_yf%s.json" % SUF)
args.prices = args.prices or os.path.join("_yf_data", "prices%s.json" % SUF)
args.db = args.db or os.path.join("data", "kg%s.db" % SUF)


# ---------------------------------------------------------------- 建表

DDL = """
DROP TABLE IF EXISTS node;
DROP TABLE IF EXISTS edge;
CREATE TABLE node (
    id    TEXT PRIMARY KEY,
    type  TEXT NOT NULL,
    name  TEXT NOT NULL,
    ticker TEXT,               -- 产品/股票节点有代码，单独成列方便 join 与索引
    props TEXT                 -- JSON 字符串，存该类型特有的字段
);
CREATE INDEX idx_node_type ON node(type);
CREATE INDEX idx_node_name ON node(name);
CREATE INDEX idx_node_ticker ON node(ticker);

CREATE TABLE edge (
    src   TEXT NOT NULL,
    rel   TEXT NOT NULL,
    dst   TEXT NOT NULL,
    props TEXT,
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX idx_edge_src ON edge(src);
CREATE INDEX idx_edge_dst ON edge(dst);
CREATE INDEX idx_edge_rel ON edge(rel);

DROP TABLE IF EXISTS customer;
CREATE TABLE customer (
    customer_id       TEXT PRIMARY KEY,
    name              TEXT,
    age               INTEGER,
    gender            TEXT,
    city              TEXT,
    occupation        TEXT,
    annual_income_cny INTEGER,
    risk_level        TEXT,
    invest_years      INTEGER,
    total_assets_cny  INTEGER,
    idle_cash_cny     INTEGER,
    time_deposit_cny  INTEGER,
    deposit_maturity  TEXT,
    advisor_id        TEXT
);

DROP TABLE IF EXISTS customer_holding;
CREATE TABLE customer_holding (
    customer_id      TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    weight_pct       INTEGER,
    market_value_cny INTEGER,
    PRIMARY KEY (customer_id, ticker)
);
CREATE INDEX idx_holding_ticker ON customer_holding(ticker);

DROP TABLE IF EXISTS customer_behavior;
CREATE TABLE customer_behavior (
    customer_id TEXT NOT NULL,
    date        TEXT,
    behavior    TEXT,
    target_type TEXT,
    target      TEXT
);
CREATE INDEX idx_behavior_target ON customer_behavior(target);

DROP TABLE IF EXISTS customer_advisor;
CREATE TABLE customer_advisor (
    customer_id TEXT NOT NULL,
    advisor_id  TEXT,
    date        TEXT,
    channel     TEXT,
    summary     TEXT
);

DROP TABLE IF EXISTS customer_offer;
CREATE TABLE customer_offer (
    customer_id TEXT NOT NULL,
    advisor_id  TEXT,
    date        TEXT,
    ticker      TEXT,
    reason      TEXT,
    status      TEXT
);
CREATE INDEX idx_offer_ticker ON customer_offer(ticker);

DROP TABLE IF EXISTS customer_trade;
CREATE TABLE customer_trade (
    customer_id TEXT NOT NULL,
    date        TEXT,
    action      TEXT,
    ticker      TEXT,
    amount_cny  INTEGER,
    channel     TEXT
);

DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

-- 日线收盘价（截至 ref_date 的若干交易日）。时序数据留在表里，不做成图节点：
-- 查询需求是"取一段时间"，不是"找关系"，做成节点只会把图撑大。
DROP TABLE IF EXISTS price;
CREATE TABLE price (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,
    close     REAL,
    adj_close REAL,
    volume    REAL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX idx_price_ticker ON price(ticker);

-- 查询用的视图：把常用的多跳关系固定下来
DROP VIEW IF EXISTS v_fund_concept;
CREATE VIEW v_fund_concept AS
SELECT src AS fund_id, dst AS concept_id
FROM edge WHERE rel = 'FOCUSES_ON';

DROP VIEW IF EXISTS v_concept_member;
CREATE VIEW v_concept_member AS
SELECT src AS concept_id, dst AS stock_id
FROM edge WHERE rel = 'INCLUDES';

DROP VIEW IF EXISTS v_stock_fund;
CREATE VIEW v_stock_fund AS
SELECT dst AS stock_id, src AS fund_id, CAST(props AS TEXT) AS props
FROM edge WHERE rel = 'CONSTITUENT';

DROP VIEW IF EXISTS v_event_theme;
CREATE VIEW v_event_theme AS
SELECT src AS event_id, dst AS theme_id
FROM edge WHERE rel = 'HAS_THEME';

DROP VIEW IF EXISTS v_theme_concept;
CREATE VIEW v_theme_concept AS
SELECT src AS theme_id, dst AS concept_id
FROM edge WHERE rel = 'MAPS_TO';

DROP VIEW IF EXISTS v_event_affects;
CREATE VIEW v_event_affects AS
SELECT src AS event_id, dst AS target_id
FROM edge WHERE rel = 'AFFECTS';
"""


def split_items(cell):
    return [x for x in (cell or "").split(";") if x]


def load_customers(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            holdings = []
            for item in split_items(r["holdings"]):
                t, w = item.split(":")
                holdings.append((t, int(w.rstrip("%"))))
            behaviors = [tuple(x.split("|")) for x in split_items(r["app_events"])]
            advisors = [tuple(x.split("|")) for x in split_items(r["advisor_interactions"])]
            offers = [tuple(x.split("|")) for x in split_items(r["offers"])]
            trades = []
            for item in split_items(r["transactions"]):
                d, act, ticker, amount, channel = item.split("|")
                trades.append((d, act, ticker, int(amount), channel))
            rows.append({
                "customer": (r["customer_id"], r["name"], int(r["age"]), r["gender"], r["city"],
                             r["occupation"], int(r["annual_income_cny"]), r["risk_level"],
                             int(r["invest_years"]), int(r["total_assets_cny"]),
                             int(r["idle_cash_cny"]), int(r["time_deposit_cny"]),
                             r["deposit_maturity"], r["advisor_id"]),
                "holdings": holdings,
                "behaviors": behaviors,
                "advisors": advisors,
                "offers": offers,
                "trades": trades,
            })
    return rows


def main():
    kg = json.load(open(args.kg, encoding="utf-8"))
    customers = load_customers(args.customers)
    prices = {}
    if os.path.exists(args.prices):
        prices = json.load(open(args.prices, encoding="utf-8"))

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    if os.path.exists(args.db):
        os.remove(args.db)
    con = sqlite3.connect(args.db)
    cur = con.cursor()
    cur.executescript(DDL)

    # 知识层
    for e in kg["entities"]:
        props = {k: v for k, v in e.items() if k not in ("id", "type", "name")}
        cur.execute("INSERT INTO node(id, type, name, ticker, props) VALUES (?,?,?,?,?)",
                    (e["id"], e["type"], e.get("name", ""), e.get("ticker"),
                     json.dumps(props, ensure_ascii=False) if props else None))
    for ed in kg["edges"]:
        props = {k: v for k, v in ed.items() if k not in ("source", "relation", "target")}
        cur.execute("INSERT INTO edge(src, rel, dst, props) VALUES (?,?,?,?)",
                    (ed["source"], ed["relation"], ed["target"],
                     json.dumps(props, ensure_ascii=False) if props else None))

    # 客户层
    for c in customers:
        cur.execute("INSERT INTO customer VALUES (" + ",".join("?" * 14) + ")", c["customer"])
        cid = c["customer"][0]
        aid = c["customer"][13]                      # 客户经理编号在客户主表里
        cur.executemany("INSERT INTO customer_holding VALUES (?,?,?,NULL)",
                        [(cid, t, w) for t, w in c["holdings"]])
        cur.executemany("INSERT INTO customer_behavior VALUES (?,?,?,?,?)",
                        [(cid,) + b for b in c["behaviors"]])
        cur.executemany("INSERT INTO customer_advisor VALUES (?,?,?,?,?)",
                        [(cid, aid) + a for a in c["advisors"]])
        cur.executemany("INSERT INTO customer_offer VALUES (?,?,?,?,?,?)",
                        [(cid, aid) + o for o in c["offers"]])
        cur.executemany("INSERT INTO customer_trade VALUES (?,?,?,?,?,?)",
                        [(cid,) + t for t in c["trades"]])

    # 持仓市值：用客户表算出来的组合市值 × 权重
    cur.execute("""
        UPDATE customer_holding
        SET market_value_cny = CAST(ROUND(
            (SELECT total_assets_cny - idle_cash_cny - time_deposit_cny
             FROM customer WHERE customer.customer_id = customer_holding.customer_id)
            * weight_pct / 100.0) AS INTEGER)
    """)

    # 日线收盘价
    n_price = 0
    for ticker, v in (prices.get("tickers") or {}).items():
        for r in v.get("bars") or []:
            cur.execute("INSERT OR REPLACE INTO price VALUES (?,?,?,?,?)",
                        (ticker, r["date"], r.get("close"), r.get("adj_close"), r.get("volume")))
            n_price += 1

    meta = {
        "source_kg": os.path.basename(args.kg),
        "source_prices": os.path.basename(args.prices) if prices else "（无）",
        "source_customers": os.path.basename(args.customers),
        "price_as_of": (prices.get("meta") or {}).get("as_of") or "",
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_nodes": str(cur.execute("SELECT COUNT(*) FROM node").fetchone()[0]),
        "n_edges": str(cur.execute("SELECT COUNT(*) FROM edge").fetchone()[0]),
        "n_customers": str(cur.execute("SELECT COUNT(*) FROM customer").fetchone()[0]),
        "n_price": str(n_price),
    }
    cur.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()

    if not args.quiet:
        print("数据库：%s（%.2f MB）" % (args.db, os.path.getsize(args.db) / 1024 / 1024))
        for k, v in meta.items():
            print("  %-18s %s" % (k, v))
        print("\n各表行数：")
        for t in ("node", "edge", "customer", "customer_holding", "customer_behavior",
                  "customer_advisor", "customer_offer", "customer_trade",
                  "price"):
            print("  %-20s %d" % (t, cur.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]))
    con.close()


if __name__ == "__main__":
    main()

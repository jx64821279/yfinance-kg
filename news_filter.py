"""
新闻过滤与分级（不调用模型，纯规则）。

输入：_yf_data/news_raw_v2.json（yf_news_v2.py 的产物，含 wide / topic / entity 三个通道）
产出：_yf_data/news_filtered_v2.json   过滤后的新闻（每条带 scope 标签，供抽取使用）
      _yf_data/news_quality_report_v2.json  质量报告（保留/丢弃的原因与分布）

处理三件事：
  ① 时间窗：以语料里最新日期为基准，只保留最近 N 天（默认 30，不用"今天"，保证可复现）
  ② 相关性分级：用**与新闻无关的外部先验**判定 scope —— 实体清单（来自 yfinance）、
     产品定义词（来自 ETF 名称）、人工宏观词表、大盘综述词。
     刻意**不使用主题词表**，避免"用同一批新闻学出来的词去筛这批新闻"的循环。
  ③ 去重：标题精确去重（跨通道合并）＋ 同日近似重复（Jaccard ≥ 0.85）。
     加"同日"这个条件是必要的——v1 曾把不同日期的"Sector Update: Tech Stocks …"
     当成重复，那是误伤（它们是每日盘面播报，不是转载）。

保留策略：只有 scope=off（宽流里完全不沾边）才丢弃；macro / market 类新闻一律保留，
因为它们会通过资产类别与宽基概念传导到产品。

运行：python news_filter.py [--window 30] [--sim 0.85]
"""
import argparse
import collections
import datetime
import json
import os
import re

OUT_DIR = "_yf_data"
RAW_IN = os.path.join(OUT_DIR, "news_raw_v2.json")
FILTERED_OUT = os.path.join(OUT_DIR, "news_filtered_v2.json")
REPORT_OUT = os.path.join(OUT_DIR, "news_quality_report_v2.json")

parser = argparse.ArgumentParser()
parser.add_argument("--raw", default=RAW_IN)
parser.add_argument("--out", default=FILTERED_OUT)
parser.add_argument("--report", default=REPORT_OUT)
parser.add_argument("--window", type=int, default=30, help="时间窗（天），以语料最新日期为基准")
parser.add_argument("--sim", type=float, default=0.85, help="同日近似重复的 Jaccard 阈值")
args, _ = parser.parse_known_args()

# 大盘综述词（用于识别 market 类：反映市场情绪但不点名标的）
MARKET_WORDS = [
    "stock market", "s&p 500", "s&p500", "nasdaq", "dow jones", "dow ", "wall street",
    "equities", "equity market", "market wrap", "premarket", "pre-market",
    "market rally", "market selloff", "market outlook", "stocks to watch",
    "market today", "indexes", "indices", "blue chips",
    # 更一般的市场词：只影响"保留但不算实体/板块"的分类，不会误挂个股
    "stock", "share", "investor", "yield", "future", "rally", "selloff", "sell-off",
    "crash", "bubble", "mover", "equity", "index", "average", "correction",
    "valuation", "bull", "bear", "portfolio", "fund flow",
    # 注意：不要把 earnings 放进市场词——它既指"公司财报"（个股消息）也指"个人收入"，
    # 会同时误收"不在 universe 的公司财报"和"教育与就业收入统计"。这类交给未命中桶的
    # LLM 复核（见 README/方案），比继续堆词表可靠。
]
# 宏观 / 政策 / 地缘词
MACRO_WORDS = [
    "federal reserve", "fed ", "fomc", "interest rate", "rate cut", "rate hike",
    "inflation", "cpi", "ppi", "tariff", "sanction", "export control", "opec",
    "geopolitic", "central bank", "treasury yield", "bond yield", "recession",
    "gdp", "stimulus", "trade war", "election", "currency", "deficit", "sovereign debt",
    "trump", "white house", "congress", "senate", "midterm", "regulator",
    "regulation", "regulatory", "trade deal", "summit",
    "rate", "mortgage", "federal funds", "quantitative", "bank of england",
    "european central bank", "bank of japan", "pboc",
]
# 产品定义词（由 13 只 ETF 的名称与类别派生，人工确认过的短清单）
PRODUCT_WORDS = [
    "semiconductor", "artificial intelligence", "ai", "robotics", "china internet",
    "technology", "treasury", "bond", "gold", "oil", "dollar", "commodity",
    "index fund", "aggregate bond",
    # 半导体与 AI 产业链的常见说法
    "chip", "memory", "dram", "hbm", "foundry", "wafer", "fab", "gpu", "datacenter",
    "data center", "cloud", "openai", "anthropic", "nvidia",
    # 大宗与债券的常见说法
    "crude", "brent", "wti", "gasoline", "diesel", "opec", "precious metal",
    "copper", "wheat", "natural gas", "yield curve", "credit spread",
    # 区域词：我们的中国概念（中概互联网 / 中国科技）对应的区域
    "china", "chinese", "hong kong",
]
STOP = set("the a an and or of for to in on at by with from as is are was were its it this that "
           "after before over under more less than into out up down new says say will would can "
           "could may amid about on as at be been being".split())


def load_entity_terms():
    with open(os.path.join(OUT_DIR, "company_index.json"), encoding="utf-8") as f:
        index = json.load(f)
    with open(os.path.join(OUT_DIR, "yf_summary.json"), encoding="utf-8") as f:
        summ = json.load(f)
    terms = set()
    for info in index.values():
        name = str(info.get("name") or "")
        for a in [name] + list(info.get("aliases") or []) + list(info.get("tickers") or []):
            a = str(a or "").strip().lower()
            if len(a) >= 3:
                terms.add(a)
        head = re.split(r"[\s,\.]+", name.strip())[0].lower()   # Micron Technology -> micron
        if len(head) >= 3:
            terms.add(head)
    for sym, s in (summ.get("etfs") or {}).items():
        terms.add(sym.lower())
        nm = str(s.get("name") or "").strip().lower()
        if nm:
            terms.add(nm)
    return terms


def match_any(title, terms):
    """词表匹配：允许名词复数（stock market → stock markets、tariff → tariffs）。"""
    tl = " " + title.lower() + " "
    for t in terms:
        t = t.strip()
        if t and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?:s|es)?(?![a-z0-9])", tl):
            return t
    return None


# 标题开头常见的"非公司名"词：用来区分"公司名 + 财报动词"与"大盘叙述"
MARKET_LEAD = {"stock", "stocks", "share", "shares", "market", "markets", "the", "a", "an",
               "why", "what", "here", "is", "are", "these", "this", "how", "global", "us",
               "u.s.", "wall", "asian", "european", "asian", "chinese", "japanese", "india",
               "these", "those", "more", "less", "one", "two", "three", "five", "if", "when"}


def looks_like_company_story(title):
    """判断标题是否在讲"某家具体公司"的个股消息（财报/股价异动）。

    这类新闻如果公司不在 universe 里就是无关内容——它既不是大盘新闻，
    也不该被 stock / portfolio 这类泛词留在市场层。
    """
    t = title.strip()
    if re.search(r"\([A-Z]{1,5}\)", t):                       # (PANW) (DOCU) (M)
        return True
    m = re.match(r"([A-Za-z][\w&\.'\-]*)(?:'s)?\s+(?:next\s+|q[1-4]\s+)?"
                 r"(earnings|results|stock|shares|reports|reported|beats|misses|tops|posts|"
                 r"plunges|soars|slides|jumps|falls|rises|guidance|outlook)\b", t, re.I)
    return bool(m) and m.group(1).lower() not in MARKET_LEAD


def classify(title, entity_terms):
    """返回 (scope, 命中的词)。"""
    title = normalize(title)
    for scope, terms in (("entity", entity_terms), ("sector", PRODUCT_WORDS),
                         ("macro", MACRO_WORDS), ("market", MARKET_WORDS)):
        hit = match_any(title, terms)
        if hit:
            # 泛词命中的"个股消息"（且公司不在 universe）→ 归为无关
            if scope in ("market", "sector", "macro") and looks_like_company_story(title):
                return "off_universe", hit
            return scope, hit
    return None, None


def normalize(title):
    """把带点号的缩写与 HTML 实体归一，便于词表匹配（A.I. → ai）。"""
    t = str(title).lower().replace("&amp;", "&")
    t = re.sub(r"\ba\.\s*i\.", "ai", t)
    t = re.sub(r"\bu\.\s*s\.", "us", t)
    return t


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", normalize(title)) if w not in STOP}


def main():
    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)
    meta = raw.get("meta", {})

    # ---- 摊平三个通道 ----
    items = []
    for ch in ("wide", "topic", "entity"):
        for group, lst in (raw.get(ch) or {}).items():
            for x in lst:
                items.append({**x, "group": group})
    print(f"[INFO] 读入 {len(items)} 条（wide/topic/entity 三通道）")

    dates = sorted(x["date"] for x in items if x.get("date"))
    latest = dates[-1] if dates else datetime.date.today().isoformat()
    cutoff = (datetime.date.fromisoformat(latest) - datetime.timedelta(days=args.window)).isoformat()
    print(f"[INFO] 时间基准 {latest}，窗口 {args.window} 天，保留 {cutoff} 之后")

    entity_terms = load_entity_terms()

    # ---- 逐条打 scope + 时间窗 ----
    dropped_window, dropped_off, dropped_universe, kept = [], [], [], []
    for x in items:
        d = x.get("date")
        if d and d < cutoff:
            dropped_window.append(x)
            continue
        scope, hit = classify(x["title"], entity_terms)
        if scope is None:
            # 宽流/类目流里完全不沾边的 → 丢弃；逐 ticker 通道的 → 保留为 weak（正文可能有，只是标题没提）
            scope = "weak" if x.get("channel") == "entity" else "off"
        if scope in ("off", "off_universe"):
            (dropped_off if scope == "off" else dropped_universe).append(x)
            continue
        kept.append({**x, "scope": scope, "matched": hit})

    # ---- 去重：先按标题合并（跨通道/跨检索词），再做同日近似重复 ----
    by_title = {}
    for x in kept:
        k = x["title"].strip()
        if k not in by_title:
            by_title[k] = {**x, "channels": [x["channel"]], "queries": [x.get("group") or x.get("query")]}
        else:
            t = by_title[k]
            if x["channel"] not in t["channels"]:
                t["channels"].append(x["channel"])
            q = x.get("group") or x.get("query")
            if q and q not in t["queries"]:
                t["queries"].append(q)
            # 保留更强的 scope（entity > sector > macro > market > weak）
            order = {"entity": 0, "sector": 1, "macro": 2, "market": 3, "weak": 4}
            if order[x["scope"]] < order[t["scope"]]:
                t["scope"], t["matched"] = x["scope"], x["matched"]
    merged = list(by_title.values())

    # 同日近似重复
    merged.sort(key=lambda x: (x.get("date") or "", x["title"]))
    by_day = collections.defaultdict(list)
    for i, x in enumerate(merged):
        by_day[x.get("date")].append(i)
    parent = list(range(len(merged)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    dup_pairs = 0
    for day, idxs in by_day.items():
        if day is None or len(idxs) < 2:
            continue
        toks = {i: tokens(merged[i]["title"]) for i in idxs}
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                ti, tj = toks[i], toks[j]
                if len(ti) < 4 or len(tj) < 4:
                    continue
                inter = len(ti & tj)
                u = len(ti | tj)
                if u and inter / u >= args.sim:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[rb] = ra
                        dup_pairs += 1

    groups = collections.defaultdict(list)
    for i in range(len(merged)):
        groups[find(i)].append(i)
    final = []
    dup_removed = []
    for gi, g in enumerate(sorted(groups.values(), key=lambda g: merged[g[0]]["title"]), 1):
        head = merged[g[0]]
        if len(g) > 1:
            head = {**head, "dup_group": f"g{gi:04d}", "dup_size": len(g),
                    "dup_titles": [merged[i]["title"][:90] for i in g[1:6]]}
            dup_removed.extend(merged[i] for i in g[1:])
        final.append(head)

    final.sort(key=lambda x: (x.get("date") or "", x["title"]))
    for n, x in enumerate(final, 1):
        x["id"] = f"n{n:04d}"

    # ---- 输出 ----
    out = {
        "meta": {
            "source": os.path.basename(args.raw),
            "fetched_at": meta.get("fetched_at"),
            "window_days": args.window, "cutoff": cutoff, "latest_date": latest,
            "similarity_threshold": args.sim,
            "policy": "只有 scope=off（宽流里完全不沾边）才丢弃；macro/market 一律保留，"
                      "因为它们会经资产类别与宽基概念传导到产品",
            "scopes": {
                "entity": "命中 universe 公司/产品名", "sector": "命中产品定义词",
                "macro": "命中宏观/政策/地缘词", "market": "命中大盘综述词",
                "weak": "来自逐 ticker 通道但标题未命中，需靠原文给证据",
            },
        },
        "items": final,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    scope_cnt = collections.Counter(x["scope"] for x in final)
    ch_cnt = collections.Counter(c for x in final for c in x["channels"])
    scope_by_ch = collections.Counter((x["channels"][0], x["scope"]) for x in final)
    day_cnt = collections.Counter(x.get("date") for x in final)
    report = {
        "meta": out["meta"],
        "input": {"total": len(items), "by_channel": dict(collections.Counter(x["channel"] for x in items))},
        "kept": {"total": len(final), "by_scope": dict(scope_cnt), "by_channel": dict(ch_cnt),
                 "scope_by_channel": {f"{c}|{s}": n for (c, s), n in sorted(scope_by_ch.items())}},
        "dropped": {
            "out_of_window": len(dropped_window),
            "off_topic": len(dropped_off),
            "off_universe_company": len(dropped_universe),
            "duplicate_title": len(kept) - len(merged),
            "near_duplicate": len(dup_removed),
            "off_topic_examples": [x["title"][:90] for x in dropped_off[:10]],
            "off_universe_examples": [x["title"][:90] for x in dropped_universe[:10]],
            "out_of_window_examples": [f'{x.get("date")} {x["title"][:70]}' for x in dropped_window[:6]],
            "near_duplicate_examples": [{"keep": g["title"][:70], "dropped": g.get("dup_titles", [])[:2]}
                                        for g in final if g.get("dup_group")][:6],
        },
        "date_range": {"min": min(day_cnt) if day_cnt else None, "max": max(day_cnt) if day_cnt else None},
        "daily_counts_top": dict(day_cnt.most_common(10)),
    }
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 被丢弃的条目也落盘：未命中桶要交给 LLM 复核，其余留作审计
    dropped_path = os.path.join(OUT_DIR, "news_dropped_v2.json")
    with open(dropped_path, "w", encoding="utf-8") as f:
        json.dump({
            "out_of_window": dropped_window,
            "off_topic": dropped_off,
            "off_universe_company": dropped_universe,
            "near_duplicate": dup_removed,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n[INFO] 保留 {len(final)} 条 -> {args.out}")
    print(f"   scope 分布: {dict(scope_cnt)}")
    print(f"   通道分布: {dict(ch_cnt)}")
    print(f"   丢弃: 超时间窗 {len(dropped_window)} | 完全不沾边 {len(dropped_off)} "
          f"| 非 universe 个股 {len(dropped_universe)} "
          f"| 标题重复 {len(kept) - len(merged)} | 同日近似重复 {len(dup_removed)}")
    print(f"[INFO] 质量报告 -> {args.report}")
    print(f"[INFO] 丢弃明细 -> {dropped_path}")


if __name__ == "__main__":
    main()

"""
抓取 v2 新闻语料：以「时间窗 + 宽类目」的新闻流为主，实体检索作为补充通道。

通道设计（每条新闻都会标注 channel，便于分通道评估）：
  wide    —— 主通道。Google News 宽检索 + when:30d，回答"这一个月市场上发生了什么"
  topic   —— 增量通道。Google News 的 BUSINESS / TECHNOLOGY 类目订阅，只覆盖最近一两天
  entity  —— 补充通道。逐 ticker 的 Yahoo 财经搜索，回答"我关心的公司有没有消息"

与 v1 的差别：
  1) 不再使用 Yahoo RSS 头条流——v1 里它是旧新闻的主要来源（312 条里有 55 条超过 30 天）；
  2) 宽流通道不带实体词，避免"用已知实体检索来的新闻证明系统能发现影响"这种评估偏差；
  3) 所有条目标注 channel，后续可以分通道统计精度与覆盖。

运行：python yf_news_v2.py
产出：_yf_data/news_raw_v2.json    原始条目（按通道分组）
      _yf_data/news_v2_stats.json  分通道统计 + 相关性初判（纯规则，不调用模型）
"""
import datetime
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

OUT_DIR = "_yf_data"
RAW_OUT = os.path.join(OUT_DIR, "news_raw_v2.json")
STATS_OUT = os.path.join(OUT_DIR, "news_v2_stats.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 主通道：宽类目词（不含具体公司名），每个词配 30 天时间窗
WIDE_QUERIES = [
    "stock market",
    "Federal Reserve",
    "interest rates",
    "semiconductor",
    "artificial intelligence",
    "oil prices",
    "China stocks",
    "earnings",
    "mergers and acquisitions",
    "gold prices",
]
WINDOW = "when:30d"

# 增量通道：Google News 类目订阅（只覆盖最近一两天）
TOPIC_SECTIONS = ["BUSINESS", "TECHNOLOGY"]

# 补充通道：逐 ticker 的 Yahoo 财经搜索
ENTITY_TICKERS = ["NVDA", "TSM", "JD", "0981.HK", "000660.KS", "005930.KS",
                  "SMH", "SOXX", "AIQ", "KWEB", "CQQQ", "QQQ", "XLK",
                  "TLT", "AGG", "GLD", "USO", "UUP"]


def make_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "application/json,text/plain,*/*")]
    return op


def get(op, url, tries=3):
    last = ""
    for i in range(tries):
        try:
            with op.open(url, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read().decode("utf-8", "replace") if e.fp else "")
        except Exception as e:                     # 网络抖动重试
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
    return -1, last


def iso_date(raw):
    """把 pubDate / 时间戳统一成 YYYY-MM-DD。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) or str(raw).isdigit():
        try:
            return datetime.datetime.fromtimestamp(int(raw), datetime.timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None
    try:
        return parsedate_to_datetime(str(raw)).strftime("%Y-%m-%d")
    except Exception:
        return str(raw)[:16]


def parse_rss(body, channel, query):
    out = []
    for it in re.findall(r"<item>(.*?)</item>", body, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        title = (t.group(1).strip() if t else "")
        if not title:
            continue
        out.append({
            "title": title,
            "link": (l.group(1).strip() if l else ""),
            "date": iso_date(p.group(1).strip() if p else None),
            "publisher": (s.group(1).strip() if s else ""),
            "channel": channel,
            "query": query,
        })
    return out


def google_search(op, query, window=WINDOW):
    q = f"{query} {window}".strip()
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
           "&hl=en-US&gl=US&ceid=US:en")
    st, body = get(op, url)
    if st != 200:
        return []
    return parse_rss(body, "wide", q)


def google_topic(op, section):
    url = (f"https://news.google.com/rss/headlines/section/topic/{section}"
           "?hl=en-US&gl=US&ceid=US:en")
    st, body = get(op, url)
    if st != 200:
        return []
    return parse_rss(body, "topic", section)


def yahoo_search_news(op, sym, n=20):
    st, body = get(op, "https://query1.finance.yahoo.com/v1/finance/search"
                       f"?q={urllib.parse.quote(sym)}&newsCount={n}&quotesCount=0&enableFuzzyQuery=false")
    if st != 200:
        return []
    out = []
    for it in (json.loads(body).get("news") or []):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "link": it.get("link") or "",
            "date": iso_date(it.get("providerPublishTime")),
            "publisher": it.get("publisher") or "",
            "relatedTickers": it.get("relatedTickers") or [],
            "channel": "entity",
            "query": sym,
        })
    return out


# ---- 相关性初判：只用「与新闻无关的外部先验」，不借用主题词表 ----
def load_matchers():
    with open(os.path.join(OUT_DIR, "company_index.json"), encoding="utf-8") as f:
        index = json.load(f)
    with open(os.path.join(OUT_DIR, "yf_summary.json"), encoding="utf-8") as f:
        summ = json.load(f)

    entity_terms = set()
    for cid, info in index.items():
        name = str(info.get("name") or "")
        for a in [name] + list(info.get("aliases") or []) + list(info.get("tickers") or []):
            a = str(a or "").strip().lower()
            if len(a) >= 3:
                entity_terms.add(a)
        # 品牌首词：Micron Technology -> micron
        head = re.split(r"[\s,\.]+", name.strip())[0].lower()
        if len(head) >= 3:
            entity_terms.add(head)
    for sym, s in (summ.get("etfs") or {}).items():
        entity_terms.add(sym.lower())
        nm = str(s.get("name") or "").strip().lower()
        if nm:
            entity_terms.add(nm)

    # 产品定义词：由 ETF 名称与类别字段派生（人工确认过的短清单）
    product_words = [
        "semiconductor", "artificial intelligence", "robotics", "china internet",
        "technology", "treasury", "bond", "gold", "oil", "dollar", "commodity",
        "index fund", "aggregate bond",
    ]
    # 宏观/政治词：人工先验（这类新闻里既没有公司名也不含产品词，但确实是要的事件）
    macro_words = [
        "federal reserve", "fed ", "interest rate", "rate cut", "rate hike", "inflation",
        "cpi", "ppi", "tariff", "sanction", "export control", "opec", "geopolitic",
        "central bank", "treasury yield", "bond yield", "recession", "gdp", "stimulus",
        "trade war", "election", "currency", "deficit",
    ]
    return entity_terms, product_words, macro_words


def match_terms(title, terms):
    tl = " " + title.lower() + " "
    for t in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(t.strip()) + r"(?![a-z0-9])", tl):
            return t.strip()
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    op = make_opener()
    get(op, "https://fc.yahoo.com")          # 给 Yahoo 取 cookie
    fetched_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    data = {"meta": {"fetched_at": fetched_at, "window": WINDOW,
                     "wide_queries": WIDE_QUERIES, "topic_sections": TOPIC_SECTIONS,
                     "entity_tickers": ENTITY_TICKERS,
                     "note": "wide 为主通道（无实体词），entity 为补充通道；不含 Yahoo RSS"},
            "wide": {}, "topic": {}, "entity": {}}

    print("===== 主通道：Google News 宽检索 + 30 天窗口 =====")
    for q in WIDE_QUERIES:
        items = google_search(op, q)
        data["wide"][q] = items
        ds = sorted(x["date"] for x in items if x["date"])
        print(f"   {q:30} {len(items):3} 条 | {ds[0] if ds else '-'} ~ {ds[-1] if ds else '-'}")
        time.sleep(1.2)

    print("\n===== 增量通道：Google News 类目订阅 =====")
    for sec in TOPIC_SECTIONS:
        items = google_topic(op, sec)
        data["topic"][sec] = items
        ds = sorted(x["date"] for x in items if x["date"])
        print(f"   {sec:30} {len(items):3} 条 | {ds[0] if ds else '-'} ~ {ds[-1] if ds else '-'}")
        time.sleep(1.2)

    print("\n===== 补充通道：逐 ticker 的 Yahoo 搜索 =====")
    for sym in ENTITY_TICKERS:
        items = yahoo_search_news(op, sym, 20)
        data["entity"][sym] = items
        print(f"   {sym:10} {len(items):3} 条")
        time.sleep(1.0)

    with open(RAW_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] 原始语料 -> {RAW_OUT}")

    # ---- 分通道统计 + 相关性初判 ----
    entity_terms, product_words, macro_words = load_matchers()
    flat = {"wide": [], "topic": [], "entity": []}
    for ch in ("wide", "topic", "entity"):
        for group in data[ch].values():
            flat[ch].extend(group)

    stats = {"fetched_at": fetched_at, "window": WINDOW, "channels": {}}
    for ch, items in flat.items():
        seen, uniq = set(), []
        for x in items:
            if x["title"] not in seen:
                seen.add(x["title"])
                uniq.append(x)
        ds = sorted(x["date"] for x in uniq if x["date"])
        buckets = {"entity": 0, "product": 0, "macro": 0, "none": 0}
        samples = {"none": [], "macro": []}
        for x in uniq:
            if match_terms(x["title"], entity_terms): b = "entity"
            elif match_terms(x["title"], product_words): b = "product"
            elif match_terms(x["title"], macro_words): b = "macro"
            else: b = "none"
            buckets[b] += 1
            if b == "none" and len(samples["none"]) < 8: samples["none"].append(x["title"][:90])
            if b == "macro" and len(samples["macro"]) < 3: samples["macro"].append(x["title"][:90])
        stats["channels"][ch] = {
            "raw": len(items), "deduped": len(uniq),
            "duplicates": len(items) - len(uniq),
            "date_min": ds[0] if ds else None, "date_max": ds[-1] if ds else None,
            "matched": buckets, "unmatched_examples": samples["none"],
            "macro_examples": samples["macro"],
        }
        rel = buckets["entity"] + buckets["product"] + buckets["macro"]
        print(f"\n[{ch}] 原始 {len(items)} 条 → 去重 {len(uniq)} 条 "
              f"| 命中实体 {buckets['entity']} / 产品词 {buckets['product']} / 宏观词 {buckets['macro']} "
              f"| 都没命中 {buckets['none']}（相关率 {rel / max(len(uniq),1) * 100:.0f}%）")

    with open(STATS_OUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 统计 -> {STATS_OUT}")


if __name__ == "__main__":
    main()

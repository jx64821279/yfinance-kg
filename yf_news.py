"""
抓新闻（当作没有事件表时的唯一事件来源）。
来源：Yahoo search news API（标题/媒体/链接/时间/relatedTickers）
      + Yahoo RSS（标题/短摘要/时间/链接）
      + Google News RSS（标题/媒体/时间/链接）
运行：python yf_news.py
产出：_yf_data/news_raw.json
"""
import os
import json
import re
import time
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

TICKERS = ["NVDA", "TSM", "JD", "0981.HK", "000660.KS", "005930.KS",
           "SMH", "SOXX", "AIQ", "KWEB", "CQQQ", "QQQ", "XLK", "TLT", "AGG", "GLD", "USO", "UUP"]
OUT_DIR = "_yf_data"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def make_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Accept", "application/json,text/plain,*/*")]
    return op


def get(op, url):
    try:
        with op.open(url, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace") if e.fp else "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def yahoo_search_news(op, sym, n=20):
    st, body = get(op, f"https://query1.finance.yahoo.com/v1/finance/search"
                       f"?q={urllib.parse.quote(sym)}&newsCount={n}&quotesCount=0&enableFuzzyQuery=false")
    if st != 200:
        return []
    return json.loads(body).get("news", []) or []


def yahoo_rss(op, sym):
    st, body = get(op, f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={urllib.parse.quote(sym)}&region=US&lang=en-US")
    if st != 200:
        return []
    out = []
    for it in re.findall(r"<item>(.*?)</item>", body, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        d = re.search(r"<description>(.*?)</description>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        desc = re.sub(r"<[^>]+>", "", (d.group(1) if d else "")).strip()
        out.append({"title": (t.group(1).strip() if t else ""), "summary": desc,
                    "link": (l.group(1).strip() if l else ""), "pubDate": (p.group(1).strip() if p else "")})
    return out


def google_rss(op, query, n=30):
    u = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    st, body = get(op, u)
    if st != 200:
        return []
    out = []
    for it in re.findall(r"<item>(.*?)</item>", body, re.S)[:n]:
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        s = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        out.append({"title": (t.group(1).strip() if t else ""),
                    "link": (l.group(1).strip() if l else ""),
                    "pubDate": (p.group(1).strip() if p else ""),
                    "source": (s.group(1).strip() if s else "")})
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    op = make_opener()
    try:
        get(op, "https://fc.yahoo.com")
    except Exception:
        pass

    data = {"per_ticker": {}, "google_news": {}}
    print("===== Yahoo search news (标题/媒体/relatedTickers) =====")
    for sym in TICKERS:
        news = yahoo_search_news(op, sym, 20)
        rss = yahoo_rss(op, sym)
        data["per_ticker"][sym] = {"yahoo_news": news, "yahoo_rss": rss}
        rel = sorted({t for it in news for t in (it.get("relatedTickers") or [])})
        print(f"{sym:10} yahoo_news={len(news):2} yahoo_rss={len(rss):2} relatedTickers={rel[:8]}")
        time.sleep(1.0)

    print("\n===== Google News RSS（广覆盖）=====")
    for q in ["NVIDIA", "Taiwan Semiconductor", "SK Hynix Samsung", "China internet stocks"]:
        g = google_rss(op, q, 30)
        data["google_news"][q] = g
        print(f"{q:26} items={len(g)}  e.g. {g[0]['title'][:70] if g else ''}")
        time.sleep(1.0)

    with open(os.path.join(OUT_DIR, "news_raw.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] -> {OUT_DIR}/news_raw.json")

    # 打印一条完整样例
    nv = data["per_ticker"].get("NVDA", {})
    if nv.get("yahoo_news"):
        print("\n样例(NVDA yahoo_news[0]):")
        print(json.dumps(nv["yahoo_news"][0], ensure_ascii=False, indent=2))
    if nv.get("yahoo_rss"):
        print("样例(NVDA yahoo_rss[0]):")
        print(json.dumps(nv["yahoo_rss"][0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

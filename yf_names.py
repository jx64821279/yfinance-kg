"""
给 universe 里每个 ticker 拉统一的名称字段（price.longName / shortName / quoteType / exchange）。
来源：现有 _yf_data/yf_summary.json + events_extracted.json 里的 ticker，逐个补拉。
产出：_yf_data/yf_names.json   {ticker: {longName, shortName, quoteType, exchange, currency}}
运行：python yf_names.py
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

SUMMARY = "_yf_data/yf_summary.json"
EVENTS = "_yf_data/events_extracted.json"
OUT = "_yf_data/yf_names.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class Y:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA), ("Accept", "application/json,text/plain,*/*")]
        self.crumb = None

    def _get(self, url):
        try:
            with self.op.open(url, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, (e.read().decode("utf-8", "replace") if e.fp else "")
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    def seed(self):
        last = None
        for attempt in range(5):
            self._get("https://fc.yahoo.com")
            st, body = self._get("https://query1.finance.yahoo.com/v1/test/getcrumb")
            if st == 200 and body.strip() and "<html" not in body.lower():
                self.crumb = body.strip()
                return
            last = f"status={st} body={body[:120]!r}"
            print(f"  [seed retry {attempt+1}/5] {last}")
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"getcrumb failed: {last}")

    def name(self, sym):
        u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(sym)}"
             f"?modules=price&crumb={urllib.parse.quote(self.crumb)}")
        for a in range(4):
            st, body = self._get(u)
            if st == 200:
                pr = (json.loads(body).get("quoteSummary", {}).get("result") or [{}])[0].get("price", {})
                return {"longName": pr.get("longName"), "shortName": pr.get("shortName"),
                        "quoteType": pr.get("quoteType"), "exchange": pr.get("exchangeName"),
                        "currency": pr.get("currency")}
            if st == 401:
                self.seed()
            time.sleep(1.5 * (a + 1))
        return None


def collect_tickers():
    tickers = set()
    if os.path.exists(SUMMARY):
        s = json.load(open(SUMMARY, encoding="utf-8"))
        tickers.update(s.get("etfs", {}).keys())
        tickers.update(s.get("stocks", {}).keys())
        for v in s.get("etfs", {}).values():
            for h in (v.get("top_holdings") or []):
                if h.get("symbol"):
                    tickers.add(h["symbol"])
    if os.path.exists(EVENTS):
        for ev in json.load(open(EVENTS, encoding="utf-8")):
            tickers.update(ev.get("affected_tickers") or [])
    return sorted(t for t in tickers if t)


def main():
    y = Y()
    y.seed()
    tickers = collect_tickers()
    print(f"[INFO] tickers: {len(tickers)}")
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    for i, t in enumerate(tickers, 1):
        if t in out and out[t].get("longName"):
            continue
        r = y.name(t)
        if r:
            out[t] = r
        print(f"{i:3}/{len(tickers)} {t:10} {str((r or {}).get('longName'))[:40]}")
        time.sleep(0.7)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)
    got = sum(1 for v in out.values() if v.get("longName"))
    print(f"\n[INFO] got longName {got}/{len(tickers)} -> {OUT}")


if __name__ == "__main__":
    main()

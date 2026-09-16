"""
补拉公司业务描述（assetProfile）：longBusinessSummary / sector / industry / country 等。
用于后续「公司自述 → 业务主题 → 概念成员」的构建。
产出：_yf_data/yf_biz.json  {ticker: {longName, sector, industry, country, longBusinessSummary}}
运行：python yf_biz.py
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

NAMES = "_yf_data/yf_names.json"
OUT = "_yf_data/yf_biz.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class Y:
    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
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
        for a in range(5):
            self._get("https://fc.yahoo.com")
            st, body = self._get("https://query1.finance.yahoo.com/v1/test/getcrumb")
            if st == 200 and body.strip() and "<html" not in body.lower():
                self.crumb = body.strip()
                return
            print(f"  [seed retry {a+1}] {st} {body[:60]!r}")
            time.sleep(2 * (a + 1))
        raise RuntimeError("getcrumb failed")

    def profile(self, sym):
        u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(sym)}"
             f"?modules=assetProfile,price&crumb={urllib.parse.quote(self.crumb)}")
        for a in range(4):
            st, body = self._get(u)
            if st == 200:
                res = (json.loads(body).get("quoteSummary", {}).get("result") or [{}])[0]
                ap = res.get("assetProfile", {})
                pr = res.get("price", {})
                return {"longName": pr.get("longName") or pr.get("shortName"),
                        "sector": ap.get("sector"), "industry": ap.get("industry"),
                        "country": ap.get("country"), "website": ap.get("website"),
                        "longBusinessSummary": ap.get("longBusinessSummary")}
            if st == 401:
                self.seed()
            time.sleep(1.5 * (a + 1))
        return None


def main():
    names = json.load(open(NAMES, encoding="utf-8"))
    tickers = [t for t, v in names.items() if (v.get("quoteType") or "").upper() == "EQUITY"]
    print(f"[INFO] EQUITY tickers: {len(tickers)}")
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    y = Y()
    y.seed()
    for i, t in enumerate(tickers, 1):
        if out.get(t, {}).get("longBusinessSummary"):
            continue
        r = y.profile(t)
        if r:
            out[t] = r
        s = (r or {}).get("longBusinessSummary") or ""
        print(f"{i:3}/{len(tickers)} {t:10} {str((r or {}).get('industry'))[:26]:26} summary={len(s)} 字")
        time.sleep(0.7)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)
    got = sum(1 for v in out.values() if v.get("longBusinessSummary"))
    print(f"\n[INFO] 有业务描述 {got}/{len(tickers)} -> {OUT}")


if __name__ == "__main__":
    main()

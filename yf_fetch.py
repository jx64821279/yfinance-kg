"""
直接调用 Yahoo Finance API（绕过 yfinance，避免其被限流的问题）拉数据。
基金层：fundProfile + topHoldings（持仓 + 各资产类别仓位 + 行业分布）
股票层：assetProfile(sector/industry) + price(市值/交易所/币种)
运行：python yf_fetch.py
产出：_yf_data/yf_raw.json  （逐 ticker 原始 JSON）
      _yf_data/yf_summary.json（精简摘要）
"""
import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

ETFS = ["SMH", "SOXX", "AIQ", "BOTZ", "KWEB", "CQQQ", "QQQ", "XLK", "TLT", "AGG", "GLD", "USO", "UUP"]
STOCKS = ["JD", "NVDA", "TSM", "0981.HK", "000660.KS", "005930.KS"]
OUT_DIR = "_yf_data"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class Yahoo:
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

    def seed(self):
        self._get("https://fc.yahoo.com")            # 拿 A3 cookie
        st, body = self._get("https://query1.finance.yahoo.com/v1/test/getcrumb")
        if st != 200:
            raise RuntimeError(f"getcrumb failed: {st} {body[:100]}")
        self.crumb = body.strip()
        return self.crumb

    def quote_summary(self, sym, modules):
        u = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(sym)}"
             f"?modules={modules}&crumb={urllib.parse.quote(self.crumb)}")
        for attempt in range(4):
            st, body = self._get(u)
            if st == 200:
                return json.loads(body).get("quoteSummary", {}).get("result", [{}])[0]
            if st == 401:               # crumb 过期，重取
                self.seed()
            time.sleep(1.5 * (attempt + 1))
        return None


def raw_val(v):
    return v.get("raw") if isinstance(v, dict) else v


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    y = Yahoo()
    print("crumb:", y.seed())
    raw_all, summary = {}, {"etfs": {}, "stocks": {}}

    print("\n===== 基金 / ETF 层 =====")
    for sym in ETFS:
        res = y.quote_summary(sym, "fundProfile,topHoldings,defaultKeyStatistics,price")
        raw_all[sym] = res
        if not res:
            print(f"{sym:8} <FAILED>")
            continue
        fp = res.get("fundProfile", {})
        th = res.get("topHoldings", {})
        pr = res.get("price", {})
        holds = th.get("holdings", []) or []
        summary["etfs"][sym] = {
            "name": raw_val(pr.get("longName")),
            "family": fp.get("family"),
            "category": fp.get("categoryName"),
            "legalType": fp.get("legalType"),
            "currency": raw_val(pr.get("currency")),
            "positions": {k: raw_val(th.get(k)) for k in
                          ("stockPosition", "bondPosition", "cashPosition", "otherPosition",
                           "preferredPosition", "convertiblePosition")},
            "n_holdings": len(holds),
            "top_holdings": [{"symbol": h.get("symbol"), "name": h.get("holdingName"),
                              "weight": raw_val(h.get("holdingPercent"))} for h in holds],
            "sector_weightings": th.get("sectorWeightings"),
        }
        pos = summary["etfs"][sym]["positions"]
        print(f"{sym:8} {str(summary['etfs'][sym]['name'])[:26]:26} cat={str(fp.get('categoryName'))[:18]:18} "
              f"stock={pos['stockPosition']} bond={pos['bondPosition']} cash={pos['cashPosition']} "
              f"other={pos['otherPosition']} | holdings={len(holds)}")
        time.sleep(1.2)

    print("\n===== 股票 / 公司层 =====")
    for sym in STOCKS:
        res = y.quote_summary(sym, "assetProfile,price,defaultKeyStatistics,summaryDetail")
        raw_all[sym] = res
        if not res:
            print(f"{sym:10} <FAILED>")
            continue
        ap = res.get("assetProfile", {})
        pr = res.get("price", {})
        summary["stocks"][sym] = {
            "name": raw_val(pr.get("longName")) or raw_val(pr.get("shortName")),
            "sector": ap.get("sector"),
            "industry": ap.get("industry"),
            "country": ap.get("country"),
            "exchange": raw_val(pr.get("exchangeName")),
            "currency": raw_val(pr.get("currency")),
            "marketCap": raw_val(pr.get("marketCap")),
        }
        s = summary["stocks"][sym]
        print(f"{sym:10} {str(s['name'])[:24]:24} sec={str(s['sector'])[:18]:18} "
              f"ind={str(s['industry'])[:26]:26} exch={s['exchange']} ccy={s['currency']}")
        time.sleep(1.2)

    with open(os.path.join(OUT_DIR, "yf_raw.json"), "w", encoding="utf-8") as f:
        json.dump(raw_all, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "yf_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] -> {OUT_DIR}/yf_raw.json, {OUT_DIR}/yf_summary.json")


if __name__ == "__main__":
    main()

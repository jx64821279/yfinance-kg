"""
用 SEC EDGAR 补公司官方行业分类（SIC）。
ticker -> CIK -> submissions.json -> sic / sicDescription / entityName
产出：_yf_data/edgar_sic.json  {ticker: {cik, entityName, sic, sicDescription}}
运行：python edgar_sic.py
"""
import os
import json
import time
import urllib.request
import urllib.error

NAMES = "_yf_data/yf_names.json"
OUT = "_yf_data/edgar_sic.json"
UA = "finkario-research/0.1 (academic research; contact: user@example.com)"


def get(u, timeout=30):
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main():
    names = json.load(open(NAMES, encoding="utf-8"))
    tickers = sorted(names)

    st, body = get("https://www.sec.gov/files/company_tickers.json")
    if st != 200:
        raise SystemExit(f"EDGAR ticker list failed: {st}")
    tk2cik = {}
    for v in json.loads(body).values():
        tk2cik.setdefault(v["ticker"].upper(), v["cik_str"])
    print(f"[INFO] EDGAR tickers: {len(tk2cik)}")

    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    hit, miss = 0, []
    for i, t in enumerate(tickers, 1):
        key = t.upper()
        if key in out:
            hit += 1
            continue
        cik = tk2cik.get(key)
        if not cik:
            miss.append(t)
            continue
        st, body = get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
        if st == 200:
            d = json.loads(body)
            out[t] = {"cik": cik, "entityName": d.get("name"), "sic": d.get("sic"),
                      "sicDescription": d.get("sicDescription")}
            hit += 1
            print(f"{i:3}/{len(tickers)} {t:10} CIK{cik:>8} {d.get('sic')} {str(d.get('sicDescription'))[:44]}")
        else:
            miss.append(t)
        time.sleep(0.35)          # SEC 建议 <10 req/s
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)
    print(f"\n[INFO] 命中 {hit}/{len(tickers)} | 无 EDGAR 记录 {len(miss)}: {miss[:12]}")
    print(f"[INFO] -> {OUT}")


if __name__ == "__main__":
    main()

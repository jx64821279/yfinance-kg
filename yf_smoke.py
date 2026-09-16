"""
yfinance 数据探测：先看能拿到哪些字段（不建图）。
universe = 美股主题 ETF（基金层）+ 事件里的全球科技公司（股票层）。
运行：python yf_smoke.py
产出：_yf_data/yf_smoke_report.json
"""
import os
import json
import time
import yfinance as yf

ETFS = ["SMH", "SOXX", "AIQ", "BOTZ", "KWEB", "CQQQ", "QQQ", "XLK", "TLT", "AGG", "GLD", "USO", "UUP"]
STOCKS = ["JD", "NVDA", "TSM", "0981.HK", "000660.KS", "005930.KS"]

OUT_DIR = "_yf_data"


def get_info(t):
    for fn in ("get_info", "info"):
        try:
            v = getattr(t, fn)
            v = v() if callable(v) else v
            if v:
                return v
        except Exception as e:
            last = f"{fn}: {e}"
    return {}


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    report = {"etfs": {}, "stocks": {}}

    print("=" * 70)
    print("ETF / 基金层")
    print("=" * 70)
    for sym in ETFS:
        t = yf.Ticker(sym)
        row = {"info_keys": [], "funds_data": False, "top_holdings": None, "asset_classes": None,
               "sector_weightings": None, "name": None, "category": None, "error": None}
        try:
            info = get_info(t)
            row["info_keys"] = sorted(info.keys())[:0]  # 只记数量
            row["info_key_count"] = len(info)
            row["name"] = info.get("longName") or info.get("shortName")
            row["category"] = info.get("category")
            fd = getattr(t, "funds_data", None)
            if fd is not None:
                row["funds_data"] = True
                th = getattr(fd, "top_holdings", None)
                if th is not None:
                    try:
                        row["top_holdings"] = {"n": int(len(th)), "cols": list(map(str, th.columns)),
                                               "top3": th.head(3).to_dict("records")}
                    except Exception as e:
                        row["top_holdings"] = {"err": str(e)}
                ac = getattr(fd, "asset_classes", None)
                if ac is not None:
                    try:
                        row["asset_classes"] = dict(ac) if not hasattr(ac, "to_dict") else ac.to_dict()
                    except Exception as e:
                        row["asset_classes"] = {"err": str(e)}
                sw = getattr(fd, "sector_weightings", None)
                if sw is not None:
                    try:
                        row["sector_weightings"] = dict(sw) if not hasattr(sw, "to_dict") else sw.to_dict()
                    except Exception as e:
                        row["sector_weightings"] = {"err": str(e)}
        except Exception as e:
            row["error"] = str(e)
        report["etfs"][sym] = row
        n = row.get("top_holdings", {})
        n = n.get("n") if isinstance(n, dict) else None
        print(f"{sym:8} name={str(row['name'])[:28]:28} cat={str(row['category'])[:18]:18} "
              f"funds_data={row['funds_data']} holdings={n} asset_classes={bool(row['asset_classes'])}")
        time.sleep(0.6)

    print()
    print("=" * 70)
    print("股票 / 公司层")
    print("=" * 70)
    for sym in STOCKS:
        t = yf.Ticker(sym)
        row = {}
        try:
            info = get_info(t)
            for k in ("shortName", "longName", "quoteType", "sector", "industry", "marketCap",
                      "currency", "exchange", "fullExchangeName", "country"):
                row[k] = info.get(k)
            row["info_key_count"] = len(info)
        except Exception as e:
            row["error"] = str(e)
        report["stocks"][sym] = row
        print(f"{sym:10} {str(row.get('shortName'))[:22]:22} sec={str(row.get('sector'))[:18]:18} "
              f"ind={str(row.get('industry'))[:22]:22} exch={row.get('fullExchangeName')}")
        time.sleep(0.6)

    with open(os.path.join(OUT_DIR, "yf_smoke_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[INFO] report -> {os.path.join(OUT_DIR, 'yf_smoke_report.json')}")


if __name__ == "__main__":
    main()

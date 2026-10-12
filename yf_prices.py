# -*- coding: utf-8 -*-
"""抓 13 只 ETF 的日线收盘价（供客户简报的"持仓近一周表现"用）。

两个口径要点
    1. **时间锚点**：整条链是锚在 ref_date（数据里最新的一天，2026-09-29）上的，
       所以价格要取"截至 ref_date 的那一周"，而不是"今天起的前一周"——
       否则拿到的价格和新闻窗口对不上，做决策就是错的。因此这里用 chart 接口
       指定日期区间，再截到 as_of 为止。
    2. **复权价**：算涨跌幅必须用复权价，否则除权日会假跌。

这 13 只 ETF 都在美股市场，交易日历统一，所以不需要处理跨市场假期。

运行：python yf_prices.py                    # 默认 as_of=2026-09-29
产出：_yf_data/prices_v3.json
"""

import argparse
import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from yf_fetch import ETFS, Yahoo
from kg_common import pct_change

parser = argparse.ArgumentParser()
parser.add_argument("--as-of", default="2026-09-29", help="把价格截到这一天为止（口径锚点）")
parser.add_argument("--days", type=int, default=5, help="窗口长度（交易日）")
parser.add_argument("--lookback", type=int, default=30, help="往前多抓几个日历日，保证交易日够数")
parser.add_argument("--tickers", default=",".join(ETFS))
parser.add_argument("--out", default=os.path.join("_yf_data", "prices_v3.json"))
args = parser.parse_args()


def to_date(ts):
    """Yahoo 给的是 UTC 时间戳，日线的日期直接取 UTC 日期即可。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    y = Yahoo()
    print("crumb:", y.seed())
    as_of = args.as_of
    p1 = int(datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp()) - args.lookback * 86400
    p2 = int(datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc).timestamp()) + 3 * 86400

    out = {"meta": {"as_of": as_of, "window_days": args.days, "source": "yahoo chart v8",
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "tickers_ok": 0, "tickers_failed": []},
           "tickers": {}}

    for i, sym in enumerate([t.strip() for t in args.tickers.split(",") if t.strip()], 1):
        u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
             "?period1=%d&period2=%d&interval=1d&events=div%%2Csplit&crumb=%s"
             % (urllib.parse.quote(sym), p1, p2, urllib.parse.quote(y.crumb or "")))
        res, err = None, None
        for attempt in range(3):
            st, body = y._get(u)
            if st == 200:
                try:
                    js = json.loads(body)
                    r = (js.get("chart") or {}).get("result") or []
                    res = r[0] if r else None
                    if res is None:
                        err = str((js.get("chart") or {}).get("error"))
                except Exception as e:
                    err = str(e)
                break
            err = "HTTP %s" % st
            time.sleep(1.5 * (attempt + 1))

        if not res:
            out["meta"]["tickers_failed"].append({"ticker": sym, "error": err})
            print("%-6s <失败> %s" % (sym, err))
            continue

        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
        rows = []
        for k, t in enumerate(ts):
            d = to_date(t)
            if d > as_of:                      # 只取到口径锚点为止
                continue
            close = (q.get("close") or [None])[k] if k < len(q.get("close") or []) else None
            if close is None:
                continue
            rows.append({"date": d, "close": round(close, 4),
                         "adj_close": round(adj[k], 4) if k < len(adj) and adj[k] is not None else None,
                         "volume": (q.get("volume") or [None])[k] if k < len(q.get("volume") or []) else None})
        if not rows:
            out["meta"]["tickers_failed"].append({"ticker": sym, "error": "没有可用交易日"})
            print("%-6s <失败> 没有可用交易日" % sym)
            continue

        n = args.days
        pct_1d = pct_change(rows, 1)
        pct_nd = pct_change(rows, n)
        # 存 n+5 天：算 n 日涨跌幅需要 n+1 个点，多留几天方便以后换窗口
        out["tickers"][sym] = {
            "bars": rows[-(n + 5):],
            "last_date": rows[-1]["date"],
            "last_close": rows[-1]["close"],
            "pct_1d": pct_1d,
            "pct_%dd" % n: pct_nd,
            "n_bars": len(rows),
        }
        out["meta"]["tickers_ok"] += 1
        print("%-6s 至 %s 收盘 %8.2f | 1日 %+6.2f%% | %d日 %+6.2f%% | %d 个交易日"
              % (sym, rows[-1]["date"], rows[-1]["close"], (pct_1d or 0) * 100,
                 n, (pct_nd or 0) * 100, len(rows)))
        time.sleep(0.8)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n[INFO] 成功 %d 只，失败 %d 只 -> %s"
          % (out["meta"]["tickers_ok"], len(out["meta"]["tickers_failed"]), args.out))


if __name__ == "__main__":
    main()

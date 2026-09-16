"""
构建概念层「骨架」：
  ① 可投资概念  ← 主题 ETF 反推（成员=ETF 持仓，天然连得到基金）
  ② 行业层      ← EDGAR SIC(官方) + yfinance industry
产出：_yf_data/concepts.json   {concepts:{key:{name,anchor_funds[],members[],n_members}}, industries:{...}}
运行：python build_concepts.py
"""
import os
import json
from collections import defaultdict

import company_norm as co

SUMMARY = "_yf_data/yf_summary.json"
SIC = "_yf_data/edgar_sic.json"
NAMES = "_yf_data/yf_names.json"
KG = os.path.join("src", "kg", "kg_yf.json")
OUT = "_yf_data/concepts.json"

# 主题 ETF → 可投资概念（人工判定；多个 ETF 可指向同一概念）
ETF_CONCEPT = {
    "SMH": ("semiconductor", "半导体"), "SOXX": ("semiconductor", "半导体"),
    "AIQ": ("ai", "人工智能"), "BOTZ": ("robotics", "机器人与自动化"),
    "KWEB": ("china_internet", "中概互联网"), "CQQQ": ("china_tech", "中国科技"),
    "QQQ": ("nasdaq100", "纳斯达克100/大盘成长"), "XLK": ("us_tech", "美国科技"),
    "TLT": ("us_long_treasury", "美国长期国债"), "AGG": ("us_agg_bond", "美国综合债券"),
    "GLD": ("gold", "黄金"), "USO": ("oil", "原油"), "UUP": ("usd_index", "美元指数"),
}


def main():
    summ = json.load(open(SUMMARY, encoding="utf-8"))
    etfs = summ.get("etfs", {})
    names = json.load(open(NAMES, encoding="utf-8")) if os.path.exists(NAMES) else {}
    sic = json.load(open(SIC, encoding="utf-8")) if os.path.exists(SIC) else {}
    # 基金 → 资产类别（取自已建好的 KG）
    fund_ac = {}
    if os.path.exists(KG):
        for e in json.load(open(KG, encoding="utf-8"))["entities"]:
            if e["type"] == "Fund" and e.get("ticker"):
                fund_ac[e["ticker"]] = e.get("asset_class")

    def qtype(t):
        return ((names.get(t) or {}).get("quoteType") or "").upper()

    def market_of(t):
        t = str(t)
        if t.endswith(".HK"): return "HK"
        if t.endswith((".SS", ".SZ", ".SH", ".BJ")): return "CN"
        if t.endswith(".T"): return "JP"
        if t.endswith((".KS", ".KQ")): return "KR"
        if t.endswith(".TW"): return "TW"
        if t.endswith(".SW"): return "CH"
        return "US" if t.isalpha() else "OTHER"

    concepts = defaultdict(lambda: {"key": None, "name": None, "anchor_funds": [],
                                    "members": {}, "concept_type": "stock", "asset_class": None,
                                    "raw_members": []})
    for sym, s in etfs.items():
        if sym not in ETF_CONCEPT:
            continue
        key, cname = ETF_CONCEPT[sym]
        c = concepts[key]
        c["key"], c["name"] = key, cname
        c["anchor_funds"].append({"ticker": sym, "name": s.get("name"), "category": s.get("category")})
        acab = fund_ac.get(sym) or "Equity"
        # 只有股票型基金的概念才走「概念→股票」；债券/黄金/原油/美元走「概念→资产类别」
        if acab != "Equity":
            c["concept_type"], c["asset_class"] = "asset", acab
            continue
        for h in (s.get("top_holdings") or []):
            t = h.get("symbol")
            if not t:
                continue
            # ① 名称优先解析（symbol 可能是 Yahoo 的错误映射，如 005930.KQ），symbol 作兜底
            hname = h.get("name") or ""
            cid = co.resolve(hname) or co.resolve(t)
            if cid:
                ts = co.tickers(cid)
                # ② symbol 若确实属于该公司就沿用（保住基金实际持有的那一只，如 GOOGL）；
                #    若是 Yahoo 的错误代码（如 005930.KQ），才回退到公司的规范 ticker
                canon = t if (ts and t in ts) else (ts[0] if ts else t)
            else:
                # 无对应公司：过滤掉非股票成员（货币基金/现金类，如 BISXX/AGPXX）
                if qtype(t) and qtype(t) != "EQUITY":
                    continue
                canon = t
            w = h.get("weight")
            if canon in c["members"]:
                c["members"][canon] = max(c["members"][canon], w or 0)
            else:
                c["members"][canon] = w or 0
            if t != canon:
                c["raw_members"].append(t)

    # 行业层：EDGAR SIC（官方）
    industries = defaultdict(list)
    for t, v in sic.items():
        d = v.get("sicDescription")
        if d:
            industries[d].append(t)

    # 同一家公司的其他上市代码也并入概念成员。
    # 例：KWEB 持有的是京东港股 9618.HK，而新闻事件点名的是美股 ADR「JD」，
    # 不并进来的话 事件 -> 概念 -> 股票 -> 基金 这条链会在半路断掉。
    # 这条规则对公司整体生效，所以以后新增的双重上市公司自动适用
    # （前提是另一个代码也在 universe 里、且名称能归一化成同一家公司）。
    merged = []
    for k, c in concepts.items():
        for t in list(c["members"]):
            for alt in co.tickers(co.resolve(t)):
                if alt not in c["members"]:
                    c["members"][alt] = c["members"][t]      # 同一笔持仓，权重相同
                    merged.append((c["name"], t, alt))

    out = {"concepts": {}, "industries": {k: sorted(v) for k, v in sorted(industries.items(), key=lambda x: -len(x[1]))}}
    for k, c in concepts.items():
        markets = sorted({market_of(t) for t in c["members"]})
        out["concepts"][k] = {"key": k, "name": c["name"],
                              "concept_type": c["concept_type"],
                              "asset_class": c["asset_class"],
                              "anchor_funds": c["anchor_funds"],
                              "members": sorted(c["members"]),
                              "member_weights": {t: round(w, 4) for t, w in sorted(c["members"].items(), key=lambda x: -x[1])},
                              "n_members": len(c["members"]),
                              "markets": markets}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)

    print(f"[INFO] 概念 {len(out['concepts'])} 个 -> {OUT}")
    if merged:
        print(f"[INFO] 并入同一公司的其他上市代码 {len(merged)} 条:")
        for cname, t, alt in merged:
            print(f"   {cname}: 已按 {t} 纳入，另补上 {alt}")
    print(f"\n{'概念':22}{'类型':6}{'资产类别':13}{'锚定基金':14}{'成员':5}{'市场'}")
    for k, c in sorted(out["concepts"].items(), key=lambda x: -x[1]["n_members"]):
        funds = ",".join(f["ticker"] for f in c["anchor_funds"])
        print(f"{c['name']:22}{c['concept_type']:6}{str(c['asset_class'] or '-'):13}{funds:14}{c['n_members']:<5}{','.join(c['markets'])}")

    # 股票的概念归属（多归属）
    stock2c = defaultdict(set)
    for k, c in out["concepts"].items():
        for t in c["members"]:
            stock2c[t].add(c["name"])
    all_stocks = sorted({t for v in [names] for t in v} )
    covered = {t: cs for t, cs in stock2c.items()}
    multi = {t: sorted(cs) for t, cs in covered.items() if len(cs) > 1}
    print(f"\n[INFO] 有概念归属的股票 {len(covered)}；多归属 {len(multi)}")
    for t, cs in sorted(multi.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"   {t:9} {len(cs)} 个概念: {cs}")

    print(f"\n[INFO] 行业(SIC) {len(out['industries'])} 类，Top6:")
    for d, ts in list(out["industries"].items())[:6]:
        print(f"   {d[:44]:46} {len(ts):2} 只: {ts[:5]}")


if __name__ == "__main__":
    main()

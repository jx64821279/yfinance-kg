# -*- coding: utf-8 -*-
"""一次性分析：把同一批曝光度数据按两种口径归集，对比客户简报的呈现方式。

口径 A（现在）：按"主题"聚合——主题是开放的文本标签
口径 B（设想）：按"概念"聚合，驱动（driver_category）作为二级展开

用法：python tools_concept_view.py --customer C009
"""

import argparse
import collections
import csv
import json
import os

parser = argparse.ArgumentParser()
parser.add_argument("--kg", default=os.path.join("src", "kg", "kg_yf_v2.json"))
parser.add_argument("--exposure", default=os.path.join("_yf_data", "exposure_v2.json"))
parser.add_argument("--theme-concept", default=os.path.join("_yf_data", "theme_concept_map_v2.json"))
parser.add_argument("--customers", default=os.path.join("data", "customer_demo", "customers_all_in_one.csv"))
parser.add_argument("--customer", default="C009")
parser.add_argument("--top", type=int, default=6)
args = parser.parse_args()

SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def main():
    kg = json.load(open(args.kg, encoding="utf-8"))
    E = {e["id"]: e for e in kg["entities"]}
    exposure = json.load(open(args.exposure, encoding="utf-8"))["events"]
    cmap = json.load(open(args.theme_concept, encoding="utf-8"))

    theme_driver = {e["id"]: (e.get("driver_category") or "（未分类）")
                    for e in kg["entities"] if e["type"] == "Theme"}
    event_theme = {ed["source"]: ed["target"] for ed in kg["edges"] if ed["relation"] == "HAS_THEME"}
    stock_concepts = collections.defaultdict(list)
    fund_concepts = collections.defaultdict(list)
    ac_concepts = collections.defaultdict(list)
    for ed in kg["edges"]:
        if ed["relation"] == "INCLUDES":
            stock_concepts[ed["target"]].append(ed["source"])
        elif ed["relation"] == "FOCUSES_ON":
            fund_concepts[ed["source"]].append(ed["target"])
        elif ed["relation"] == "IS_CLASS" and E[ed["source"]]["type"] == "Concept":
            ac_concepts[ed["target"]].append(ed["source"])
    cname = {e["id"]: e["name"] for e in kg["entities"] if e["type"] == "Concept"}
    fund_id = {e["ticker"]: e["id"] for e in kg["entities"] if e["type"] == "Fund" and e.get("ticker")}

    with open(args.customers, encoding="utf-8-sig") as f:
        cust = next(r for r in csv.DictReader(f) if r["customer_id"] == args.customer)
    held = {x.split(":")[0]: int(x.split(":")[1].rstrip("%")) for x in cust["holdings"].split(";")}
    my_funds = {fund_id[t]: w for t, w in held.items() if t in fund_id}

    by_theme = collections.defaultdict(float)
    by_concept = collections.defaultdict(lambda: collections.defaultdict(float))

    for eid, ev in exposure.items():
        sgn = SIGN.get(ev.get("polarity"), 0.0)
        tid = event_theme.get(eid)
        driver = theme_driver.get(tid, "（未分类）")
        theme = E.get(tid, {}).get("name", "?")
        for f in ev.get("funds", []):
            if f["fund"] not in my_funds:
                continue
            w = my_funds[f["fund"]] / 100.0
            by_theme[theme] += f["score"] * w
            if sgn == 0:
                continue
            # 每条路径先取"未加符号"的贡献，再等比缩放到该产品总分
            # （曝光度里每个产品只保留前 4 条路径，直接加总会和总分对不上）
            raw = []
            for p in f.get("paths") or []:
                if p["type"] == "direct_product":
                    raw.append((fund_concepts.get(f["fund"]) or ["（无落点）"], 1.0))
                elif p["type"] == "direct":
                    raw.append((stock_concepts.get(p.get("via")) or ["（无落点）"], p.get("weight", 0.0)))
                elif p["type"] == "same_asset_class":
                    raw.append((ac_concepts.get(p.get("via")) or ["（资产类别）"], 0.5))
                else:
                    raw.append(([p.get("via")], p.get("affinity", 1.0) if p["type"] == "concept" else 1.0))
            total = sum(v * len(cs) for cs, v in raw) or 1.0
            for cs, v in raw:
                for c in cs:
                    by_concept[c][driver] += sgn * v * (abs(f["score"]) / total) * w

    print("客户 %s %s  |  持仓 %s" % (cust["customer_id"], cust["name"], cust["holdings"]))
    print()
    print("=== 口径 A：按主题聚合（现在的做法）===")
    for k, v in sorted(by_theme.items(), key=lambda x: -abs(x[1]))[:args.top]:
        print("   %-24s %+8.2f" % (k[:24], v))
    print("   %-24s %+8.2f  （%d 个主题）" % ("合计", sum(by_theme.values()), len(by_theme)))
    print()
    print("=== 口径 B：按概念聚合，驱动做二级（设想）===")
    ranked = sorted(by_concept.items(), key=lambda x: -abs(sum(x[1].values())))
    for c, d in ranked[:args.top]:
        print("   %-18s 净 %+7.2f" % (cname.get(c, c)[:18], sum(d.values())))
        for dr, v in sorted(d.items(), key=lambda x: -abs(x[1]))[:3]:
            print("        驱动：%-22s %+7.2f" % (dr[:22], v))
    print("   %-18s %+8.2f  （%d 个概念）"
          % ("合计", sum(sum(d.values()) for _, d in ranked), len(by_concept)))


if __name__ == "__main__":
    main()

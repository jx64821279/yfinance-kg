"""
在知识图谱之上做「事件 → 产品」的曝光度打分（检索层，产物不写回图）。

为什么要单独一层：图谱只记事实（谁持有谁、谁属于哪个概念、事件属于什么主题），
而"某条新闻对哪只产品影响多大"是**策略输出**，参数会调整，所以放在图之外。

打分规则（一阶 = 直接持有，二阶 = 同概念辐射）：
  A. 事件直接点名产品（AFFECTS→Fund）            w = 1.00
  B. 事件点名公司，产品持有该公司                 w = 该股票的持仓权重
  C. 产品在该概念上的持仓集中度（概念成员权重之和）   w = 集中度 × 0.30
  D. 产品聚焦该概念（锚定基金）                    w = 0.30
  E. 资产型概念：产品与该概念同属一个资产类别         w = 0.15
  score(fund) = Σ 各条路径的 w，再按 polarity 给正负号

产物：_yf_data/exposure_v2.json
  {"meta": {...参数...},
   "events": {"event_0001": {"polarity": "negative", "funds": [
        {"fund": "fund_SMH", "ticker": "SMH", "score": -0.442, "abs_score": 0.442,
         "paths": [{"type":"direct","via":"stock_NVDA","weight":0.2263},
                   {"type":"concept","via":"concept_semiconductor","affinity":0.72}]}, ...]}}}

用法：
  python exposure_v2.py                      # 全量计算并落盘 + 打印统计
  python exposure_v2.py --event event_0123   # 只打印某条事件的排序与路径
  python exposure_v2.py --params w2=0.2      # 临时改参数试算
"""
import argparse
import collections
import json
import os

OUT_DIR = "_yf_data"

parser = argparse.ArgumentParser()
parser.add_argument("--kg", default=os.path.join("src", "kg", "kg_yf_v2.json"))
parser.add_argument("--out", default=os.path.join(OUT_DIR, "exposure_v2.json"))
parser.add_argument("--event", default=None, help="只打印这一条事件的排序结果")
parser.add_argument("--w1", type=float, default=1.00, help="一阶直接持有的权重系数")
parser.add_argument("--w2", type=float, default=0.30, help="二阶同概念辐射的权重系数")
parser.add_argument("--top", type=int, default=0, help="每条事件只保留前 N 只产品（0=全部有分的）")
parser.add_argument("--quiet", action="store_true")
args, _ = parser.parse_known_args()

SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}

# 规则 E（资产型概念 → 同资产类别的其它产品）的折扣系数，按资产类别区分。
# 口径修正：原来一律 0.5，导致“黄金”和“原油”同为 Commodity 时互相打分
# （油价新闻会给黄金基金加分），在客户侧会变成“用石油主题去解释黄金持仓”这种硬凑。
# 债券保留，因为 AGG 与 TLT 确实同受利率驱动；商品/外汇取消。
SAME_CLASS_W = {"FixedIncome": 0.5, "Commodity": 0.0, "FX": 0.0, "Equity": 0.0}


def load_graph(path):
    kg = json.load(open(path, encoding="utf-8"))
    E = {e["id"]: e for e in kg["entities"]}
    out, inc = collections.defaultdict(list), collections.defaultdict(list)
    for ed in kg["edges"]:
        out[(ed["source"], ed["relation"])].append(ed)
        inc[(ed["target"], ed["relation"])].append(ed)
    return kg, E, out, inc


def main():
    kg, E, out, inc = load_graph(args.kg)
    funds = [e for e in kg["entities"] if e["type"] == "Fund"]
    concepts = [e for e in kg["entities"] if e["type"] == "Concept"]

    # 产品持仓：fund -> {stock: weight}
    holding = collections.defaultdict(dict)
    for ed in out[("__none__", "__none__")] if False else []:
        pass
    for ed in kg["edges"]:
        if ed["relation"] == "CONSTITUENT" and (ed.get("weight") is not None):
            holding[ed["source"]][ed["target"]] = float(ed["weight"])

    # 概念集中度：concept -> fund -> 该概念成员在该产品里的权重之和
    affinity = collections.defaultdict(dict)
    for c in concepts:
        members = [ed["target"] for ed in out[(c["id"], "INCLUDES")]]
        for f in funds:
            s = sum(holding[f["id"]].get(m, 0.0) for m in members)
            if s > 0:
                affinity[c["id"]][f["id"]] = min(s, 1.0)
    # 产品聚焦的概念 / 资产型概念的同类产品
    focus = collections.defaultdict(set)
    for ed in kg["edges"]:
        if ed["relation"] == "FOCUSES_ON":
            focus[ed["target"]].add(ed["source"])
    assets_of = collections.defaultdict(set)   # AssetClass -> {fund}
    for ed in kg["edges"]:
        if ed["relation"] == "IS_CLASS" and E[ed["source"]]["type"] == "Fund":
            assets_of[ed["target"]].add(ed["source"])

    events = [e for e in kg["entities"] if e["type"] == "Event"]
    result = {"meta": {"source_kg": os.path.basename(args.kg), "w1_direct": args.w1,
                       "w2_concept": args.w2, "rule": "A/B 一阶直接；C/D/E 二阶概念；求和后按 polarity 取正负",
                       "n_events": len(events)}, "events": {}}

    covered = 0
    spread = collections.Counter()
    for ev in events:
        fscore = collections.defaultdict(float)
        fpaths = collections.defaultdict(list)

        # A. 事件直接点名产品
        for ed in out[(ev["id"], "AFFECTS")]:
            if E[ed["target"]]["type"] == "Fund":
                fscore[ed["target"]] += args.w1 * 1.0
                fpaths[ed["target"]].append({"type": "direct_product"})
            else:  # B. 点名公司 → 持有它的产品
                for f, w in holding[ed["target"]].items():
                    fscore[f] += args.w1 * w
                    fpaths[f].append({"type": "direct", "via": ed["target"],
                                      "weight": round(w, 4)})
        # C/D/E. 主题 → 概念 → 产品
        for ted in out[(ev["id"], "HAS_THEME")]:
            for ced in out[(ted["target"], "MAPS_TO")]:
                cid = ced["target"]
                for f, aff in affinity.get(cid, {}).items():
                    fscore[f] += args.w2 * aff
                    fpaths[f].append({"type": "concept", "via": cid, "affinity": round(aff, 3)})
                for f in focus.get(cid, ()):
                    fscore[f] += args.w2 * 1.0
                    fpaths[f].append({"type": "anchor_fund", "via": cid})
                for ac in [ed["target"] for ed in out[(cid, "IS_CLASS")]]:
                    k = SAME_CLASS_W.get(E[ac].get("name"), 0.0)
                    if k <= 0:
                        continue
                    for f in assets_of.get(ac, ()):
                        fscore[f] += args.w2 * k
                        fpaths[f].append({"type": "same_asset_class", "via": ac})

        sign = SIGN.get(ev.get("polarity"), 0.0)
        rows = []
        for f, s in fscore.items():
            if s <= 0:
                continue
            rows.append({"fund": f, "ticker": E[f].get("ticker"),
                         "score": round(s * sign, 4), "abs_score": round(s, 4),
                         "paths": fpaths[f][:4]})
        rows.sort(key=lambda r: -r["abs_score"])
        if args.top:
            rows = rows[:args.top]
        if rows:
            covered += 1
            spread[len(rows)] += 1
        result["events"][ev["id"]] = {"polarity": ev.get("polarity"), "scope": ev.get("scope"),
                                      "news_title": ev.get("news_title"), "funds": rows}

    if args.event:
        ev = result["events"].get(args.event)
        if not ev:
            raise SystemExit(f"未找到 {args.event}")
        print(f'{args.event} | polarity={ev["polarity"]} | scope={ev["scope"]}')
        print(f'   {(ev["news_title"] or "")[:90]}')
        for r in ev["funds"]:
            paths = " + ".join(
                p["type"] + (f'({p.get("via","")},{p.get("weight", p.get("affinity",""))})' if p.get("via") else "")
                for p in r["paths"])
            print(f'   {r["ticker"]:6} score={r["score"]:+.4f}  {paths}')
        return

    json.dump(result, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if not args.quiet:
        print(f"[INFO] 事件 {len(events)} 条 | 至少命中一只产品 {covered} 条 | -> {args.out}")
        print(f"   每条事件命中的产品数分布: {dict(sorted(spread.items()))}")


if __name__ == "__main__":
    main()

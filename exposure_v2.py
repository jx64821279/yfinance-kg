# -*- coding: utf-8 -*-
"""事件 → 产品的曝光度打分（策略口径层，可按需调用）。

定位
    图谱只记事实（谁持有谁、谁属于哪个概念、事件属于什么主题），
    "某条新闻对哪只产品影响多大"是**策略输出**：参数会调、要能随时重算，
    所以既不入库、也不落成管线产物——检索层（kg_subgraph.py）用到时现算。

    这个文件对外提供算分能力（模块），自己也是个命令行工具（壳子）：
        model = ExposureModel.from_kg("src/kg/kg_yf_v3.json")
        model = ExposureModel.from_db(con, fund_ids=[...])   # 只算某客户持有的产品
        rows  = model.score_event(event_id, polarity)        # → 排序好的产品列表

文件名
    沿用 v2 时期的叫法（v2 文档里就是按这个名字引用的）。v2 时它"跑一遍、落一个
    文件"，v3 起改成被检索层调用、按需现算，不再产出 exposure_*.json。

规则（一阶 = 直接持有，二阶 = 同概念辐射）：
  A. 事件直接点名产品（AFFECTS→Fund）               w = 1.00
  B. 事件点名公司（AFFECTS→Stock），产品持有该公司    w = 该股票在该产品里的持仓权重
  C. 产品在该概念上的持仓集中度（概念成员权重之和）    w = 集中度 × w2
  D. 产品聚焦该概念（锚定基金）                      w = w2
  E. 资产型概念：产品与该概念同属一个资产类别         w = w2 × 该资产类别的折扣
  score(fund) = Σ 各条路径的 w，再按 polarity 给正负号

用法
    python exposure_v2.py --event event_17aee2fa          # 看这条事件的排序与路径
    python exposure_v2.py --fund SMH                      # 只算这一只产品
    python exposure_v2.py --out _yf_data/exposure_v3.json # 导出快照（排查用，不是管线产物）
"""

import argparse
import collections
import json
import os

SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}

# 规则 E（资产型概念 → 同资产类别的其它产品）的折扣系数，按资产类别区分。
# 口径修正：原来一律 0.5，导致"黄金"和"原油"同为 Commodity 时互相打分
# （油价新闻会给黄金基金加分），在客户侧会变成"用石油主题去解释黄金持仓"这种硬凑。
# 债券保留，因为 AGG 与 TLT 确实同受利率驱动；商品/外汇取消。
SAME_CLASS_W = {"FixedIncome": 0.5, "Commodity": 0.0, "FX": 0.0, "Equity": 0.0}


class ExposureModel:
    """从图（或库）建索引，给事件打分。索引建一次，可反复调用。"""

    def __init__(self, entities, edges, w1=1.00, w2=0.30, same_class_w=None):
        self.w1, self.w2 = w1, w2
        self.same_class_w = dict(SAME_CLASS_W)
        if same_class_w:
            self.same_class_w.update(same_class_w)

        self.E = {e["id"]: e for e in entities}
        self.holding = collections.defaultdict(dict)            # 产品 -> {股票: 权重}
        self.holding_by_stock = collections.defaultdict(dict)   # 股票 -> {产品: 权重}
        self.concept_members = collections.defaultdict(set)     # 概念 -> {股票}
        self.focus = collections.defaultdict(set)               # 概念 -> {锚定产品}
        self.assets_of = collections.defaultdict(set)           # 资产类别 -> {产品}
        self.concept_class = collections.defaultdict(set)       # 概念 -> {资产类别}
        self.event_themes = collections.defaultdict(list)       # 事件 -> [主题]
        self.theme_concepts = collections.defaultdict(list)     # 主题 -> [概念]
        self.event_affects = collections.defaultdict(list)      # 事件 -> [产品/股票]

        for ed in edges:
            s, r, t = ed["source"], ed["relation"], ed["target"]
            if r == "CONSTITUENT":
                w = ed.get("weight")
                if w is None:
                    continue
                w = float(w)
                self.holding[s][t] = w
                self.holding_by_stock[t][s] = w
            elif r == "INCLUDES":
                self.concept_members[s].add(t)
            elif r == "FOCUSES_ON":
                self.focus[t].add(s)
            elif r == "IS_CLASS":
                e = self.E.get(s) or {}
                if e.get("type") == "Fund":
                    self.assets_of[t].add(s)
                elif e.get("type") == "Concept":
                    self.concept_class[s].add(t)
            elif r == "HAS_THEME":
                self.event_themes[s].append(t)
            elif r == "MAPS_TO":
                self.theme_concepts[s].append(t)
            elif r == "AFFECTS":
                self.event_affects[s].append(t)

        # 概念集中度：产品里属于该概念的成员权重之和
        self.affinity = collections.defaultdict(dict)
        for cid, members in self.concept_members.items():
            for fid, hs in self.holding.items():
                s = sum(hs.get(m, 0.0) for m in members)
                if s > 0:
                    self.affinity[cid][fid] = min(s, 1.0)

    # ------------------------------------------------------------ 构造

    @classmethod
    def from_kg(cls, path, **kw):
        kg = json.load(open(path, encoding="utf-8"))
        return cls(kg["entities"], kg["edges"], **kw)

    @classmethod
    def from_json(cls, kg, **kw):
        return cls(kg["entities"], kg["edges"], **kw)

    @classmethod
    def from_db(cls, con, fund_ids=None, node_types=("Fund", "Concept", "AssetClass", "Stock"),
                **kw):
        """从知识库取数建索引。

        fund_ids 给定时只装这些产品的持仓，曝光度也只可能落在这些产品上
        （客户侧就是"只算他持有的产品"）。
        """
        cur = con.cursor()
        entities = []
        for r in cur.execute(
                "SELECT id, type, name, ticker, props FROM node WHERE type IN (%s)"
                % ",".join("?" * len(node_types)), node_types):
            e = {"id": r[0], "type": r[1], "name": r[2]}
            if r[3]:
                e["ticker"] = r[3]
            if r[4]:
                e.update(json.loads(r[4]))
            entities.append(e)

        rels = ["INCLUDES", "IS_CLASS", "MAPS_TO", "HAS_THEME", "AFFECTS"]
        queries = [("SELECT src, rel, dst, props FROM edge WHERE rel IN (%s)"
                    % ",".join("?" * len(rels)), list(rels))]
        for rel in ("CONSTITUENT", "FOCUSES_ON"):
            if fund_ids:
                queries.append(("SELECT src, rel, dst, props FROM edge WHERE rel='%s' AND src IN (%s)"
                                % (rel, ",".join("?" * len(fund_ids))), list(fund_ids)))
            else:
                queries.append(("SELECT src, rel, dst, props FROM edge WHERE rel='%s'" % rel, []))

        edges = []
        for q, prm in queries:
            for r in cur.execute(q, prm):
                ed = {"source": r[0], "relation": r[1], "target": r[2]}
                p = json.loads(r[3]) if r[3] else {}
                if isinstance(p, dict):
                    ed.update(p)
                edges.append(ed)
        return cls(entities, edges, **kw)

    # ------------------------------------------------------------ 打分

    def score_concepts(self, concepts=(), affects=(), polarity="neutral", funds=None, top=0):
        """「概念 + 点名标的」→ 受影响产品列表（按 |score| 从大到小）。

        这是打分的本体；score_event() 只是把图里的事件翻译成这两个入参再调它。
        单独暴露出来是为了**还没进图的事件**也能算（例如刚抓到、还在预览的一条新闻）。

        concepts: 概念，key（"ai"）或 id（"concept_ai"）都可以
        affects : 点名到的标的，stock_/fund_ 前缀或裸 ticker 都可以
        funds   : 只算哪些产品（None = 索引里的全部）
        """
        def keep(f):
            return funds is None or f in funds

        fscore = collections.defaultdict(float)
        fpaths = collections.defaultdict(list)

        # A. 事件直接点名产品
        for t in affects:
            t = self._resolve_entity_id(t)
            e = self.E.get(t) or {}
            if e.get("type") == "Fund":
                if keep(t):
                    fscore[t] += self.w1
                    fpaths[t].append({"type": "direct_product"})
            else:
                # B. 点名公司 → 持有它的产品（按股票反查产品，不是按产品查股票）
                for f, w in self.holding_by_stock.get(t, {}).items():
                    if not keep(f):
                        continue
                    fscore[f] += self.w1 * w
                    fpaths[f].append({"type": "direct_holding", "via": t, "weight": round(w, 4)})

        # C/D/E. 主题 → 概念 → 产品
        for cid in concepts:
            cid = self._resolve_concept_id(cid)
            if not cid:
                continue
            for f, aff in self.affinity.get(cid, {}).items():
                if not keep(f):
                    continue
                fscore[f] += self.w2 * aff
                fpaths[f].append({"type": "concept", "via": cid, "affinity": round(aff, 3)})
            for f in self.focus.get(cid, ()):
                if not keep(f):
                    continue
                fscore[f] += self.w2
                fpaths[f].append({"type": "anchor_fund", "via": cid})
            for ac in self.concept_class.get(cid, ()):
                k = self.same_class_w.get((self.E.get(ac) or {}).get("name"), 0.0)
                if k <= 0:
                    continue
                for f in self.assets_of.get(ac, ()):
                    if not keep(f):
                        continue
                    fscore[f] += self.w2 * k
                    fpaths[f].append({"type": "same_asset_class", "via": ac})

        sign = SIGN.get(polarity, 0.0)
        rows = []
        for f, s in fscore.items():
            if s <= 0:
                continue
            rows.append({"fund": f, "ticker": (self.E.get(f) or {}).get("ticker"),
                         "score": round(s * sign, 4), "abs_score": round(s, 4),
                         "paths": fpaths[f][:4]})
        rows.sort(key=lambda r: -r["abs_score"])
        return rows[:top] if top else rows

    # 入参宽一点：key / 裸代码 / 带前缀的 id 都认
    def _resolve_concept_id(self, c):
        c = str(c or "")
        if c in self.E:
            return c
        for cand in ("concept_" + c, "concept_" + c.lower()):
            if cand in self.E:
                return cand
        byname = {e.get("name"): i for i, e in self.E.items() if e.get("type") == "Concept"}
        return byname.get(c)

    def _resolve_entity_id(self, t):
        t = str(t or "")
        if t in self.E:
            return t
        for pref in ("stock_", "fund_"):
            if pref + t in self.E:
                return pref + t
        return t

    def score_event(self, event_id, polarity, funds=None, top=0):
        """图里的一条事件 → 受影响产品列表。

        v3 里每个主题恰好映射一个概念，所以"事件 → 主题 → 概念"等价于直接给概念清单。
        """
        concepts = [c for th in self.event_themes.get(event_id, ())
                    for c in self.theme_concepts.get(th, ())]
        return self.score_concepts(concepts, self.event_affects.get(event_id, ()),
                                   polarity, funds=funds, top=top)

    def score_events(self, events, funds=None, top=0):
        """批量：events 是 [{"id":..., "polarity":...}, ...] → {event_id: [行]}。"""
        return {ev["id"]: self.score_event(ev["id"], ev.get("polarity"), funds=funds, top=top)
                for ev in events}


# ---------------------------------------------------------------- 命令行

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kg", default=os.path.join("src", "kg", "kg_yf_v3.json"))
    ap.add_argument("--event", default=None, help="只打印这一条事件的排序结果")
    ap.add_argument("--fund", default=None, help="只算这一只产品（按 ticker）")
    ap.add_argument("--out", default=None, help="导出快照到 JSON（排查用，不是管线产物）")
    ap.add_argument("--w1", type=float, default=1.00, help="一阶直接持有的权重系数")
    ap.add_argument("--w2", type=float, default=0.30, help="二阶同概念辐射的权重系数")
    ap.add_argument("--top", type=int, default=0, help="每条事件只保留前 N 只产品（0=全部）")
    args = ap.parse_args()

    model = ExposureModel.from_kg(args.kg, w1=args.w1, w2=args.w2)
    funds = None
    if args.fund:
        tick = {e.get("ticker"): i for i, e in model.E.items() if e.get("ticker")}
        fid = tick.get(args.fund)
        if not fid:
            raise SystemExit("图里没有这个产品：%s" % args.fund)
        funds = {fid}

    events = [{"id": e["id"], "polarity": e.get("polarity"), "title": e.get("news_title")}
              for e in model.E.values() if e.get("type") == "Event"]

    if args.event:
        ev = next((e for e in events if e["id"] == args.event), None)
        if not ev:
            raise SystemExit("图里没有这条事件：%s" % args.event)
        rows = model.score_event(args.event, ev.get("polarity"), funds=funds, top=args.top)
        print("%s | polarity=%s" % (args.event, ev.get("polarity")))
        print("   %s" % (ev.get("title") or "")[:90])
        for r in rows:
            paths = " + ".join(
                p["type"] + (("(%s,%s)" % (p.get("via", ""), p.get("weight", p.get("affinity", ""))))
                             if p.get("via") else "")
                for p in r["paths"])
            print("   %-6s score=%+.4f  %s" % (r["ticker"], r["score"], paths))
        return

    scored = model.score_events(events, funds=funds, top=args.top)
    hit = {k: v for k, v in scored.items() if v}
    spread = collections.Counter(len(v) for v in hit.values())
    print("[INFO] 事件 %d 条 | 至少命中一只产品 %d 条" % (len(events), len(hit)))
    print("   每条事件命中的产品数分布: %s" % dict(sorted(spread.items())))
    if args.out:
        by_id = {e["id"]: e for e in events}
        dump = {"meta": {"source_kg": os.path.basename(args.kg), "w1_direct": args.w1,
                         "w2_concept": args.w2, "n_events": len(events),
                         "note": "排查用快照；管线里曝光度按需现算，不落盘"},
                "events": {k: {"polarity": by_id[k].get("polarity"),
                               "news_title": by_id[k].get("title"), "funds": v}
                           for k, v in scored.items()}}
        json.dump(dump, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("   快照 -> %s" % args.out)


if __name__ == "__main__":
    main()

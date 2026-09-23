"""
用 yfinance/Yahoo 数据 + 抽取出的新闻事件，构建知识图谱。
节点：AssetClass / Theme / Event / Stock / Company / Fund / Industry
边  ：Event-HAS_THEME->Theme, Theme-IMPACTS->AssetClass, Event-AFFECTS->Stock,
       Stock-IS_CLASS->AssetClass, Company-CORRESPONDS_TO->Stock, Stock-IN_INDUSTRY->Industry,
       Fund-IS_CLASS->AssetClass, Fund-CONSTITUENT(weight)->Stock
       Fund-HAS_CASH(weight)->AssetClass:MoneyMarket（产品里的现金类资产）
       Theme-MAPS_TO->Concept（没有点名公司的新闻靠这条边传播到可投资标的）
约定：ETF/共同基金/货币基金只建 Fund 节点，不建 Stock；新闻点名 ETF 时直接
      Event-AFFECTS->Fund；基金持仓的代码按公司规范 ticker 落点（避免 005930.KQ 之类
      的错误条目把同一家公司拆成两个节点）；持仓表里的货币基金不建实体，只体现为
      Fund-HAS_CASH->MoneyMarket。
运行：python build_kg_yf.py
产出：src/kg/kg_yf.json
"""
import os
import re
import json

EVENTS = ("_yf_data/events_extracted_all.json"
          if os.path.exists("_yf_data/events_extracted_all.json")
          else "_yf_data/events_extracted.json")
SUMMARY = "_yf_data/yf_summary.json"
NAMES = "_yf_data/yf_names.json"
THEME_VOCAB = "_yf_data/theme_vocab.json"
THEME_MAP = "_yf_data/theme_map.json"
CONCEPTS = "_yf_data/concepts.json"
THEME_CONCEPT = "_yf_data/theme_concept_map.json"
SIC = "_yf_data/edgar_sic.json"
OUT_DIR = os.path.join("src", "kg")
OS_PATH = os.path.join(OUT_DIR, "kg_yf.json")

ASSET_CLASSES = ["Equity", "FixedIncome", "MoneyMarket", "Commodity", "FX"]


def slug(s):
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(s)).strip("_")
    return s[:48] or "na"


# 公司名尾部的「法律形式 / 组织形式 / 存托凭证」后缀，只从末尾剥，
# 不动名字中间的词（避免把 Technology、Group 之类的品牌词误删）
_LEGAL_TAIL = (
    r"incorporated|inc|corporation|corp|company|co|limited|ltd|plc|llc|llp|lp|"
    r"nv|n\.v|sa|s\.a|ag|gmbh|bhd|berhad|pte|sdn|kk|kg|oyj|ab|asa|spa|sarl|sas|"
    r"holdings|holding|group|"
    r"shs|unitary|144a|"
    r"american depositary shares|american depositary receipts|"
    r"global depositary receipts|sponsored|registered|"
    r"gdr|adr|ads|ordinary|common|stock|shares"
)
# 一次只剥掉末尾一个词，方便在不合适时把「实义词」还回去
_TAIL_RE = re.compile(r"[\s,\.\-–\(\)]*(?:\b(?:%s)\b\.?[\s,\.\-–\(\)]*)$" % _LEGAL_TAIL, re.I)
_CLASS_RE = re.compile(r"[\s,\-–]*\bclass\s+[a-z0-9]+\b.*$", re.I)
# 「Shs Unitary 144A/Reg S」这类美股存托格式的整段尾巴
_SHARE_TAIL_RE = re.compile(
    r"[\s,\./\-–]*(?:\bshs\b[\s,]+unitary[\s,]+)?(?:\b144a\b[\s,]*/[\s,]*)?"
    r"\breg(?:ulation)?\b[\s,]*\bs\b[\s,\./\-–]*$", re.I)
# 有辨识度的机构词：剥完如果只剩很短一截（容易和别家代码撞名），就把最后一个还回去
_SEMANTIC_TAIL = {"holdings", "holding", "group"}
_MIN_NAME_LEN = 4


def clean_company_name(n):
    """去掉 Inc / Corporation / Limited / Co., Ltd. 等后缀，以及 Class A / ADR 之类的尾巴。

    NVIDIA Corporation        -> NVIDIA
    JD.com, Inc.              -> JD.com
    Samsung Electronics Co., Ltd. -> Samsung Electronics
    Alibaba Group Holding Limited -> Alibaba

    护栏：只剥到名字剩下的部分不少于 4 个字符；如果剥完只剩很短一截
    （如 KE Holdings Inc. -> KE，会和 Kimball Electronics 的代码 KE 撞上），
    就把最后一个实义词还回来（-> KE Holdings）。
    """
    s = str(n or "").replace("\u00a0", " ").strip()
    if not s:
        return ""
    s = _CLASS_RE.sub("", s)
    s = _SHARE_TAIL_RE.sub("", s).strip(" ,.-–()/")
    removed = []
    for _ in range(8):
        m = _TAIL_RE.search(s)
        if not m:
            break
        t = (s[:m.start()]).strip(" ,.-–()")
        if len(t) < 2:                       # 防止把整个名字剥光
            break
        removed.append(s[m.start():].strip(" ,.-–()"))
        s = t
    if len(s) < _MIN_NAME_LEN:               # 太短：把最后剥掉的实义词还回来
        for frag in reversed(removed):
            if frag.split()[0].lower() in _SEMANTIC_TAIL:
                s = f"{s} {frag}"
                break
    return re.sub(r"\s+", " ", s).strip(" ,.-–()")


_CO_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|nv|sa|ag|"
    r"group|holding|holdings|technologies|technology|the|adr|ads|ordinary|shares|class)\b", re.I)


def norm_company(name):
    """公司名归一化：去后缀/标点/大小写，用于把同一家公司的多个 ticker 归到一组。"""
    s = str(name or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    s = _CO_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def fund_class(sym, s):
    cat = (s.get("category") or "")
    pos = s.get("positions") or {}
    stock = pos.get("stockPosition") or 0
    bond = pos.get("bondPosition") or 0
    cash = pos.get("cashPosition") or 0
    other = pos.get("otherPosition") or 0
    if "Commodit" in cat:
        return "Commodity"
    if sym == "UUP" or "US Dollar" in (s.get("name") or "") or "Currency" in cat:
        return "FX"
    if bond >= 0.5:
        return "FixedIncome"
    if stock >= 0.5:
        return "Equity"
    if other >= 0.5:
        return "Commodity"
    if cash >= 0.5:
        return "MoneyMarket"
    return "Equity"


def main():
    events = json.load(open(EVENTS, encoding="utf-8"))
    summ = json.load(open(SUMMARY, encoding="utf-8"))
    etfs, stocks_meta = summ["etfs"], summ["stocks"]

    entities, edges, seen = {}, [], set()

    def add_entity(eid, etype, name, props=None):
        if eid not in entities:
            e = {"id": eid, "type": etype, "name": name}
            if props:
                e.update({k: v for k, v in props.items() if v not in (None, "")})
            entities[eid] = e

    def add_edge(s, rel, t, props=None):
        if (s, rel, t) in seen:
            return
        seen.add((s, rel, t))
        e = {"source": s, "relation": rel, "target": t}
        if props:
            # 注意：source/relation/target 是保留键，属性里同名的要改名，否则会把边的起点覆盖掉
            e.update({k: v for k, v in props.items()
                      if k not in ("source", "relation", "target") and v not in (None, "")})
        edges.append(e)

    for c in ASSET_CLASSES:
        add_entity(f"asset_class_{c}", "AssetClass", c)

    # ---- 公司归一化：按「归一化 longName」分组（同一公司可有多个 ticker）----
    names = json.load(open(NAMES, encoding="utf-8")) if os.path.exists(NAMES) else {}

    def nm_of(ticker, fallback=""):
        v = names.get(ticker) or {}
        return v.get("longName") or v.get("shortName") or fallback or ""

    def qt_of(ticker):
        return ((names.get(ticker) or {}).get("quoteType") or "").upper()

    def is_fund_type(ticker):
        """ETF / 共同基金 / 货币基金不是「股票」，不该建成 Stock 节点。"""
        return qt_of(ticker) in ("ETF", "MUTUALFUND", "MONEYMARKET") or ticker in etfs

    hold_name, all_tickers = {}, set()
    for _, s in etfs.items():
        for h in (s.get("top_holdings") or []):
            hs = h.get("symbol")
            if hs:
                all_tickers.add(hs)
                if h.get("name"):
                    hold_name.setdefault(hs, h["name"])
    all_tickers.update(stocks_meta.keys())
    for ev in events:
        all_tickers.update(ev.get("affected_tickers") or [])

    alias_map = {}          # 名称 -> company id
    company_by_key = {}     # 归一化名 -> company id

    def canon_holding_ticker(sym, hname):
        """基金持仓里的 symbol -> 该公司在 KG 里的规范 ticker。

        Yahoo 的持仓表有时给的是错误/非主上市代码（如 Samsung 记成 005930.KQ，
        实际是 KOSDAQ 上的基金条目）。这里按名称解析到公司，再取规范代码；
        若 symbol 本身就在该公司的代码列表里（如 GOOGL），保持原样。
        """
        cid = company_by_key.get(norm_company(hname))
        if cid:
            ts = entities[cid].get("tickers") or []
            if sym in ts:
                return sym
            if ts:
                return ts[0]
        return sym

    def ensure_company(name, alias=None):
        if not name:
            return None
        key = norm_company(name)
        if not key:
            return None
        cid = company_by_key.get(key)
        if cid is None:
            cid = f"company_{slug(key)}"
            company_by_key[key] = cid
            entities[cid] = {"id": cid, "type": "Company", "name": clean_company_name(name),
                             "tickers": [], "aliases": []}
        e = entities[cid]
        for a in (e["name"], str(name or "").strip(), str(alias or "").strip()):
            if a and a != e["name"] and a not in e["aliases"]:
                e["aliases"].append(a)
            if a:
                alias_map[a] = cid
        alias_map[e["name"]] = cid
        return cid

    # (1) 每个 ticker：建 Stock；股票按归一化 longName 归到 Company（一家公司可挂多个 ticker）
    for t in sorted(all_tickers):
        if is_fund_type(t):
            continue                    # ETF/货币基金走下面的基金层
        sid = f"stock_{t}"
        add_entity(sid, "Stock", clean_company_name(nm_of(t, hold_name.get(t, ""))) or t,
                   {"ticker": t, "asset_class": "Equity"})
        add_edge(sid, "IS_CLASS", "asset_class_Equity")
        qt = (names.get(t) or {}).get("quoteType")
        if qt in (None, "EQUITY"):           # ETF/基金不算公司
            cid = ensure_company(nm_of(t, hold_name.get(t, "")), alias=hold_name.get(t))
            if cid:
                if t not in entities[cid]["tickers"]:
                    entities[cid]["tickers"].append(t)
                add_edge(cid, "CORRESPONDS_TO", sid)

    # 行业层：EDGAR SIC（官方；只挂到股票，不挂 ETF）
    if os.path.exists(SIC):
        for tk, v in json.load(open(SIC, encoding="utf-8")).items():
            desc = v.get("sicDescription")
            if not desc:
                continue
            if qt_of(tk) not in ("", "EQUITY"):        # 跳过基金（GLD/USO/UUP 也有 SIC）
                continue
            iid = f"industry_sic_{slug(desc)}"
            add_entity(iid, "Industry", desc, {"standard": "EDGAR SIC", "sic": v.get("sic")})
            add_edge(f"stock_{tk}", "IN_INDUSTRY", iid)

    # (2) 基金层：Fund → 持仓
    n_fund_holdings = 0        # 持仓表里属于「基金」的行（货币基金等），按现金处理
    for sym, s in etfs.items():
        fid = f"fund_{sym}"
        cls = fund_class(sym, s)
        add_entity(fid, "Fund", nm_of(sym, s.get("name") or sym) or sym,
                   {"ticker": sym, "family": s.get("family"), "category": s.get("category"),
                    "asset_class": cls, "positions": json.dumps(s.get("positions"), ensure_ascii=False)})
        add_edge(fid, "IS_CLASS", f"asset_class_{cls}")

        # 现金类资产：ETF 自己申报的 cashPosition。持仓表里那些"货币基金"
        # （AGG 的 BISXX 3.01%、UUP 的 AGPXX 48.83%）其实就是这一块现金，
        # 所以不建货币基金实体，只把「这只产品有多少钱在现金类资产上」
        # 挂到货币型资产类别节点上。
        cash = (s.get("positions") or {}).get("cashPosition") or 0
        if cash > 0:
            add_edge(fid, "HAS_CASH", "asset_class_MoneyMarket", {"weight": round(cash, 8)})

        for h in (s.get("top_holdings") or []):
            hs = h.get("symbol")
            if not hs:
                continue
            canon = canon_holding_ticker(hs, h.get("name") or "")
            if is_fund_type(canon):
                # 基金类持仓（货币基金等）已按上面的 HAS_CASH 处理，不再建节点
                n_fund_holdings += 1
                continue
            add_edge(fid, "CONSTITUENT", f"stock_{canon}", {"weight": h.get("weight")})

    # (3) 概念层：主题 ETF 反推的「可投资概念」
    if os.path.exists(CONCEPTS):
        cj = json.load(open(CONCEPTS, encoding="utf-8"))
        for key, c in cj.get("concepts", {}).items():
            cid = f"concept_{key}"
            add_entity(cid, "Concept", c["name"],
                       {"concept_type": c.get("concept_type"), "markets": ",".join(c.get("markets") or []),
                        "n_members": c.get("n_members")})
            if c.get("concept_type") == "stock":
                for t in c.get("members", []):
                    sid = f"stock_{t}"
                    if sid in entities:
                        add_edge(cid, "INCLUDES", sid)
            elif c.get("asset_class"):
                add_edge(cid, "IS_CLASS", f"asset_class_{c['asset_class']}")
            # 概念 ↔ 基金（概念由这些基金反推出来，天然可投）
            for f in c.get("anchor_funds", []):
                fnd = f"fund_{f['ticker']}"
                if fnd in entities:
                    add_edge(fnd, "FOCUSES_ON", cid)

    # ---- 事件 / theme 层（theme 走受控词表；theme 挂 driver_category；theme 映射到 concept）----
    vocab = json.load(open(THEME_VOCAB, encoding="utf-8")) if os.path.exists(THEME_VOCAB) else None
    tmap = json.load(open(THEME_MAP, encoding="utf-8")) if os.path.exists(THEME_MAP) else {}
    t2c = json.load(open(THEME_CONCEPT, encoding="utf-8")) if os.path.exists(THEME_CONCEPT) else {}
    themes_v = (vocab or {}).get("themes", {})

    for i, ev in enumerate(events, 1):
        if "error" in ev:
            continue
        eid = f"event_{i:03d}"
        raw_theme = ev.get("theme") or ev.get("event_type_summary") or f"event_{i}"
        acab = ev.get("asset_class") or "Equity"
        if acab not in ASSET_CLASSES:
            acab = "Equity"
        key = tmap.get(raw_theme)
        vt = themes_v.get(key) if key else None
        if vt:
            tid = f"theme_{key}"
            add_entity(tid, "Theme", vt.get("name") or key,
                       {"key": key, "driver_category": vt.get("driver_category"),
                        "aliases": "; ".join(vt.get("aliases", [])[:6])})
            dc = vt.get("driver_category")
            if dc:
                did = f"driver_category_{slug(dc)}"
                add_entity(did, "DriverCategory", dc)
                add_edge(tid, "BELONGS_TO", did)
        else:
            tid = f"theme_{slug(raw_theme)}"
            add_entity(tid, "Theme", raw_theme)
        add_entity(eid, "Event", ev.get("event_type_summary") or raw_theme,
                   {"theme": (vt or {}).get("name") or raw_theme, "asset_class": acab, "polarity": ev.get("polarity"),
                    "event_category": ev.get("event_category"), "rationale": ev.get("rationale_text"),
                    "news_title": ev.get("_news_title"), "news_source": ev.get("_from_source"),
                    "pub_date": ev.get("_pub_date")})
        add_edge(eid, "HAS_THEME", tid)
        add_edge(tid, "IMPACTS", f"asset_class_{acab}")
        for tk in (ev.get("affected_tickers") or []):
            if is_fund_type(tk):
                # 新闻直接点名了产品本身（如"半导体ETF"），挂到 Fund 上
                if f"fund_{tk}" in entities:
                    add_edge(eid, "AFFECTS", f"fund_{tk}",
                             {"polarity": ev.get("polarity"), "news_source": ev.get("company_source")})
                continue
            sid = f"stock_{tk}"
            add_entity(sid, "Stock", tk, {"ticker": tk, "asset_class": "Equity"})
            add_edge(sid, "IS_CLASS", "asset_class_Equity")
            add_edge(eid, "AFFECTS", sid,
                     {"polarity": ev.get("polarity"), "news_source": ev.get("company_source")})

    # ---- 最后一环：Theme --MAPS_TO--> Concept ----
    # 让"没点名具体公司的新闻"也能沿着 Event→Theme→Concept→(Stock 或 AssetClass)→Fund 传下去
    n_t2c = 0
    for tkey, ckeys in sorted(t2c.items()):
        tid = f"theme_{tkey}"
        if tid not in entities:
            continue
        for ck in ckeys:
            cid = f"concept_{ck}"
            if cid in entities:
                add_edge(tid, "MAPS_TO", cid, {"mapping_source": "llm"})
                n_t2c += 1

    os.makedirs(OUT_DIR, exist_ok=True)
    result = {"meta": {"generated_for": "yfinance KG (30 news events)",
                       "theme_vocab_version": (vocab or {}).get("version"),
                       "entity_types": sorted({e["type"] for e in entities.values()}),
                       "relation_types": sorted({e["relation"] for e in edges}),
                       "n_entities": len(entities), "n_edges": len(edges)},
              "entities": list(entities.values()), "edges": edges}
    json.dump(result, open(OS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 公司名 → ticker 对照表（供新闻/事件实体归一化使用）
    os.makedirs("_yf_data", exist_ok=True)
    json.dump(alias_map, open("_yf_data/company_alias.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)
    company_index = {e["id"]: {"name": e["name"], "tickers": e.get("tickers", []),
                               "aliases": e.get("aliases", [])}
                     for e in entities.values() if e["type"] == "Company"}
    json.dump(company_index, open("_yf_data/company_index.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)

    # ---- 名称自检：显示名过短 / 撞别家代码 / 重名，自动报出来 ----
    stock_owner = {e["ticker"]: e["id"] for e in entities.values()
                   if e["type"] == "Stock" and e.get("ticker")}
    seen_name, warns = {}, []
    for cid, info in sorted(company_index.items()):
        nm = info["name"]
        own = {f"stock_{t}" for t in info["tickers"]}
        if len(nm) < 3:      # 2 个字符以下基本就是剥过头了（如 KE）
            warns.append(f"名称过短: {nm}  ({cid})")
        if nm.upper() in stock_owner and stock_owner[nm.upper()] not in own:
            warns.append(f"与别的股票代码同名: {nm}  ({cid} vs {stock_owner[nm.upper()]})")
        if nm in seen_name:
            warns.append(f"公司重名: {nm}  ({seen_name[nm]} / {cid})")
        seen_name[nm] = cid
    for w in warns:
        print("[WARN]", w)

    print(f"[INFO] entities {len(entities)} | edges {len(edges)}")
    print(f"[INFO] companies {len(company_index)} | aliases {len(alias_map)} -> _yf_data/company_alias.json")
    n_theme_entities = sum(1 for e in entities.values() if e["type"] == "Theme")
    print(f"[INFO] Theme->Concept 边 {n_t2c} 条 | 有映射的 Theme {len(t2c)}/{n_theme_entities}")
    print(f"[INFO] 持仓表里的基金类条目 {n_fund_holdings} 条，已按现金处理（HAS_CASH），未建节点")
    print("[INFO] entity types:", result["meta"]["entity_types"])
    print("[INFO] relation types:", result["meta"]["relation_types"])


if __name__ == "__main__":
    main()

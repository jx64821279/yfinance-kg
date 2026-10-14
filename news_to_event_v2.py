"""
v2 新闻 → 结构化事件（按 scope 分档抽取，LLM）。

与 v1 的关键差别：
  1) 每条新闻带 scope（entity / sector / macro / market / weak），提示词按 scope 施加约束：
     macro 与 market 类**不允许**挂任何个股标的；
  2) 要求模型给出 evidence（公司名 → 标题里的片段），脚本侧做**证据校验**，
     名字在标题里找不到的标的直接丢弃并记入 evidence_failed；
  3) 抽出的公司名经 company_norm 归一到 universe，落不到的名字记入 unresolved_mentions。

输入：_yf_data/news_sample100_v2.json（或任意 news_filtered_v2_llm.json）
产出：_yf_data/events_v2_sample.json
运行（key 走环境变量）：
  set OPENAI_API_KEY=sk-...
  python news_to_event_v2.py [--news ...] [--out ...] [--limit N]
"""
import argparse
import collections
import json
import os
import re
import time

import openai

import company_norm

OUT_DIR = "_yf_data"

parser = argparse.ArgumentParser()
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--news", default=os.path.join(OUT_DIR, "news_sample100_v2.json"))
parser.add_argument("--out", default=os.path.join(OUT_DIR, "events_v2_sample.json"))
parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
parser.add_argument("--revalidate", action="store_true",
                    help="不调用模型，只对已有 --out 文件重跑证据校验与实体解析")
args, _ = parser.parse_known_args()
if not args.api_key and not args.revalidate:
    raise SystemExit("need --api_key or OPENAI_API_KEY")
openai.api_key = args.api_key
openai.api_base = args.api_base


def load_product_alias():
    """产品（ETF）别名：代码与全名 -> 代码。用于把 QQQ / GLDM 这类提及落到产品上。"""
    alias = {}
    path = os.path.join(OUT_DIR, "yf_summary.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            summ = json.load(f)
        for sym, s in (summ.get("etfs") or {}).items():
            alias[sym.strip().lower()] = sym
            nm = str(s.get("name") or "").strip().lower()
            if nm:
                alias[nm] = sym
    return alias


PRODUCT_ALIAS = load_product_alias()

# 加密货币语境：裸代码在这种上下文里不是 ETF（例：「Binance to Support QQQ Token Airdrops」）
CRYPTO_HINT = re.compile(r"\b(token|airdrop|crypto|blockchain|nft|coin|memecoin|defi|binance|stablecoin)\b", re.I)


def norm_text(s):
    """统一成"小写 + 标点换空格"，这样 JD.com 与 "JD com" 能对上。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(s).lower().replace("&amp;", "&")).strip()


# 事件域：模型偶尔自造（如 CommodityEvents），收敛到受控集合
EVENT_CATEGORIES = {"MarketEvents", "CorporateEvents", "MacroEvents", "PolicyEvents", "CommodityEvents"}


def norm_category(cat):
    c = str(cat or "").strip()
    if c in EVENT_CATEGORIES:
        return c
    if "commodit" in c.lower():
        return "CommodityEvents"
    return "MarketEvents"


def resolve_entity(name, title=""):
    """名字 -> (kind, value)：kind 取 company / product / None。"""
    name = str(name or "").strip()
    if not name:
        return None, None
    cid = company_norm.resolve(name)
    if cid:
        return "company", cid
    pk = PRODUCT_ALIAS.get(name.lower())
    if pk:
        # 加密语境 → 不认产品（例：「Binance to Support QQQ Token Airdrops」里的 QQQ 是代币）
        if CRYPTO_HINT.search(title or ""):
            return None, None
        return "product", pk
    # 部分名称：如 "Taiwan Semiconductor" -> Taiwan Semiconductor Manufacturing
    n = norm_text(name)
    if len(n) >= 8 and len(n.split()) >= 2:
        for cid2, info in company_norm._INDEX.items():
            full = norm_text(info.get("name") or "")
            if full.startswith(n) or n in full:
                return "company", cid2
    return None, None

SCOPE_NOTE = {
    "entity": "这条新闻点名了我们关注的标的。",
    "sector": "这条新闻是板块/产品线级别的。",
    "macro": "这条新闻是宏观/政策/地缘级别的：**不要**挂任何个股标的。",
    "market": "这条新闻是大盘级别的：**不要**挂任何个股标的，只描述市场层面。",
    "weak": "这条新闻来自逐标的检索，但标题未点名：只有标题里确实出现公司名时才填。",
}

PROMPT = """你是金融新闻事件抽取专家。给定一条新闻（标题/媒体/日期）及其作用范围 scope，抽取结构化事件，只输出 JSON。

要求：
1. theme：由 asset_class + 事件语义提炼的英文主题短语（如 "AI capex slowdown"、"rate cut expectations"）。
2. asset_class 只能取：Equity / FixedIncome / MoneyMarket / Commodity / FX。
3. polarity 取 positive / negative / neutral（对该资产类别的方向）。
4. event_category 取 MarketEvents / CorporateEvents / MacroEvents / PolicyEvents。
5. companies：只有当**标题原文里确实出现**该公司名（或常见简称）时才填，且用标题里的原话；
   没有就填空数组。绝对不要根据常识补充标题里没有的公司。
   只填公司或金融产品（ETF）的名称，不要填人名、政府机构、指数或与金融无关的实体。
6. evidence：companies 里每个名字对应标题里出现的那一小段原文；没有标的时给空对象。

scope 说明：{scope_note}

只输出 JSON：
{{"event_category":"","event_type":"","event_type_summary":"","theme":"","asset_class":"","polarity":"","companies":[],"evidence":{{}},"rationale_text":""}}

新闻标题：{title}
媒体：{publisher}
日期：{date}
scope：{scope}
"""


def parse_json(s):
    for f in (lambda x: json.loads(x),
              lambda x: json.loads(re.search(r"```(?:json)?\s*(.*?)\s*```", x, re.S).group(1)),
              lambda x: json.loads(re.search(r"\{.*\}", x, re.S).group(0))):
        try:
            return f(s)
        except Exception:
            pass
    return None


def extract(item):
    scope = item.get("scope", "sector")
    prompt = (PROMPT.replace("{scope_note}", SCOPE_NOTE.get(scope, SCOPE_NOTE["sector"]))
                    .replace("{title}", item.get("title", ""))
                    .replace("{publisher}", item.get("publisher", ""))
                    .replace("{date}", item.get("date", ""))
                    .replace("{scope}", scope))
    last = None
    for i in range(4):
        try:
            r = openai.ChatCompletion.create(model=args.model,
                                             messages=[{"role": "user", "content": prompt}],
                                             temperature=0.0, max_tokens=800,
                                             thinking={"type": "disabled"})
            out = parse_json(r.choices[0].message.content)
            if out:
                return out
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    return {"error": str(last)[:120] if last else "parse failed"}


def in_title(name, title):
    """名字（或其常见写法）是否出现在标题里。两侧都做同样的标点归一，2 个字符也允许匹配。"""
    t = " " + norm_text(title) + " "
    variants = {norm_text(name)}
    kind, val = resolve_entity(name, title)
    cid = val if kind == "company" else None
    if cid:
        info = company_norm._INDEX.get(cid) or {}
        variants |= {norm_text(info.get("name", ""))}
        variants |= {norm_text(a) for a in (info.get("aliases") or [])[:8]}
        variants |= {norm_text(x) for x in (info.get("tickers") or [])}
    elif kind == "product":
        variants.add(norm_text(val))
    for v in variants:
        v = (v or "").strip()
        if len(v) < 2:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", t):
            return True
    return False


def postprocess(ev, item_title, scope):
    """证据校验 + 实体解析：公司落到 company，产品落到 fund ticker。"""
    kept, failed, unresolved = [], [], []
    products = []
    for name in (ev.get("companies") or []):
        name = str(name or "").strip()
        if not name:
            continue
        if not in_title(name, item_title):
            failed.append(name)
            continue
        kind, val = resolve_entity(name, item_title)
        if kind == "company":
            kept.append(name)
        elif kind == "product":
            if val not in products:
                products.append(val)
        else:
            unresolved.append(name)
    ev["companies_kept"] = kept
    ev["evidence_failed"] = failed
    ev["unresolved_mentions"] = unresolved

    tks = []
    for name in kept:
        cid = company_norm.resolve(name)
        if cid:
            tks += company_norm.tickers(cid)
    ev["affected_tickers"] = sorted(set(tks))
    ev["affected_products"] = sorted(set(products))
    ev["event_category"] = norm_category(ev.get("event_category"))
    # scope 是 macro / market 时不允许挂标的
    if scope in ("macro", "market") and (ev["affected_tickers"] or ev["affected_products"]):
        ev["scope_violation"] = True
        ev["affected_tickers"], ev["affected_products"] = [], []
        ev["companies_kept"] = []
    return ev


def main():
    if args.revalidate:
        return revalidate()
    return extract_all()

def revalidate():
    """不调模型：对已抽取的事件重跑证据校验与实体解析（修校验规则后用它回填）。"""
    with open(args.out, encoding="utf-8") as f:
        events = json.load(f)
    out = []
    for ev in events:
        if "error" not in ev:
            ev = postprocess(ev, ev.get("_title", ""), ev.get("_scope"))
        out.append(ev)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    summary(out)
    return None


def summary(results):
    ok = [r for r in results if "error" not in r]
    by_scope = collections.Counter(r["_scope"] for r in ok)
    named = sum(1 for r in ok if r.get("affected_tickers") or r.get("affected_products"))
    failed = sum(len(r.get("evidence_failed") or []) for r in ok)
    unresolved = sum(len(r.get("unresolved_mentions") or []) for r in ok)
    violations = sum(1 for r in ok if r.get("scope_violation"))
    themes = collections.Counter(r.get("theme") for r in ok)
    print(f"[INFO] 事件 {len(ok)}/{len(results)} | 按 scope: {dict(by_scope)}")
    print(f"   点名了标的的事件: {named} | 证据校验丢弃的名字: {failed} "
          f"| 未落地到 universe 的名字: {unresolved} | macro/market 违规挂标的: {violations}")
    print(f"   不同 theme 数: {len(themes)}")


def extract_all():
    with open(args.news, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]
    if args.limit:
        items = items[:args.limit]
    print(f"[INFO] 待抽取 {len(items)} 条（来源 {os.path.basename(args.news)}）")

    results = []
    for i, it in enumerate(items, 1):
        ev = extract(it)
        ev["_title"] = it.get("title")
        ev["_date"] = it.get("date")
        ev["_publisher"] = it.get("publisher")
        ev["_scope"] = it.get("scope")
        ev["_channels"] = it.get("channels")
        ev["_sample_id"] = it.get("sample_id") or it.get("id")
        # 链接要一路带到事件层：事件 id 由它哈希而来（见 event_ids.py），
        # 同时留在图上做溯源。丢掉的话下游只能靠位置编号认事件，插入新新闻就会错位。
        ev["_link"] = it.get("link")

        if "error" not in ev:
            ev = postprocess(ev, it.get("title", ""), it.get("scope"))
        results.append(ev)

        if i % 10 == 0 or i == len(items):
            ok = sum(1 for r in results if "error" not in r)
            print(f"   {i}/{len(items)} 完成，成功 {ok}")
        time.sleep(0.3)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    summary(results)
    return None


if __name__ == "__main__":
    main()

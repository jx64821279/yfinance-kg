"""
新闻 → 结构化事件（LLM 抽取）。
输入：_yf_data/news_raw.json（yf_news.py 的产物）
输出：_yf_data/events_extracted.json
运行（key 走环境变量）：
  set OPENAI_API_KEY=sk-...
  python news_to_event.py --limit 30

公司名 / 产品名的落地方式：模型只负责把新闻里**出现过**的公司名、产品名原样写出来，
再由本脚本解析到 universe 上——公司走 company_norm（一家公司可挂多个上市代码，
京东会同时落到 9618.HK 和 JD），产品（ETF）走别名表（"SMH"、"VanEck Semiconductor ETF"
都落到 SMH）；两边都解析不了的记进 unresolved_mentions。

--reresolve：不重新调用 LLM，只对已有事件文件补跑一遍上面的解析
（脚本升级后让旧数据与新逻辑对齐用）：
  python news_to_event.py --reresolve --out _yf_data/events_extracted_all.json
"""
import os
import re
import json
import time
import argparse
import datetime
from email.utils import parsedate_to_datetime

import openai
import company_norm

parser = argparse.ArgumentParser()
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--news", default="_yf_data/news_raw.json")
parser.add_argument("--out", default="_yf_data/events_extracted.json")
parser.add_argument("--limit", type=int, default=30)
parser.add_argument("--reresolve", action="store_true",
                    help="不调用 LLM，只对 --out 里已有事件补跑公司名/产品名解析")
args, _ = parser.parse_known_args()
if not args.api_key and not args.reresolve:      # --reresolve 不调用模型，无需 key
    raise SystemExit("need --api_key or OPENAI_API_KEY")

openai.api_key = args.api_key
openai.api_base = args.api_base

UNIVERSE = ["NVDA", "TSM", "JD", "0981.HK", "000660.KS", "005930.KS",
            "SMH", "SOXX", "AIQ", "KWEB", "CQQQ", "QQQ", "XLK", "TLT", "AGG", "GLD", "USO", "UUP"]

# 产品 / 代码别名表：ticker 本身 + 基金全名（来自 yf_fetch.py 的产物）
TICKER_ALIAS = {t.upper(): t for t in UNIVERSE}
if os.path.exists("_yf_data/yf_summary.json"):
    try:
        _summ = json.load(open("_yf_data/yf_summary.json", encoding="utf-8"))
        for _sym, _s in (_summ.get("etfs") or {}).items():
            _nm = (_s.get("name") or "").strip()
            if _nm:
                TICKER_ALIAS.setdefault(_nm.upper(), _sym)
    except Exception:
        pass


def resolve_mentions(mentions):
    """把「新闻原话里的公司名 / 产品名」落到 universe 上。

    返回 (company_ids, tickers, unresolved)：公司走 company_norm，
    产品（ETF 代码或全名）走 TICKER_ALIAS，两边都不中才记入 unresolved。
    """
    comps, tks, unresolved = [], [], []
    for m in mentions or []:
        m = str(m or "").strip()
        if not m:
            continue
        cid = company_norm.resolve(m)
        if cid:
            if cid not in comps:
                comps.append(cid)
            continue
        tk = TICKER_ALIAS.get(m.upper())
        if tk:
            if tk not in tks:
                tks.append(tk)
            continue
        unresolved.append(m)
    return comps, tks, unresolved


def date_of(it):
    """统一新闻发布时间：Yahoo News 是 unix 时间戳，RSS 是 RFC822 字符串。"""
    pts = it.get("providerPublishTime")
    if pts:
        try:
            return datetime.datetime.fromtimestamp(int(pts), datetime.timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass
    raw = it.get("pubDate")
    if raw:
        try:
            return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
        except Exception:
            return str(raw)[:16]
    return None


def parse_json(s):
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def reresolve(path):
    """对已有事件文件补跑解析（不调用 LLM）。

    只把每条事件里 unresolved_mentions 中的公司名/产品名重新落到 universe 上，
    结果与"用当前脚本重跑一遍抽取"一致，但省掉一次全量 API 调用。
    同时按新闻标题回填来源（_from_source）与发布日（_pub_date）。
    """
    evs = json.load(open(path, encoding="utf-8"))

    # 标题 -> (来源, 发布日期)
    meta = {}
    if os.path.exists(args.news):
        nd = json.load(open(args.news, encoding="utf-8"))
        for _sym, blob in (nd.get("per_ticker") or {}).items():
            for _it in (blob.get("yahoo_news") or []):
                meta.setdefault((_it.get("title") or "").strip(), ("yahoo_news", date_of(_it)))
            for _it in (blob.get("yahoo_rss") or []):
                meta.setdefault((_it.get("title") or "").strip(), ("yahoo_rss", date_of(_it)))
        for _topic, lst in (nd.get("google_news") or {}).items():
            for _it in lst:
                meta.setdefault((_it.get("title") or "").strip(), ("google_news", date_of(_it)))

    n_ev, n_add, n_date = 0, 0, 0
    for ev in evs:
        if "error" in ev:
            continue
        m = meta.get((ev.get("_news_title") or "").strip())
        if m:
            ev["_from_source"], ev["_pub_date"] = m
            n_date += 1
        comps, prod_tks, unresolved = resolve_mentions(ev.get("unresolved_mentions") or [])
        if not comps and not prod_tks:
            continue
        tks = list(prod_tks)
        for cid in comps:
            tks += company_norm.tickers(cid)
        add = sorted(set(tks) - set(ev.get("affected_tickers") or []))
        if not add:
            continue
        ev["affected_tickers"] = sorted(set(ev.get("affected_tickers") or []) | set(tks))
        ev["affected_companies"] = sorted(set(ev.get("affected_companies") or []) | set(comps))
        ev["affected_products"] = sorted(set(ev.get("affected_products") or []) | set(prod_tks))
        ev["unresolved_mentions"] = unresolved
        n_ev += 1
        n_add += len(add)
    json.dump(evs, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[INFO] 补跑解析：{n_ev} 条事件新增 {n_add} 个标的；回填来源/日期 {n_date} 条 -> {path}")


def extract(item, related):
    prompt = f"""你是金融新闻事件抽取专家。给定一条新闻（标题+摘要），抽取结构化事件，只输出 JSON。

要求：
1. companies：找出新闻中**被直接影响的上市公司**，用**新闻原话**写（公司名/简称/中英文皆可，如 "NVIDIA"、"英伟达"、"京东"、"SK Hynix"、"台积电"）。不要臆造没出现的公司；没有就给空数组。
2. theme：由 asset_class + 事件语义提炼的"信号/主题"短语（如 "AI capex slowdown"、"rate cut"）。
3. asset_class 只能取：Equity / FixedIncome / MoneyMarket / Commodity / FX
4. polarity 取：positive / negative / neutral（对上述资产的方向）
5. event_category 取值参考：MarketEvents / CorporateEvents / MacroEvents / PolicyEvents

输出格式：
{{"event_category":"...","event_type":"...","event_type_summary":"...","theme":"...","asset_class":"...","polarity":"...","companies":["新闻原话里的公司名"],"rationale_text":"一句话中文概括"}}

新闻标题：{item.get('title','')}
新闻摘要：{item.get('summary','')}
（参考：行情源上的关联代码 {related}，仅供辅助判断，不要直接照抄）
"""
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


def main():
    if args.reresolve:
        return reresolve(args.out)

    data = json.load(open(args.news, encoding="utf-8"))
    # 汇总 + 去重（按标题）
    seen, items = set(), []
    for sym, blob in data.get("per_ticker", {}).items():
        for it in blob.get("yahoo_news", []):
            t = (it.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                items.append({"title": t, "summary": "", "sym": sym,
                              "related": it.get("relatedTickers") or [],
                              "publisher": it.get("publisher"), "link": it.get("link"),
                              "source": "yahoo_news", "pub_date": date_of(it)})
        for it in blob.get("yahoo_rss", []):
            t = (it.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                items.append({"title": t, "summary": it.get("summary", ""), "sym": sym,
                              "related": [], "publisher": "", "link": it.get("link"),
                              "source": "yahoo_rss", "pub_date": date_of(it)})
    # Google News（独立新闻源，非 Yahoo 数据）：按主题检索的 RSS 结果，
    # 没有 relatedTickers，所以只能靠新闻正文里的公司名/产品名落地
    for topic, lst in (data.get("google_news") or {}).items():
        for it in lst:
            t = (it.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t)
                items.append({"title": t, "summary": "", "sym": topic,
                              "related": [], "publisher": it.get("source"),
                              "link": it.get("link"), "source": "google_news",
                              "pub_date": date_of(it)})
    print(f"[INFO] 去重后新闻 {len(items)} 条，取前 {args.limit} 条抽取")
    items = items[: args.limit]

    results = []
    for i, it in enumerate(items, 1):
        ev = extract(it, it["related"])
        ev["_news_title"] = it["title"]
        ev["_from_ticker"] = it["sym"]
        ev["_from_source"] = it.get("source")
        ev["_pub_date"] = it.get("pub_date")

        # ---- 实体解析：文本抽取(高可信) / 行情源关联(低可信) 分开处理 ----
        text_comps, text_tks, unresolved = resolve_mentions(ev.get("companies") or [])
        rel_all = [m for m in (it.get("related") or []) if not str(m).startswith("^")]
        rel_comps, rel_tks, _ = resolve_mentions(rel_all)
        if text_comps or text_tks:                 # 以文本抽取为准
            used_comps, used_tks, src = text_comps, text_tks, "text"
        else:                                      # 文本什么都没抽到才退回行情源关联
            used_comps, used_tks = rel_comps, rel_tks
            src = "related" if (rel_comps or rel_tks) else "none"
        tks = list(used_tks)
        for cid in used_comps:
            tks += company_norm.tickers(cid)
        ev["company_source"] = src
        ev["affected_companies"] = used_comps
        ev["affected_products"] = sorted(set(used_tks))
        ev["affected_tickers"] = sorted(set(tks))
        ev["related_companies"] = rel_comps
        ev["unresolved_mentions"] = sorted(set(unresolved))

        results.append(ev)
        t = ev.get("theme"); ac = ev.get("asset_class"); pol = ev.get("polarity")
        aff = ev.get("affected_tickers")
        if "error" in ev:
            print(f"{i:2}. [ERR] {it['title'][:60]} -> {ev['error']}")
        else:
            print(f"{i:2}. [{ac}/{pol}] {str(t)[:26]:26} | {str(ev.get('event_type_summary'))[:30]:30} | {aff}")
        time.sleep(0.3)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ok = sum(1 for r in results if "error" not in r)
    print(f"\n[INFO] 成功 {ok}/{len(results)} -> {args.out}")


if __name__ == "__main__":
    main()

"""
公司名称归一化：把各种写法（含中文）解析到「公司节点」。
公司节点按归一化 longName 分组，一家公司可对应多个 ticker（如 GOOG/GOOGL）。

数据来源（由 build_kg_yf.py 导出）：
  _yf_data/company_alias.json : 名称 -> company id
  _yf_data/company_index.json : company id -> {name, tickers, aliases}

用法：
    from company_norm import resolve, tickers, resolve_ticker
    resolve("NVIDIA Corp")  -> "company_nvidia"
    resolve("英伟达")        -> "company_nvidia"
    tickers("company_alphabet") -> ["GOOG","GOOGL"]
"""
import os
import re
import json

DATA = "_yf_data"
ALIAS_PATH = os.path.join(DATA, "company_alias.json")
INDEX_PATH = os.path.join(DATA, "company_index.json")

# 人工补充的中文/常用别名：名称 -> ticker（再经 ticker 映射到公司）
MANUAL = {
    "英伟达": "NVDA", "辉达": "NVDA", "nvidia": "NVDA",
    "台积电": "TSM", "台積電": "TSM",
    "京东": "JD", "京東": "JD", "京东集团": "JD",
    "中芯国际": "0981.HK", "中芯國際": "0981.HK", "smic": "0981.HK",
    "海力士": "000660.KS", "sk海力士": "000660.KS", "sk hynix": "000660.KS",
    "三星": "005930.KS", "三星电子": "005930.KS", "samsung": "005930.KS",
    "博通": "AVGO", "美光": "MU", "超微": "AMD", "微软": "MSFT", "微軟": "MSFT",
    "阿里": "BABA", "阿里巴巴": "BABA", "腾讯": "0700.HK", "百度": "BIDU",
    # 贝壳找房（KE Holdings）：美股代码 BEKE，港股 2423.HK。
    # 注意：不要写 "ke" —— 它同时也是 Kimball Electronics 的代码，太容易误伤
    "beke": "2423.HK", "贝壳": "2423.HK", "贝壳找房": "2423.HK",
    # 常见缩写 / 俗名 -> ticker
    "tsmc": "TSM", "google": "GOOG", "alphabet": "GOOG", "meta": "META",
    "facebook": "META", "amazon": "AMZN", "apple": "AAPL", "tesla": "TSLA",
    "netflix": "NFLX", "oracle": "ORCL", "palantir": "PLTR", "micron": "MU",
    "broadcom": "AVGO", "qualcomm": "QCOM", "intel": "INTC", "asml": "ASML",
    "texas instruments": "TXN", "applied materials": "AMAT", "lam research": "LRCX",
    "kla": "KLAC", "analog devices": "ADI", "marvell": "MRVL", "arm": "ARM",
    "synopsys": "SNPS", "cadence": "CDNS", "corning": "GLW", "cognEX": "CGNX",
    "sandisk": "SNDK", "sndk": "SNDK", "nvidia corp": "NVDA", "tsmc adr": "TSM",
}

_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|nv|sa|ag|"
    r"group|holding|holdings|technologies|technology|the|adr|ads|ordinary|shares|class)\b", re.I)
# 只剥「纯法律形式」，用来判断某个短名字是不是真正的品牌名
_SUFFIX_LEGAL = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|nv|sa|ag|"
    r"adr|ads|ordinary|shares|class)\b", re.I)


def _key(s):
    s = str(s or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _key_legal(s):
    """只去掉法律后缀，保留 Holdings / Group / Technology 这类实义词。"""
    s = str(s or "").strip().lower()
    s = re.sub(r"[^\w\u4e00-\u9fff]+", " ", s)
    s = _SUFFIX_LEGAL.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


_ALIAS = json.load(open(ALIAS_PATH, encoding="utf-8")) if os.path.exists(ALIAS_PATH) else {}
_INDEX = json.load(open(INDEX_PATH, encoding="utf-8")) if os.path.exists(INDEX_PATH) else {}
# 短归一化键（<4 个字符）只有在「本身就是去掉法律后缀得到的」才收：
#   ABB Ltd -> abb（品牌名就是 ABB）   ✓ 收
#   KE Holdings -> ke（"KE" 是 Kimball Electronics 的代码）✗ 不收
_MIN_KEY_LEN = 4
_NORM = {}
for k, cid in _ALIAS.items():
    _k = _key(k)
    if len(_k) >= _MIN_KEY_LEN or _k == _key_legal(k):
        _NORM.setdefault(_k, cid)
# ticker -> company id（用于把中文别名经 ticker 落到公司）
_TICKER2CO = {}
for cid, info in _INDEX.items():
    for t in info.get("tickers", []):
        _TICKER2CO[t.upper()] = cid
# 注意：不做「按数字部分归一」。.KS(KOSPI) / .KQ(KOSDAQ) 是不同市场，
# 同一个 6 位码可能是两家完全不同的公司，按数字合并会误伤。


def resolve(name):
    """名称 -> 公司节点 id（解析不了返回 None）。"""
    if not name:
        return None
    s = str(name).strip()
    if s.upper() in _TICKER2CO:                 # 直接给的是 ticker
        return _TICKER2CO[s.upper()]
    if s in _ALIAS:
        return _ALIAS[s]
    if s.lower() in MANUAL:
        return _TICKER2CO.get(MANUAL[s.lower()].upper())
    k = _key(s)
    cid = _NORM.get(k)
    if cid:
        return cid
    tk = MANUAL.get(k)
    return _TICKER2CO.get(tk.upper()) if tk else None


def tickers(company_id):
    """公司节点 -> 其全部 ticker。"""
    return list((_INDEX.get(company_id) or {}).get("tickers", []))


def resolve_ticker(name):
    """名称 -> 代表性 ticker（无则 None）。"""
    ts = tickers(resolve(name) or "")
    return ts[0] if ts else None


if __name__ == "__main__":
    tests = ["NVIDIA Corp", "NVIDIA Corporation", "英伟达", "台积电", "Taiwan Semiconductor Manufacturing Co Ltd ADR",
             "JD.com, Inc.", "京东", "Alphabet Inc Class A", "GOOGL", "SK hynix Inc.", "海力士",
             "Samsung Electronics Co., Ltd.", "某不存在的公司"]
    for t in tests:
        cid = resolve(t)
        print(f"  {t:48} -> {str(cid):22} tickers={tickers(cid) if cid else []}")
    print(f"\n[INFO] companies {len(_INDEX)} | alias entries {len(_ALIAS)} | normalized keys {len(_NORM)}")

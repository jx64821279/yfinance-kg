# -*- coding: utf-8 -*-
"""跨脚本共用的口径参数。

「实质性权重」这套参数检索层（kg_subgraph.py 选事件）和决策层（customer_brief.py
聚合暴露）都要用，而且必须一致，所以抽到这里，避免两处各写一份、日后改一处忘一处。
"""

# 事件的"实质性"权重：泛泛的大盘综述不该和公司事件同权重。
# 权重只做温和折扣，最轻的档也有 0.42，避免把"大盘回调"这类对权益客户
# 真正重要的风险压没（这是 v2 时期调过一次的口径）。
W_CATEGORY = {
    "CorporateEvents": 1.0,
    "CommodityEvents": 1.0,
    "PolicyEvents": 0.9,
    "MacroEvents": 0.8,
    "MarketEvents": 0.6,
}
W_SCOPE = {"entity": 1.0, "sector": 0.9, "macro": 0.8, "market": 0.7, "weak": 0.6}

# 事件方向
SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def materiality(ev_meta):
    """事件类别 × 范围，缺省 0.5。"""
    cat = W_CATEGORY.get((ev_meta or {}).get("category"), 0.5)
    scp = W_SCOPE.get((ev_meta or {}).get("scope"), 0.5)
    return round(cat * scp, 3)


def pct_change(rows, n):
    """近 n 个交易日的涨跌幅。rows 按日期从旧到新，元素含 adj_close（缺则用 close）。

    取复权价是必须的：不复权的话，除权日会显示成假跌。
    """
    a = [(r.get("adj_close") if r.get("adj_close") is not None else r.get("close")) for r in rows]
    a = [x for x in a if x]
    if len(a) < n + 1:
        return None
    return round(a[-1] / a[-1 - n] - 1, 6)

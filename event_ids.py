# -*- coding: utf-8 -*-
"""事件 ID 的口径：用新闻链接的哈希做主键（v3 起）。

为什么改
    原来事件 id 是事件文件里的序号（event_001、event_002…），而事件文件按日期排序
    生成。往中间插一条新新闻，它后面所有事件的 id 都会平移——已经写进简报、决策里
    的 evidence（"event_806"）就指到别的事件上去了，而且看不出来错了。重抓一遍新闻
    （哪怕只是多抓到一条）也会整体错位。

口径
    id = "event_" + sha1(链接)[:8]
    链接拿不到时退化成「标题 + 日期」的哈希，统计里会单独报出来（key_sources）。

为什么用链接
    1. 只跟这条新闻本身有关：插入新事件、重排、重跑都不变
    2. 同一篇新闻在不同查询下被抓到多次，天然合并成同一个 id
    3. 顺着 id 能回到原文：链接作为 Event 节点的 link 属性保留在图上

链接从哪来
    事件文件里存的是 _sample_id（过滤后新闻的编号，如 n0001），不是链接。
    attach_links() 用 _sample_id 回 news_filtered*.json 里把链接补回来。
    news_to_event_v2.py 从这一版起在抽取时直接写 _link，新数据不用再补。

用法
    python event_ids.py --events _yf_data/events_v3_all.json
    python event_ids.py --events _yf_data/events_v3_all.json --dump-ids 5
"""

import argparse
import collections
import hashlib
import json
import os

HASH_LEN = 8
PREFIX = "event_"


def normalize_link(link):
    if not link:
        return None
    s = str(link).strip()
    return s or None


def event_id(key):
    """由任意稳定 key 得到事件 id。"""
    return PREFIX + hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:HASH_LEN]


def key_of(ev):
    """事件的稳定 key。返回 (key, 来源)，来源用于统计 id 的可靠性。"""
    link = normalize_link(ev.get("_link"))
    if link:
        return link, "link"
    title = str(ev.get("_title") or ev.get("event_type_summary") or "").strip()
    if title:
        return "%s|%s" % (title, str(ev.get("_date") or "").strip()), "title_date"
    sid = str(ev.get("_sample_id") or "").strip()
    if sid:
        return sid, "sample_id"
    raise ValueError("事件既没有链接也没有标题，无法生成稳定 id：%r" % (ev,))


def event_id_for(ev):
    key, _ = key_of(ev)
    return event_id(key)


def load_news_links(path):
    """过滤后的新闻文件 → {sample_id: link}。"""
    d = json.load(open(path, encoding="utf-8"))
    items = d["items"] if isinstance(d, dict) and "items" in d else d
    out = {}
    for it in items:
        sid = it.get("sample_id") or it.get("id")
        if sid:
            out[str(sid)] = it.get("link")
    return out


def attach_links(events, news_path=None, news=None):
    """给事件补 _link：自己带了的就用，没带的用 _sample_id 去新闻文件里查。

    就地修改 events 里的字典，返回计数 {"already": 自带, "filled": 补上, "missing": 仍缺}。
    """
    if news is None:
        news = load_news_links(news_path) if news_path and os.path.exists(news_path) else {}
    stat = {"already": 0, "filled": 0, "missing": 0}
    for ev in events:
        if normalize_link(ev.get("_link")):
            stat["already"] += 1
            continue
        sid = str(ev.get("_sample_id") or "")
        link = news.get(sid) if sid else None
        if link:
            ev["_link"] = link
            stat["filled"] += 1
        else:
            stat["missing"] += 1
    return stat


def positional_map(events):
    """老文件的位置编号 → 稳定 id 的对照表（迁移用）。"""
    return {PREFIX + "%03d" % i: event_id_for(ev) for i, ev in enumerate(events, 1)}


def audit(events):
    """自检：条数、id 唯一性、有没有撞哈希、id 是靠什么算出来的。"""
    ids = collections.Counter(event_id_for(ev) for ev in events)
    srcs = collections.Counter(key_of(ev)[1] for ev in events)
    return {"n_events": len(events), "n_unique_ids": len(ids),
            "collisions": {k: v for k, v in ids.items() if v > 1},
            "key_sources": dict(srcs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default=os.path.join("_yf_data", "events_v3_all.json"))
    ap.add_argument("--news", default=os.path.join("_yf_data", "news_filtered_v2.json"))
    ap.add_argument("--dump-ids", type=int, default=0, help="打印前 N 条：id ← 标题")
    args = ap.parse_args()

    events = json.load(open(args.events, encoding="utf-8"))
    if isinstance(events, dict):
        events = events["events"]
    stat = attach_links(events, args.news)
    au = audit(events)
    print("事件 %d 条 | 唯一 id %d 个" % (au["n_events"], au["n_unique_ids"]))
    print("  id 来源: %s" % au["key_sources"])
    print("  链接: 自带 %d | 由 _sample_id 补上 %d | 仍缺 %d"
          % (stat["already"], stat["filled"], stat["missing"]))
    if au["collisions"]:
        print("  [警告] 撞哈希 %d 处: %s" % (len(au["collisions"]), list(au["collisions"])[:5]))
    for ev in events[:args.dump_ids]:
        print("  %s ← %s" % (event_id_for(ev), (ev.get("_title") or "")[:70]))


if __name__ == "__main__":
    main()

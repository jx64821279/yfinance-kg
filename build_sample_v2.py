"""
从复核后的 v2 语料里抽一个分层小样本，用于验证抽取效果（默认 100 条）。

各档配额（按 scope）：entity 30 / sector 30 / macro 20 / market 15 / weak 5
抽样是确定性的（固定随机种子），market 档尽量分散在不同日期。

输入：_yf_data/news_filtered_v2_llm.json
产出：_yf_data/news_sample100_v2.json
运行：python build_sample_v2.py [--n 100] [--seed 20260929]
"""
import argparse
import collections
import json
import os
import random

OUT_DIR = "_yf_data"
IN = os.path.join(OUT_DIR, "news_filtered_v2_llm.json")
OUT = os.path.join(OUT_DIR, "news_sample100_v2.json")

QUOTA = {"entity": 30, "sector": 30, "macro": 20, "market": 15, "weak": 5}

parser = argparse.ArgumentParser()
parser.add_argument("--in", dest="src", default=IN)
parser.add_argument("--out", default=OUT)
parser.add_argument("--seed", type=int, default=20260929)
args, _ = parser.parse_known_args()


def main():
    with open(args.src, encoding="utf-8") as f:
        items = json.load(f)["items"]
    by_scope = collections.defaultdict(list)
    for x in items:
        by_scope[x["scope"]].append(x)

    rnd = random.Random(args.seed)
    sample, picked = [], {}
    for scope, quota in QUOTA.items():
        pool = by_scope.get(scope, [])
        if scope == "market":
            # 按日期分组，轮转取样，避免集中在同一天
            by_day = collections.defaultdict(list)
            for x in pool:
                by_day[x.get("date")].append(x)
            days = sorted(by_day)
            rnd.shuffle(days)
            take = []
            i = 0
            while len(take) < min(quota, len(pool)) and days:
                day = days[i % len(days)]
                if by_day[day]:
                    take.append(by_day[day].pop(rnd.randrange(len(by_day[day]))))
                else:
                    days.remove(day)
                    continue
                i += 1
        else:
            take = rnd.sample(pool, min(quota, len(pool))) if pool else []
        picked[scope] = len(take)
        sample.extend(take)

    sample.sort(key=lambda x: (x.get("date") or "", x["scope"], x["title"]))
    for n, x in enumerate(sample, 1):
        x["sample_id"] = f"s{n:03d}"

    out = {"meta": {"source": os.path.basename(args.src), "seed": args.seed,
                    "quota": QUOTA, "picked": picked, "total": len(sample)},
           "items": sample}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[INFO] 样本 {len(sample)} 条 -> {args.out}")
    print("   实际抽取:", picked)
    print("   发布日期跨度:", min(x['date'] for x in sample if x.get('date')),
          "~", max(x['date'] for x in sample if x.get('date')))


if __name__ == "__main__":
    main()

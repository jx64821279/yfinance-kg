"""
构建受控主题词表（冷启动 v1）。
两轮归并：
  pass1 分批把原始 theme 归并成"分块规范主题"
  pass2 全局归并成最终规范主题表，并给每个主题分配 driver_category
产出：
  _yf_data/theme_vocab.json  {version, driver_categories, themes{key:{name,driver_category,members,aliases,n_events}}}
  _yf_data/theme_map.json    {原始 theme -> 规范 key}
运行（key 走环境变量）：
  set OPENAI_API_KEY=sk-...
  python build_theme_vocab.py
"""
import os
import re
import json
import time
import argparse
import hashlib
from collections import Counter

import openai

parser = argparse.ArgumentParser()
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
parser.add_argument("--events", default="_yf_data/events_extracted_all.json")
parser.add_argument("--chunk", type=int, default=60)
parser.add_argument("--cache", default="_yf_data/theme_vocab_pass1.json")
args, _ = parser.parse_known_args()
if not args.api_key:
    raise SystemExit("need --api_key or OPENAI_API_KEY")
openai.api_key = args.api_key
openai.api_base = args.api_base

DRIVER = ["Supply", "Demand", "Revenue", "Efficiency/Cost", "Strategic Action",
          "Technology/Innovation", "Policy/Regulation", "Macro & FX",
          "Geopolitics", "Market Flow", "Industry Cycle"]


def parse_json(s):
    for f in (lambda x: json.loads(x),
              lambda x: json.loads(re.search(r"```(?:json)?\s*(.*?)\s*```", x, re.S).group(1)),
              lambda x: json.loads(re.search(r"\{.*\}", x, re.S).group(0))):
        try:
            return f(s)
        except Exception:
            pass
    return None


def call(prompt, max_tokens=8000):
    for i in range(4):
        try:
            r = openai.ChatCompletion.create(model=args.model,
                                             messages=[{"role": "user", "content": prompt}],
                                             temperature=0.0, max_tokens=max_tokens,
                                             thinking={"type": "disabled"})
            out = parse_json(r.choices[0].message.content)
            if out:
                return out
        except Exception as e:
            print(f"   [retry {i+1}] {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2 * (i + 1))
    return None


def main():
    events = json.load(open(args.events, encoding="utf-8"))
    cnt = Counter(ev.get("theme", "").strip() for ev in events if ev.get("theme"))
    cnt.pop("", None)
    uniq = sorted(cnt)
    print(f"[INFO] 事件 {len(events)} | 去重 theme {len(uniq)}")

    # ---------- pass1：分批归并 ----------
    # 缓存与输入绑定：指纹变了就重做，避免复用上一批事件留下的过期归并结果
    fp = hashlib.md5("\n".join(uniq).encode("utf-8")).hexdigest()[:12]
    groups = None
    if os.path.exists(args.cache):
        try:
            cached = json.load(open(args.cache, encoding="utf-8"))
        except Exception:
            cached = None
        if isinstance(cached, dict) and cached.get("fingerprint") == fp:
            groups = cached.get("groups") or []
            print(f"[INFO] pass1 读缓存（输入指纹 {fp} 一致）: {len(groups)} 组")
        else:
            print(f"[INFO] pass1 缓存已过期（输入指纹 {fp}），重新归并")
    if groups is None:
        groups = []
        chunks = [uniq[i:i + args.chunk] for i in range(0, len(uniq), args.chunk)]
        for ci, ch in enumerate(chunks, 1):
            prompt = f"""你是金融新闻主题归并专家。下面是一批英文主题标签（来自新闻事件抽取，语义相近的很多）。
请把它们归并成更少的「规范主题」：
1) 同一语义的不同措辞要合并（如 "AI capex slowdown" 与 "AI capex slowdown risk"）；
2) 规范主题名用简洁英文短语；
3) 必须覆盖全部输入标签，不能遗漏。
只输出 JSON：{{"canonical":[{{"name":"...","members":["原始标签1","原始标签2"]}}]}}

输入标签：
""" + "\n".join(f"- {t}" for t in ch)
            out = call(prompt)
            got = (out or {}).get("canonical", [])
            print(f"  pass1 {ci}/{len(chunks)}: {len(ch)} -> {len(got)}")
            groups += got
            time.sleep(0.5)
        json.dump({"fingerprint": fp, "n_themes": len(uniq), "groups": groups},
                  open(args.cache, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # ---------- pass2：全局归并 + 分配 driver_category ----------
    listing = "\n".join(f"- {g.get('name')}" for g in groups if g.get("name"))
    prompt = f"""下面是从上一轮归并得到的规范主题清单。请做一次「全局归并」，得到最终的规范主题表（目标 40-60 个），
并给每个主题分配一个 driver_category（必须从下列列表中选择其一）：
{", ".join(DRIVER)}

要求：
1) 语义相同的合并；
2) key 用英文下划线短 id；name 用简洁规范名（可含中文注解）；
3) 必须覆盖清单里的全部主题，不能遗漏。
只输出 JSON：{{"themes":[{{"key":"...","name":"...","driver_category":"...","members":["上一轮主题名"]}}]}}

上一轮规范主题：
{listing}
"""
    out = call(prompt, max_tokens=12000)
    finals = (out or {}).get("themes", [])
    print(f"[INFO] pass2: {len(groups)} -> {len(finals)} 个规范主题")

    # ---------- 建立 原始theme -> 最终 key 的映射 ----------
    name2key = {}
    for f in finals:
        for m in f.get("members", []):
            name2key[str(m).strip()] = f["key"]
    theme_map, unmapped = {}, []
    for g in groups:
        key = name2key.get(str(g.get("name")).strip())
        for raw in g.get("members", []):
            if key:
                theme_map[raw] = key
            else:
                unmapped.append(raw)
    for t in uniq:                      # 兜底：直接同名命中
        if t not in theme_map and t in name2key:
            theme_map[t] = name2key[t]

    vocab = {
        "version": 1,
        "driver_categories": DRIVER,
        "themes": {f["key"]: {"name": f.get("name"), "driver_category": f.get("driver_category"),
                              "aliases": sorted({r for r, k in theme_map.items() if k == f["key"]}),
                              "n_events": sum(cnt.get(r, 0) for r, k in theme_map.items() if k == f["key"])}
                   for f in finals},
        "stats": {"raw_themes": len(uniq), "canonical_themes": len(finals),
                  "mapped": len(theme_map), "unmapped": len(set(uniq) - set(theme_map))},
    }
    json.dump(vocab, open("_yf_data/theme_vocab.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(theme_map, open("_yf_data/theme_map.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)
    print(f"[INFO] vocab {len(finals)} 个主题 -> _yf_data/theme_vocab.json")
    print(f"[INFO] 映射 {len(theme_map)}/{len(uniq)} 条（{len(set(uniq) - set(theme_map))} 条未映射）")
    missing = sorted(set(uniq) - set(theme_map))
    if missing:
        print("  未映射样例:", missing[:8])


if __name__ == "__main__":
    main()

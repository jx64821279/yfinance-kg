"""
把人工补丁（_yf_data/theme_merge_fix.json）应用到主题词表：
  ① 原始主题 → theme_map 增加映射；
  ② 对应规范主题的 aliases 里补进这个原始主题；
  ③ 按事件文件重算 n_events；④ 更新 stats 与 version。

幂等：重复运行结果一致。不改任何原始数据，只改词表产物。

运行：python apply_theme_fixes.py --suffix _v2
"""
import argparse
import collections
import json
import os

OUT_DIR = "_yf_data"

parser = argparse.ArgumentParser()
parser.add_argument("--suffix", default="_v2")
parser.add_argument("--events", default=None, help="默认 <suffix> 对应的事件文件")
parser.add_argument("--fix", default=os.path.join(OUT_DIR, "theme_merge_fix.json"))
args, _ = parser.parse_known_args()

SUF = args.suffix
VOCAB_PATH = os.path.join(OUT_DIR, f"theme_vocab{SUF}.json")
MAP_PATH = os.path.join(OUT_DIR, f"theme_map{SUF}.json")
EVENTS_PATH = args.events or os.path.join(OUT_DIR, f"events{SUF}_all.json")


def main():
    vocab = json.load(open(VOCAB_PATH, encoding="utf-8"))
    theme_map = json.load(open(MAP_PATH, encoding="utf-8"))
    events = json.load(open(EVENTS_PATH, encoding="utf-8"))
    fixes = json.load(open(args.fix, encoding="utf-8"))["merge"]

    applied, skipped = [], []
    for raw, key in fixes.items():
        if key not in vocab["themes"]:
            skipped.append((raw, key, "规范主题不存在"))
            continue
        if theme_map.get(raw) == key and raw in vocab["themes"][key]["aliases"]:
            skipped.append((raw, key, "已应用"))
            continue
        theme_map[raw] = key
        al = set(vocab["themes"][key]["aliases"])
        al.add(raw)
        vocab["themes"][key]["aliases"] = sorted(al)
        applied.append((raw, key))

    # 重算 n_events（按 aliases 统计事件数）
    cnt = collections.Counter(ev.get("theme") for ev in events if ev.get("theme"))
    for key, th in vocab["themes"].items():
        th["n_events"] = sum(cnt.get(a, 0) for a in (th["aliases"] + [th["name"]]))

    raw_themes = set(cnt)
    vocab["version"] = "2.1"
    vocab["stats"] = {"raw_themes": len(raw_themes), "canonical_themes": len(vocab["themes"]),
                      "mapped": len(theme_map), "unmapped": len(raw_themes - set(theme_map)),
                      "manual_fix_applied": len(applied)}
    vocab.setdefault("manual_fixes", []).append(
        {"source": os.path.basename(args.fix), "applied": len(applied)})

    json.dump(vocab, open(VOCAB_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(theme_map, open(MAP_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2, sort_keys=True)

    print(f"[INFO] 应用 {len(applied)} 条修正（跳过 {len(skipped)}）")
    for raw, key in applied:
        print(f"   {raw[:52]:54} -> {vocab['themes'][key]['name']}")
    print(f"[INFO] 词表 version={vocab['version']} | 规范主题 {len(vocab['themes'])} | "
          f"映射 {len(theme_map)}/{len(raw_themes)}（未映射 {len(raw_themes - set(theme_map))}）")


if __name__ == "__main__":
    main()

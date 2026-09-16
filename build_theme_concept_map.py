"""
建立「新闻主题 Theme → 可投资概念 Concept」的映射（多对多）。
这一步让"没点名公司的新闻"也能传播：Event → Theme → Concept → Stock → Fund
产出：_yf_data/theme_concept_map.json  {theme_key: [concept_key, ...]}
运行（key 走环境变量）：python build_theme_concept_map.py
"""
import os
import re
import json
import time
import argparse

import openai

parser = argparse.ArgumentParser()
parser.add_argument("--api_base", default="https://api.deepseek.com")
parser.add_argument("--api_key", default=os.getenv("OPENAI_API_KEY", ""))
parser.add_argument("--model", default="deepseek-v4-flash-vision-exp")
args, _ = parser.parse_known_args()
if not args.api_key:
    raise SystemExit("need --api_key or OPENAI_API_KEY")
openai.api_key = args.api_key
openai.api_base = args.api_base


def parse_json(s):
    for f in (lambda x: json.loads(x),
              lambda x: json.loads(re.search(r"```(?:json)?\s*(.*?)\s*```", x, re.S).group(1)),
              lambda x: json.loads(re.search(r"\{.*\}", x, re.S).group(0))):
        try:
            return f(s)
        except Exception:
            pass
    return None


def main():
    vocab = json.load(open("_yf_data/theme_vocab.json", encoding="utf-8"))["themes"]
    concepts = json.load(open("_yf_data/concepts.json", encoding="utf-8"))["concepts"]

    themes_txt = "\n".join(f"- {k}: {v['name']}" for k, v in vocab.items())
    concepts_txt = "\n".join(
        f"- {k}: {v['name']}（类型={v.get('concept_type')}, 资产类别={v.get('asset_class') or '-'}, 市场={','.join(v.get('markets') or []) or '-'}）"
        for k, v in concepts.items())

    prompt = f"""你有两组金融主题标签，请判断它们之间的映射关系（多对多）。

A. 新闻主题 Theme（来自新闻抽取）：
{themes_txt}

B. 可投资概念 Concept（由主题 ETF 定义）：
{concepts_txt}

要求：
1. 只有当语义明确相关时才建立映射；不确定就不要连（宁可少连，不要乱连）；
2. 一个 Theme 可以映射到 0~3 个 Concept；
3. 只能用上面给出的 Concept key；
4. 特别注意：宏观类 Theme（利率、美元、黄金、原油、债券）应映射到对应的资产型 Concept（us_long_treasury / us_agg_bond / gold / oil / usd_index）；科技/行业类 Theme 映射到股票型 Concept。

只输出 JSON：{{"mapping": {{"theme_key": ["concept_key", ...]}}}}
"""
    out = None
    for i in range(4):
        try:
            r = openai.ChatCompletion.create(model=args.model,
                                             messages=[{"role": "user", "content": prompt}],
                                             temperature=0.0, max_tokens=6000,
                                             thinking={"type": "disabled"})
            out = parse_json(r.choices[0].message.content)
            if out:
                break
        except Exception as e:
            print(f"  [retry {i+1}] {type(e).__name__}: {str(e)[:80]}")
            time.sleep(2 * (i + 1))
    if not out:
        raise SystemExit("LLM 未返回可用结果")

    raw = out.get("mapping", {})
    clean, dropped = {}, []
    for t, cs in raw.items():
        if t not in vocab:
            dropped.append(t); continue
        cs = [c for c in (cs or []) if c in concepts]
        if cs:
            clean[t] = cs
    json.dump(clean, open("_yf_data/theme_concept_map.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, sort_keys=True)

    print(f"[INFO] Theme {len(vocab)} 个 → 有映射 {len(clean)} 个（无映射 {len(vocab)-len(clean)}）")
    if dropped:
        print("  忽略的未知 theme:", dropped[:5])
    print("\n映射明细:")
    for t, cs in sorted(clean.items()):
        print(f"  {vocab[t]['name'][:24]:26} → {[concepts[c]['name'] for c in cs]}")
    unmapped = [vocab[t]["name"] for t in vocab if t not in clean]
    print(f"\n未映射的主题（{len(unmapped)}）:", unmapped[:14])


if __name__ == "__main__":
    main()

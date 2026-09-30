# 知识图谱 demo：事件 → 主题 → 概念 → 产品 → 客户

本仓库是一个可运行的 demo 工程：**用公开市场数据构建知识图谱，并在这张图上接入客户**，
覆盖从"一条新闻"到"对某个客户意味着什么"的完整链路。
代码、数据、图谱结果、文档都在仓库内，目录内部使用相对路径（`_yf_data/`、`src/kg/`、`data/`），
**直接在本目录运行脚本即可**，不需要改任何路径。

内容分三块：

| 部分 | 做什么 | 主要产物 |
|---|---|---|
| **v1 基础链路** | 抓公开市场数据，建出 `事件 → 主题 → 概念 → 公司股票 → 基金（产品）` | `src/kg/kg_yf.json`（436 事件 / 626 节点 / 1358 边） |
| **v2 数据质量改造** | 新闻改为"宽流抓取 + 分层过滤"，事件扩到 1031 条，资产类别不再几乎全是权益 | `src/kg/kg_yf_v2.json`（1265 节点 / 1805 边） |
| **客户层** | 以客户为对象、用知识层判断外部事件对他的影响，产出客户简报与行动建议 | `data/customer_demo/`（20 个合成客户 + 20 份简报） |

## 建议先读

| 文档 | 内容 |
|---|---|
| [docs/demo_说明.md](docs/demo_说明.md) | **先看这份**：整体做了什么、结论是什么、有哪些设计取舍、已知限制 |
| [docs/yf_kg_data.md](docs/yf_kg_data.md) | 数据来源、字段含义、规模与已知问题（v1 口径） |
| [docs/yf_kg_architecture.md](docs/yf_kg_architecture.md) | 管线架构、每个脚本的职责、图谱 schema（v1 口径） |

## 目录

| 路径 | 内容 |
|---|---|
| `*.py` | 22 个管线脚本 |
| `_yf_data/` | 中间数据与数据产物（JSON） |
| `src/kg/` | 知识图谱 `kg_yf.json`（v1）与 `kg_yf_v2.json`（v2），及对应的交互式可视化 HTML |
| `data/customer_demo/` | 客户层：合成客户数据（CSV）与 20 份客户简报（JSON） |
| `docs/` | 说明文档 |

## 脚本一览

**数据抓取层**（只用标准库，需要联网）

| 脚本 | 作用 | 产出 |
|---|---|---|
| `yf_smoke.py` | 数据可用性探测 | `yf_smoke_report.json` |
| `yf_fetch.py` | 基金与股票数据（持仓、资产配置） | `yf_raw.json`、`yf_summary.json` |
| `yf_names.py` | 名称与 `quoteType` | `yf_names.json` |
| `yf_news.py` | 新闻（Yahoo News + RSS + Google News） | `news_raw.json` |
| `yf_biz.py` | 公司业务描述（**未完成**，Yahoo 限流） | `yf_biz.json` |
| `edgar_sic.py` | SEC 官方行业码 | `edgar_sic.json` |

**v2 新闻层**（数据质量改造，前四个不调模型）

| 脚本 | 作用 | 产出 |
|---|---|---|
| `yf_news_v2.py` | 三通道新闻抓取（wide / topic / entity） | `news_raw_v2.json`、`news_v2_stats.json` |
| `news_filter.py` | 时间窗 + 相关性分级 + 去重（纯规则） | `news_filtered_v2.json`、`news_quality_report_v2.json` |
| `news_scope_llm.py` | 对 market 桶与未命中桶做 LLM 批量复核 | `news_filtered_v2_llm.json`、`scope_llm_v2.json` |
| `build_sample_v2.py` | 分层抽样（默认 100 条） | `news_sample100_v2.json` |
| `news_to_event_v2.py` | v2 事件抽取（scope 约束 + 证据校验 + 实体解析） | `events_v2_all.json` |

**抽取与词表层**（调用 LLM）

| 脚本 | 作用 | 产出 |
|---|---|---|
| `news_to_event.py` | v1：新闻 → 结构化事件 | `events_extracted*.json` |
| `build_theme_vocab.py` | 主题受控词表（两轮归并），`--suffix _v2` 出 v2 词表 | `theme_vocab.json` / `theme_vocab_v2.json` |
| `apply_theme_fixes.py` | 词表人工补丁（把漏归并的原始主题并入规范主题） | `theme_map_v2.json` |
| `build_theme_concept_map.py` | 主题 → 可投资概念，`--suffix _v2` 对应 v2 | `theme_concept_map*.json` |

**图谱构建层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `company_norm.py` | 公司名归一化解析器（模块） | — |
| `build_concepts.py` | 概念层骨架 | `concepts.json` |
| `build_kg_yf.py` | 主建图脚本，`--suffix _v2` 建 v2 图 | `src/kg/kg_yf*.json`、`company_alias/index.json` |

**检索层与客户层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `exposure_v2.py` | 事件 → 产品曝光度打分（独立于图，参数可调） | `exposure_v2.json` |
| `gen_customer_demo.py` | 生成 20 个合成客户（数据写死，可复现） | `data/customer_demo/customers_all_in_one.csv` |
| `customer_brief.py` | 客户简报：暴露地图 + 需求缺口 + 时机与避雷（可选调 LLM 生成行动建议） | `data/customer_demo/briefs/*.json` |

**展示层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `kg_to_pyvis.py` | 交互式可视化（浏览器打开，可拖拽缩放） | `src/kg/kg_yf*.html` |

## 运行方式

数据已经在本仓库里，**只想重建图谱的话跑最后几步就够了**。

### 1) 重建 v1 图谱

```powershell
cd yfinance
$env:PYTHONIOENCODING='utf-8'

python build_kg_yf.py     # 建图（同时导出公司索引）
python build_concepts.py  # 概念层（依赖上一步的公司索引）
python build_kg_yf.py     # 用概念层重建图
python kg_to_pyvis.py --kg src/kg/kg_yf.json --out src/kg/kg_yf.html
```

### 2) 重建 v2 图谱（需要 LLM key 的只有词表与映射两步）

```powershell
python build_theme_vocab.py --suffix _v2 --api_key sk-...
python apply_theme_fixes.py
python build_theme_concept_map.py --suffix _v2 --api_key sk-...
python build_kg_yf.py --suffix _v2
python kg_to_pyvis.py --kg src/kg/kg_yf_v2.json --out src/kg/kg_yf_v2.html
```

### 3) 客户层（前三步不调模型）

```powershell
python exposure_v2.py                                   # 事件 → 产品曝光度
python gen_customer_demo.py                             # 生成合成客户数据
python customer_brief.py --all --outdir data/customer_demo/briefs
python customer_brief.py --customer C009 --llm --api_key sk-...   # 加 LLM 行动建议
```

### 4) 从头抓数据并重跑抽取（需要联网 + LLM key）

```powershell
python yf_fetch.py; python yf_names.py; python yf_news.py; python edgar_sic.py
$env:OPENAI_API_KEY='sk-...'
python news_to_event.py; python build_theme_vocab.py; python build_theme_concept_map.py
python build_kg_yf.py; python build_concepts.py; python build_kg_yf.py

# v2 新闻层：抓取 → 过滤 → scope 复核 → 抽取
python yf_news_v2.py; python news_filter.py; python news_scope_llm.py
python build_sample_v2.py; python news_to_event_v2.py --news <抽样文件> --out <事件文件>
```

## 环境

- Python 3.13
- 第三方库见 `requirements.txt`，一条命令装好：`pip install -r requirements.txt`
  - `openai==0.28.0`：LLM 相关步骤用，**必须锁 0.28.x**（脚本用的是旧接口 `openai.ChatCompletion.create`）
  - `pyvis`：交互式可视化用
  - `yfinance`：只有 `yf_smoke.py` 这个探测脚本用；抓取与建图都不依赖它——它的请求会被 Yahoo 风控拦截，本管线改用自建直连客户端（见 `docs/yf_kg_data.md` 4.1 节）
- LLM：DeepSeek `deepseek-v4-flash-vision-exp`，key 走命令行参数或环境变量 `OPENAI_API_KEY`

## 说明

- 图谱数据来自公开市场数据（Yahoo Finance / SEC EDGAR / Google News RSS），抓取时间见 `docs/yf_kg_data.md`；**客户数据是合成的**，仅用于跑通链路。
- 需要调用 LLM 的步骤：事件抽取、主题词表、主题→概念映射，以及客户简报里的行动建议；其余步骤都可以离线复算。
- 重跑抓取会覆盖 `_yf_data/` 下的同名文件，若要保留某次快照请先备份。

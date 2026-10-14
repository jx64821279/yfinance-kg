# 知识图谱 demo：事件 → 概念 → 产品 → 客户

本仓库是一个可运行的 demo 工程：**用公开市场数据构建知识图谱，并在这张图上接入客户**，
覆盖从"一条新闻"到"对某个客户意味着什么"的完整链路。
代码、数据、图谱结果、文档都在仓库内，目录内部使用相对路径（`_yf_data/`、`src/kg/`、`data/`），
**直接在本目录运行脚本即可**，不需要改任何路径。

内容分三块：

| 部分 | 做什么 | 主要产物 |
|---|---|---|
| **v1 基础链路** | 抓公开市场数据，建出 `事件 → 主题 → 概念 → 公司股票 → 基金（产品）` | `src/kg/kg_yf.json`（436 事件 / 626 节点 / 1358 边） |
| **v2 数据质量改造** | 新闻改为"宽流抓取 + 分层过滤"，事件扩到 1031 条，资产类别不再几乎全是权益 | `src/kg/kg_yf_v2.json`（1265 节点 / 1805 边） |
| **v2 客户接入** | 以客户为对象、用知识层判断外部事件对他的影响 | `data/customer_demo/`（20 个合成客户） |
| **v3 主题层重构 + 检索 / 决策层** | 主题改成「概念 × 驱动」的组合；图谱入库；新增"取客户子图"的检索层与出判断的决策层；加入价格；再补一条反向查询：**一条新闻进来 → 受影响的客户** | `src/kg/kg_yf_v3.json`（1316 节点 / 2590 边）、`data/kg_v3.db`、`_yf_data/decisions/` |

## 建议先读

| 文档 | 内容 |
|---|---|
| [docs/demo_说明_v3.md](docs/demo_说明_v3.md) | **先看这份**：v3 做了什么（主题层重构、检索层、决策层、价格）、有哪些取舍与限制 |
| [docs/demo_说明.md](docs/demo_说明.md) | v2：数据质量改造 + 客户接入 |
| [docs/yf_kg_data.md](docs/yf_kg_data.md) | 数据来源、字段含义、规模与已知问题（v1 口径） |
| [docs/yf_kg_architecture.md](docs/yf_kg_architecture.md) | 管线架构、每个脚本的职责、图谱 schema（v1 口径） |

## 目录

| 路径 | 内容 |
|---|---|
| `*.py` | 31 个管线脚本 |
| `_yf_data/` | 中间数据与数据产物（JSON） |
| `src/kg/` | 知识图谱 `kg_yf.json`（v1）、`kg_yf_v2.json`（v2）、`kg_yf_v3.json`（v3），及对应的交互式可视化 HTML |
| `data/kg_v3.db` | v3 的 SQLite 库：知识层 + 客户层 + 价格（曝光度按需现算，不入库） |
| `data/customer_demo/` | 客户层：合成客户数据（CSV）与客户简报（JSON） |
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
| `classify_theme_v3.py` | **v3 主题层**：给每条事件选「概念 + 驱动」（两个封闭集合分类，可断点续跑） | `theme_assign_v3.json`、`events_v3_all.json` |

**图谱构建层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `company_norm.py` | 公司名归一化解析器（模块） | — |
| `build_concepts.py` | 概念层骨架 | `concepts.json` |
| `build_kg_yf.py` | 主建图脚本，`--suffix _v2` / `--suffix _v3` 分别建 v2 / v3 图 | `src/kg/kg_yf*.json`、`company_alias/index.json` |
| `event_ids.py` | 事件 id 口径：`sha1(新闻链接)[:8]`，链接回填与自检（模块） | — |

**v3 检索层与决策层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `exposure_v2.py` | 事件 → 产品曝光度打分（模块 `ExposureModel`，被检索层与反向查询按需调用；也兼命令行壳子） | —（不再落盘；要快照可 `--out`） |
| `gen_customer_demo.py` | 生成 20 个合成客户（数据写死，可复现） | `data/customer_demo/customers_all_in_one.csv` |
| `yf_prices.py` | 抓 13 只 ETF 日线收盘价（锚定 as_of、用复权价） | `prices_v3.json` |
| `build_kg_db.py` | 把 KG + 客户数据 + 价格装进 SQLite | `data/kg_v3.db` |
| `kg_subgraph.py` | **检索层**：取客户子图，含 top-k 有界选取（默认正负两头对称取） | `_yf_data/subs/*.json` |
| `news_impact.py` | **反向查询**：一条新闻（事件）→ 受影响的产品 → 持有它们的客户；支持单条与批量 | 控制台 + `--out` JSON |
| `customer_brief.py` | **事实层**：确定性事实清单（概念暴露 / 市场层面 / 需求缺口 / 时机 / 持仓近一周表现） | `data/customer_demo/briefs_v3*/` |
| `customer_decision.py` | **决策层**：子图 + 简报 → 模型决策（每条结论挂事件证据，自动自检越界） | `_yf_data/decisions/*.json` |
| `kg_common.py` | 检索层与决策层共用的口径参数（模块） | — |

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

### 3) 生成合成客户数据（不调模型）

```powershell
python gen_customer_demo.py    # 20 个合成客户（暴露度不预先算，见第 4 节）
```

### 4) v3 全链路：主题标注 → 建图 → 入库 → 检索 → 事实 → 决策

```powershell
# 主题标注（调 LLM，可断点续跑）
python classify_theme_v3.py --api_key sk-...
# 建 v3 图谱
python build_kg_yf.py --suffix _v3
python kg_to_pyvis.py --kg src/kg/kg_yf_v3.json --out src/kg/kg_yf_v3.html
# 抓价格（需要能访问 Yahoo）→ 建库
python yf_prices.py
python build_kg_db.py --suffix _v3     # 曝光度不在库里，检索时现算
# 检索层：取客户子图
python kg_subgraph.py --db data/kg_v3.db --all --outdir _yf_data/subs
# 事实层：每个客户一份简报（走子图；也可以不加 --subgraph，直接读文件）
foreach ($f in Get-ChildItem _yf_data/subs -File) { python customer_brief.py --subgraph $f.FullName --outdir data/customer_demo/briefs_v3_sub }
# 决策层（调 LLM）
python customer_decision.py --subdir _yf_data/subs --briefs data/customer_demo/briefs_v3_sub --api_key sk-...
# 反向：一条新闻影响哪些客户（事件已在图里 / 或给一条新新闻）
python news_impact.py --event event_5a09d11b
#    新新闻：默认直接入库（抽事件 + 标概念/驱动，2 次模型调用，再重建图与库约 1.5 秒）
python news_impact.py --title "…" --date 2026-09-30 --url "…" --api_key sk-...
#    一批新新闻：--news <文件>；只看影响不写文件加 --preview
```

### 5) 从头抓数据并重跑抽取（需要联网 + LLM key）

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
- 需要调用 LLM 的步骤：事件抽取、v2 的主题词表与主题→概念映射、v3 的主题标注（`classify_theme_v3.py`）、决策层（`customer_decision.py`）；其余步骤都可以离线复算。
- v3 把「事实」和「判断」分开了：`customer_brief.py` 只出确定性的事实清单，判断在 `customer_decision.py`（每条结论都要挂事件证据，并自动自检越界）。
- v3 有两套入口，用的是同一套打分口径：**客户入口**（`kg_subgraph.py`：一个客户 → 他的持仓 → 哪些事件影响到他）和**事件入口**（`news_impact.py`：一条新闻 → 受影响的产品 → 持有它们的客户）。事件入口只算"直接持有"，不含"关注过但没持有"的客户。
- 事件 id 是**新闻链接的哈希**（`event_ids.py`），所以往图里插新事件不会让已有事件 id 平移；新加进来的新闻单独放 `_yf_data/events_v3_extra.json`，建图时按 id 去重合并，全量重跑也不会丢。
- `_yf_data/subs/`（客户子图）是检索层的中间产物，不在仓库里，按第 4 节的命令一条就能重新生成。
- 重跑抓取会覆盖 `_yf_data/` 下的同名文件，若要保留某次快照请先备份。

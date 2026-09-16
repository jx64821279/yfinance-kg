# yfinance 知识图谱：项目架构

本文档说明"用公开市场数据构建知识图谱"这一版（下称 yfinance 版）的整体架构、每个脚本的职责、图谱 schema 和运行方式。
数据来源与字段说明见 [yf_kg_data.md](yf_kg_data.md)。

> 与数据说明一致：**这是一份演示（demo）工程**，规模较小（13 只 ETF、6 家事件公司、436 条事件），重点是把链路跑通并说清每一步在做什么。

---

## 1. 总览

目标链路：`事件 → 主题 → 概念 → 公司股票 → 基金（产品）`

```
                         ┌─────────── 数据抓取层（无需 LLM）───────────┐
  Yahoo Finance  ──►  yf_fetch.py    ──►  yf_summary.json   （基金持仓、资产配置）
                 ──►  yf_names.py    ──►  yf_names.json     （名称、quoteType）
                 ──►  yf_news.py     ──►  news_raw.json     （新闻）
  SEC EDGAR      ──►  edgar_sic.py    ──►  edgar_sic.json   （官方行业码）
                         └──────────────────────────────────────────────┘
                                          │
                         ┌─────────── 抽取与词表层（LLM）───────────────┐
  news_raw.json  ──►  news_to_event.py          ──►  events_extracted_all.json
                 ──►  build_theme_vocab.py      ──►  theme_vocab.json / theme_map.json
                 ──►  build_theme_concept_map.py──►  theme_concept_map.json
                         └──────────────────────────────────────────────┘
                                          │
                         ┌─────────── 图谱构建层 ──────────────────────┐
  company_norm.py（公司名解析器，供概念层使用）
  build_kg_yf.py      ──►  src/kg/kg_yf.json + company_alias/index.json
  build_concepts.py   ──►  concepts.json（概念骨架，依赖上一步的公司索引）
  build_kg_yf.py（再跑一次，把概念层并进图）
                         └──────────────────────────────────────────────┘
                                          │
                         ┌─────────── 展示层 ──────────────────────────┐
  kg_to_pyvis.py      ──►  src/kg/kg_yf.html（可拖拽/缩放的交互图）
                         └──────────────────────────────────────────────┘
```

**设计要点**：抓取层与抽取层分离——抓下来的原始数据落盘后，后面的步骤可以反复重跑而不用重新联网；LLM 只在"新闻抽事件""主题归并""主题映射概念"三处使用，其余全部可确定性复算。

---

## 2. 目录结构

```
<本目录>/
├── README.md                   目录说明与运行入口
├── docs/                       本文档与数据说明
├── _yf_data/                   yfinance 管线中间数据与结果（JSON）
├── yf_smoke.py                 数据可用性探测
├── yf_fetch.py                 基金 / 股票数据抓取
├── yf_names.py                 名称与类型抓取
├── yf_news.py                  新闻抓取
├── yf_biz.py                   公司业务描述抓取（未完成）
├── edgar_sic.py                SEC 行业分类抓取
├── news_to_event.py            新闻 → 事件（LLM）
├── build_theme_vocab.py        受控主题词表（LLM）
├── build_theme_concept_map.py  主题 → 概念映射（LLM）
├── company_norm.py             公司名归一化解析器
├── build_concepts.py           概念层骨架
├── build_kg_yf.py              主建图脚本
├── kg_to_pyvis.py              交互式可视化
└── src/kg/
    ├── kg_yf.json              yfinance 版知识图谱
    └── kg_yf.html              交互式可视化
```

> 本管线只使用 `src/kg/kg_yf.json` 与 `src/kg/kg_yf.html`。目录下若还出现其它 KG 产物（`kg_v2.*`、`kg_extended.*` 等），属于早期另一条已停止维护的路线，与本管线无关。

---

## 3. 脚本说明

### 3.1 数据抓取层

**`yf_smoke.py`** — 数据可用性探测，不建图。
输入：无；输出：`_yf_data/yf_smoke_report.json`。
先看 Yahoo 到底能给出哪些字段（尤其是基金层的持仓与资产配置），确认可行后再写正式抓取。这一步用的是 yfinance 库，也正是当时遇到限流、取不到数据的那一步（经过见数据说明 4.1 节）。

**`yf_fetch.py`** — 抓基金与股票数据，是整个管线的数据入口。
输入：无（universe 写在脚本里的 `ETFS` / `STOCKS` 常量）；输出：`yf_raw.json`（原始返回）、`yf_summary.json`（精简摘要）。
基金层取 `fundProfile` + `topHoldings`：名称、家族、类别、各资产类别仓位、前十大持仓；股票层取 `assetProfile` + `price`。
因为 yfinance 库被 Yahoo 风控拦截（`curl_cffi` 指纹问题），这里改用 `urllib` 自建客户端：先取 cookie，再取 `crumb`，然后带 `crumb` 请求接口。

**`yf_names.py`** — 补拉每个 ticker 的规范名称。
输入：无；输出：`yf_names.json`（69 个 ticker 全部命中）。
字段：`longName` / `shortName` / `quoteType` / `exchange` / `currency`。
作用有两个：一是给公司归一化提供"权威名称"，二是指出哪些 ticker 根本不是股票（`quoteType` 为 ETF / MUTUALFUND / MONEYMARKET）。

**`yf_news.py`** — 抓新闻，也是事件的唯一来源。
输入：无；输出：`news_raw.json`。
三路抓取：Yahoo 逐 ticker 的新闻搜索接口、Yahoo RSS、Google News RSS（按主题检索）。抓取时把每只产品也当作关键词，因此能抓到"半导体 ETF""中概股"这类没有具体公司名的新闻。

**`yf_biz.py`** — 补拉公司业务描述（`longBusinessSummary` / `sector` / `industry` / `country`）。
输入：`yf_names.json`；输出：`yf_biz.json`（**未产出**）。
原计划用于"公司自述 → 业务主题 → 扩充概念成员"，弥补"只有前十大持仓"导致的成员缺失。抓取时 Yahoo 已全线限流（403），未跑完。

**`edgar_sic.py`** — 用 SEC EDGAR 的官方数据给股票打行业标签。
输入：无；输出：`edgar_sic.json`（31/69 命中，只有美股有）。
字段：`cik`、`entityName`、`sic`、`sicDescription`。这是"官方行业分类"，比 Yahoo 的行业字段更权威。

### 3.2 抽取与词表层（LLM）

三个脚本都用 DeepSeek（`deepseek-v4-flash-vision-exp`），API key 走环境变量 `OPENAI_API_KEY`，base 为 `https://api.deepseek.com`。

**`news_to_event.py`** — 把新闻抽成结构化事件。
输入：`news_raw.json`（三路新闻：Yahoo News / Yahoo RSS / Google News RSS）；输出：`events_extracted.json`（小样本）或 `events_extracted_all.json`（全量）。
三条来源都会写入事件的 `_from_source`，并带进图谱的 Event 节点属性（`news_source`），便于区分或过滤。
每条事件包含：事件域、事件类型、中文摘要、原始主题、影响的资产类别、方向（positive/negative/neutral）、影响到的标的、判断理由、来源新闻。
**实体落地方式**：模型只负责把新闻里**真的出现过**的公司名、产品名原样写出来，脚本再解析到 universe——公司走 `company_norm`（一家公司可挂多个上市代码，京东会同时落到 `9618.HK` 和 `JD`），产品（ETF）走别名表（`"SMH"` 与 `"VanEck Semiconductor ETF"` 都落到 SMH）；两边都解析不了的记进 `unresolved_mentions`。新闻原文抽不到时才退回到行情源的关联代码（`company_source` 字段标明来源）。
用 `--limit N` 可以先跑小样本验证；`--reresolve` 不调用模型，只对已有事件文件重跑一遍实体解析，并按新闻标题回填来源与发布日期。

**`build_theme_vocab.py`** — 构建受控主题词表（冷启动 v1）。
输入：`events_extracted_all.json`；输出：`theme_vocab.json`、`theme_map.json`、`theme_vocab_pass1.json`。
两轮归并：第一轮分批把原始主题归并成"分块规范主题"，第二轮全局归并成 30 个规范主题并给每个主题分配驱动类别（覆盖 397/400 个原始主题）。
第一轮结果按**输入指纹**缓存：事件主题集合一变，缓存自动失效重算，避免复用上一批事件留下的过期结果。
设计原则是**主题表示话题、方向交给 polarity**（"AI 资本开支放缓/加速"归为同一主题）。驱动类别沿用 FinKario 原 `driven_categories.yaml` 的框架并补充三类。

**`build_theme_concept_map.py`** — 建立"主题 → 可投资概念"的多对多映射。
输入：`theme_vocab.json` + `concepts.json`；输出：`theme_concept_map.json`。
这是让"没有点名公司的新闻"也能传导到产品的关键一步：宏观/政治类主题映射到资产型概念，科技/行业类主题映射到股票型概念。提示词里明确要求"不确定就不要连"，宁可少连。

### 3.3 图谱构建层

**`company_norm.py`** — 公司名归一化解析器，供概念层与后续新闻接入使用。
输入：`company_alias.json` + `company_index.json`（由 `build_kg_yf.py` 导出）；输出：无（模块）。
提供 `resolve(name)` → 公司 id、`tickers(cid)` → 该公司全部代码。
内置两类护栏：不做"按代码数字归一"（`.KS` / `.KQ` 是不同市场）；过短的归一化键（如把 `KE Holdings` 折成 `ke`，会撞上 Kimball Electronics 的代码）不予收录。

**`build_kg_yf.py`** — 主建图脚本，产出最终图谱。
输入：`events_extracted_all.json`、`yf_summary.json`、`yf_names.json`、`theme_vocab.json`、`theme_map.json`、`theme_concept_map.json`、`concepts.json`、`edgar_sic.json`；输出：`src/kg/kg_yf.json`、`company_alias.json`、`company_index.json`。
职责：
1. 建 Stock 节点，并按归一化公司名归到 Company 节点（一家公司可挂多个代码）；
2. 建 Fund 节点与前十大持仓边，基金类持仓按现金处理（`HAS_CASH`）；
3. 并进概念层与行业层；
4. 建 Event / Theme / DriverCategory 层，并加入"主题 → 概念"边；
5. 导出公司索引供后续步骤使用；
6. 跑名称自检（显示名过短、撞别家代码、公司重名会告警）。

**`build_concepts.py`** — 构建概念层骨架。
输入：`yf_summary.json`、`edgar_sic.json`、`yf_names.json`、`src/kg/kg_yf.json`（取基金资产类别）；输出：`concepts.json`。
两种概念：股票型（从主题 ETF 反推成员，走"概念 → 股票 → 基金"）和资产型（走"概念 → 资产类别 → 基金"）。
两条关键规则：解析持仓代码时**名称优先、代码兜底**（Yahoo 有错误条目）；概念成员**按公司展开**（同一家公司的其他上市代码也算成员，如京东的 `9618.HK` 与 `JD`）。

**`kg_to_pyvis.py`** — 生成交互式可视化。
输入：任意 KG JSON；输出：HTML。
用 pyvis / vis-network，支持拖拽、缩放、按节点类型筛选、物理布局开关、可拖动图例；悬停显示节点属性与关系说明（纯文本，含中文标签）。

---

## 4. 图谱 Schema

### 4.1 节点（9 类，626 个）

| 类型 | 数量 | 含义 | 关键属性 |
|---|---|---|---|
| Event | 436 | 从新闻抽出的事件 | `event_category`、`theme`、`asset_class`、`polarity`、`rationale`、`news_source`、`news_title` |
| Theme | 33 | 受控主题（30 个规范主题 + 3 个原始主题兜底） | `key`、`driver_category`、`aliases` |
| DriverCategory | 9 | 主题的驱动类别 | — |
| Concept | 12 | 可投资概念 | `concept_type`（stock/asset）、`markets`、`n_members` |
| Stock | 53 | 上市代码 | `ticker`、`asset_class` |
| Company | 51 | 公司（可挂多个代码） | `tickers[]`、`aliases[]` |
| Industry | 14 | 行业（EDGAR SIC） | `sic`、`standard` |
| Fund | 13 | 产品（ETF） | `ticker`、`family`、`category`、`asset_class`、`positions` |
| AssetClass | 5 | 权益/固收/货币/大宗/外汇 | — |

### 4.2 关系（12 类，1358 条）

| 关系 | 数量 | 起点 → 终点 | 含义 |
|---|---|---|---|
| `HAS_THEME` | 436 | Event → Theme | 事件的主题 |
| `AFFECTS` | 479 | Event → Stock / Fund | 事件直接影响的标的（点名 ETF 时直连产品） |
| `BELONGS_TO` | 30 | Theme → DriverCategory | 主题所属驱动类别 |
| `MAPS_TO` | 39 | Theme → Concept | 主题对应的可投资概念 |
| `IMPACTS` | 42 | Theme → AssetClass | 主题影响的资产类别 |
| `CONSTITUENT` | 80 | Fund → Stock | 基金持有成分股（带 `weight`） |
| `HAS_CASH` | 12 | Fund → AssetClass:MoneyMarket | 产品里的现金类资产占比 |
| `IS_CLASS` | 71 | Stock / Fund / Concept → AssetClass | 属于哪类资产 |
| `CORRESPONDS_TO` | 53 | Company → Stock | 公司对应的上市代码 |
| `IN_INDUSTRY` | 28 | Stock → Industry | 所属行业 |
| `INCLUDES` | 75 | Concept → Stock | 概念包含的成分股 |
| `FOCUSES_ON` | 13 | Fund → Concept | 基金聚焦的概念 |

### 4.3 主链

```
事件 ──HAS_THEME──► 主题 ──MAPS_TO──► 概念 ──INCLUDES──► 股票 ◄──CONSTITUENT── 基金
                    │                                        ▲
                    └──IMPACTS──► 资产类别 ◄──IS_CLASS───────┘
事件 ──AFFECTS──────────────────────────────────────────► 股票 / 基金
公司 ──CORRESPONDS_TO──► 股票       股票 ──IN_INDUSTRY──► 行业
```

两条到产品的路径：

- **点名了公司的新闻**：`Event --AFFECTS--> Stock <--CONSTITUENT-- Fund`（一跳）；
- **没点名公司的新闻**：`Event --HAS_THEME--> Theme --MAPS_TO--> Concept --INCLUDES--> Stock <--CONSTITUENT-- Fund`。

实测覆盖：436 条事件中 **429 条**能走到产品，13 只产品全部被触达。

---

## 5. 运行顺序

```powershell
cd <本目录>
$env:PYTHONIOENCODING='utf-8'

# 第一次跑（或重新抓数据）
python yf_fetch.py
python yf_names.py
python yf_news.py
python edgar_sic.py

$env:OPENAI_API_KEY='sk-...'
python news_to_event.py
python build_theme_vocab.py
python build_theme_concept_map.py

# 只改了实体解析、不需要重跑模型时（结果与重跑一致）
python news_to_event.py --reresolve --out _yf_data/events_extracted_all.json

# 建图（注意顺序：先建图导公司索引，再建概念层，最后重建图）
python build_kg_yf.py
python build_concepts.py
python build_kg_yf.py

python kg_to_pyvis.py --kg src/kg/kg_yf.json --out src/kg/kg_yf.html
```

**为什么 `build_kg_yf.py` 要跑两次**：概念层的成员解析依赖"公司索引"（`company_index.json`），而索引是建图时导出的；概念层写出的 `concepts.json` 又要在建图时读进来。所以顺序是"建图 → 概念层 → 建图"。

已经拿到 `_yf_data/` 里的数据时，可以跳过抓取与抽取步骤，直接从 `build_kg_yf.py` 开始。

---

## 6. 环境与依赖

- **Python**：3.13
- **第三方库**：`openai==0.28.0`（LLM 调用）、`pyvis`（可视化）；抓取层只用标准库（`urllib`、`http.cookiejar`）
- **不依赖 yfinance 库**：yfinance 会被 Yahoo 风控拦截，本管线改用自建直连客户端
- **网络**：抓取层需要访问 Yahoo Finance、Google News、SEC EDGAR
- **API Key**：`OPENAI_API_KEY`（DeepSeek），base `https://api.deepseek.com`，model `deepseek-v4-flash-vision-exp`

---

## 7. 几个必须遵守的约定

1. **ETF / 共同基金 / 货币基金只建 Fund 节点，不建 Stock**；新闻点名 ETF 时直接 `AFFECTS → Fund`。
2. **基金持仓代码按公司规范代码落点**，避免 Yahoo 的错误条目（如 `005930.KQ`）把同一家公司拆成两个节点。
3. **持仓表里的货币基金不建实体**，只表达为 `Fund --HAS_CASH--> MoneyMarket`，这样货架清单干净、货币型节点也不空挂。
4. **边的属性不能用 `source` / `relation` / `target` 这三个保留键**，否则会覆盖边的起点（建图脚本里已有保护，但新增关系命名时仍需注意）。
5. **公司名清洗只从末尾剥后缀**，剥完如果不足 4 个字符就把最后一个实义词还回来（否则 `KE Holdings` 会变成 `KE`）。
6. **事件里的实体先落地、再建边**：公司名走 `company_norm`（一家公司可挂多个上市代码），产品名走别名表；解析不到的名称进 `unresolved_mentions`，不要硬塞进 `affected_tickers`。

---

## 8. 怎么扩展

**加新产品（ETF）**：把代码加进 `yf_fetch.py` 的 `ETFS`、`yf_news.py` 的 `TICKERS`，重跑抓取；在 `build_concepts.py` 的 `ETF_CONCEPT` 里登记它定义的概念（或映射到已有概念）；然后按标准顺序重建。产品会自动获得"产品→成分股""产品→概念""产品→资产类别"三类边。

**加新公司**：如果它已经在某只 ETF 的前十大里，抓取时会自动带出；如果只在新闻里出现，就把它加进 `yf_news.py` 的关键词，事件抽取会自动对齐到 universe。公司名归一化会按 `longName` 自动分组，同一家公司的多个上市代码不需要手工处理。

**加新新闻**：`yf_news.py` 抓取 → `news_to_event.py` 抽事件 → 重跑 `build_theme_vocab.py`（新主题需要归并且词表升版本）→ `build_theme_concept_map.py` → 重建图。

---

## 9. 与原 FinKario 项目的关系

yfinance 版是**独立的一条路线**，复用的是 FinKario 的知识组织思路，不是它的代码：

- **沿用**：事件的字段结构（事件域 / 类型 / 摘要 / 影响标的 / 方向）；主题与驱动类别的分层（`driver_category` 沿用原 `driven_categories.yaml` 框架）；"用知识图谱承载事件到产品的传导"这一整体目标。
- **新写**：全部数据抓取脚本、事件抽取、主题词表、概念层、建图与可视化脚本（即本文档第 3 节列出的文件）。
- **未包含**：原 FinKario 的研报语料管线（`refinement.py`、`attribute_knowledge_extraction.py`、`triple_extraction.py`、`build_kg.py`、`FinKario-RAG.py` 等）针对的是"研报 → 三元组"的场景，与本管线不通用，因此**不在本目录内**；本目录只包含上面第 3 节列出的脚本与 `_yf_data/` 数据。

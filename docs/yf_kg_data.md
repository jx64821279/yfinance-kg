# yfinance 知识图谱：数据来源与数据说明

本文档说明"用公开市场数据构建知识图谱"这一版（下称 yfinance 版）用了哪些数据、字段含义、数据规模和已知问题。
对应的构建流程见 [yf_kg_architecture.md](yf_kg_architecture.md)。

> **这是一份演示（demo）工程**：数据规模刻意控制得较小——13 只 ETF（产品）、6 家事件公司、436 条新闻事件——目的是把 `事件 → 主题 → 概念 → 公司股票 → 基金（产品）` 这条链路完整跑通，并展示其中的设计取舍与数据细节，**不代表生产级的数据规模**。数据全部为公开数据，抓取时间见第 1 节。

---

## 1. 概述

yfinance 版的产品侧（基金）与公司侧（股票）数据**全部来自公开数据源**，不使用原 FinKario 项目的东方财富研报语料：

| 数据 | 来源 | 用途 |
|---|---|---|
| 基金画像、前十大持仓、资产配置 | Yahoo Finance `quoteSummary` | 产品定义、产品→成分股 |
| 名称、`quoteType`、交易所 | Yahoo Finance `quoteSummary` | 实体归一化、区分股票/基金 |
| 新闻 | Yahoo News API + RSS + Google News RSS | 事件的唯一来源 |
| 行业分类 | SEC EDGAR（官方 SIC） | 行业层 |

**数据抓取时间**：2026-09-15（Yahoo 行情、持仓、名称）；新闻覆盖 2026-06-18 ~ 2026-09-14。

---

## 2. 产品与标的的选择

数据入口是 yfinance（Yahoo Finance）的公开接口，产品货架与标的范围是这样定的：

**产品货架用美股主题 ETF。** 每只 ETF 都能直接取到持仓明细（前十大成分股）与资产配置（股票/债券/现金/其他各占多少），后者还能用来判定它属于哪一类资产。这批 13 只 ETF 覆盖了权益、固收、大宗商品、货币、外汇五类，可以同时支撑"基金 → 成分股"和"基金 → 资产类别"两种落点。

**没有采用 A 股 ETF。** A 股 ETF 取不到持仓和资产类别字段，产品侧建不起来，所以货架只放在美股。

**事件来源没有采用东财个股新闻。** 这类新闻与单只股票绑定过紧，多是该股自身的消息，很难支撑"事件 → 行业 → 公司"这种跨层传导；因此事件改从更宽的市场新闻里抽，具体见 4.2 节。

---

## 3. Universe（69 个 ticker）

### 3.1 产品货架：13 只 ETF

| 代码 | 名称 | 类别 | 主资产 | 定义的概念 |
|---|---|---|---|---|
| SMH | VanEck Semiconductor ETF | Technology | 股票 100% | 半导体 |
| SOXX | iShares Semiconductor ETF | Technology | 股票 100% | 半导体 |
| AIQ | Global X Artificial Intelligence & Technology ETF | Technology | 股票 100% | 人工智能 |
| BOTZ | Global X Robotics & Artificial Intelligence ETF | Technology | 股票 100% | 机器人与自动化 |
| KWEB | KraneShares CSI China Internet ETF | Greater China Region | 股票 100% | 中概互联网 |
| CQQQ | Invesco China Technology ETF | Greater China Region | 股票 100% | 中国科技 |
| QQQ | Invesco QQQ Trust | Large Growth | 股票 100% | 纳斯达克100/大盘成长 |
| XLK | State Street Technology Select Sector SPDR ETF | Technology | 股票 100% | 美国科技 |
| TLT | iShares 20+ Year Treasury Bond ETF | Long Government | 债券 100% | 美国长期国债 |
| AGG | iShares Core U.S. Aggregate Bond ETF | Intermediate Core Bond | 债券 97% | 美国综合债券 |
| GLD | SPDR Gold Shares | Commodities Focused | 其他（实物黄金）100% | 黄金 |
| USO | United States Oil Fund, LP | Commodities Focused | 其他（原油期货）49% | 原油 |
| UUP | Invesco DB US Dollar Index Bullish Fund | Trading--Miscellaneous | 现金 100% | 美元指数 |

这 13 只覆盖了五类资产：权益、固收、大宗商品、货币、外汇。

### 3.2 事件公司：6 家

`JD`（京东）、`NVDA`（英伟达）、`TSM`（台积电）、`0981.HK`（中芯国际）、`000660.KS`（SK 海力士）、`005930.KS`（三星电子）。

这 6 家是全球科技/半导体/中概方向的核心标的，作为新闻事件的锚点。

### 3.3 其余 ticker

由 13 只 ETF 的前十大持仓自动带出。最终图谱里共有 **53 只股票**，其中：

- **50 只**被至少一只 ETF 持有；
- **3 只**只在新闻事件里出现、不被任何 ETF 持有（`JD`、`0981.HK`、`000660.KS`）。

---

## 4. 数据来源明细

### 4.1 Yahoo Finance（自建直连客户端）

**为什么不直接用 yfinance 库（实际踩到的坑）**：最早确实是用 yfinance 库写的（`yf_smoke.py` 就是那时留下的可用性探测脚本），但调用 `Ticker.info`、`funds_data.top_holdings` 时**始终取不到数据**，报的是限流错误：

```
yfinance.exceptions.YFRateLimitError: Too Many Requests. Rate limited. Try after a while.
```

换版本（1.7.0、0.2.66 都试过）、隔一段时间重试，结果一样。但同一时间、同一台机器上，用普通的 `urllib` 直接请求 Yahoo 的同一个接口却能正常返回 200——说明不是我们的出口 IP 被限流，而是 **yfinance 发出的请求特征被 Yahoo 拦了**：它内部用 `curl_cffi` 模拟 Chrome 的 TLS 指纹，反而更容易被风控识别。

所以最后改成**自建直连客户端**：先访问 `fc.yahoo.com` 拿 cookie，再取 `crumb`，之后带着 `crumb` 请求 `quoteSummary`（见 `yf_fetch.py` / `yf_names.py`）。这套方式此后一直可用。

> 补充一句：Yahoo 后来也出现过一次全线限流，连直连方式也返回 403，"公司业务描述"那一步就是在那时没跑完的（见第 8 节第 7 条）。

| 请求模块 | 取到的字段 | 用途 | 产物 |
|---|---|---|---|
| `quoteSummary?modules=fundProfile,topHoldings` | 基金名称、家族、`category`、`fundProfile`、各资产类别仓位 `stockPosition/bondPosition/cashPosition/otherPosition`、前十大持仓 `{symbol, name, weight}` | 定义产品、生成"产品→成分股"边 | `yf_raw.json`、`yf_summary.json` |
| `quoteSummary?modules=price` | `longName`、`shortName`、`quoteType`、`exchangeName`、`currency` | 公司名归一化、区分股票/ETF/货币基金 | `yf_names.json` |
| `quoteSummary?modules=assetProfile` | `sector`、`industry`、`country`、`longBusinessSummary` | 补充概念成员（**未完成**，Yahoo 限流） | `yf_biz.json`（缺） |
| `v1/finance/search`（新闻） | `title`、`publisher`、`link`、`providerPublishTime` | 事件来源 | `news_raw.json` |

### 4.2 新闻（三路抓取）

事件表（原计划的结构化 xlsx）拿不到，因此新闻是事件的唯一来源：

| 来源 | 抓取方式 | 字段 |
|---|---|---|
| Yahoo News | 逐 ticker 搜索 API | `title`、`publisher`、`link`、`providerPublishTime` |
| Yahoo RSS | 逐 ticker 订阅 | 标题、摘要、时间、链接 |
| Google News RSS | 按主题检索（NVIDIA / Taiwan Semiconductor / SK Hynix Samsung / China internet stocks） | `title`、`source`、`pubDate`、`link` |

合计 **627 条原始新闻**：Yahoo News 195 条 + Yahoo RSS 312 条 + Google News 120 条。按标题去重后 **436 条**全部进入 LLM 抽取，抽出 **436 条事件**。

> **来源说明**：前两路来自 Yahoo Finance；Google News 是独立的第三方新闻聚合源（免费 RSS，按主题检索）。三条来源都写进了事件的 `_from_source` 字段，也带到了图谱的 Event 节点属性上（`news_source`），需要"只看 Yahoo"时按这个字段过滤即可。

### 4.3 SEC EDGAR（行业层）

用官方 SIC 码为股票打行业标签，命中 **31/69**（只有美股公司有，港股/韩股/日股/A股没有）。

字段：`cik`、`entityName`、`sic`、`sicDescription`。例如 `AAPL → 3571 Electronic Computers`。

---

## 5. 数据产物清单（`_yf_data/`）

| 文件 | 内容 | 条数 | 生成脚本 |
|---|---|---|---|
| `yf_raw.json` | 逐 ticker 的原始返回（13 只 ETF + 6 家事件公司） | 19 | `yf_fetch.py` |
| `yf_summary.json` | 精简摘要：`etfs{}` / `stocks{}` | 13 + 6 | `yf_fetch.py` |
| `yf_names.json` | 每个 ticker 的规范名称与类型 | 69 | `yf_names.py` |
| `news_raw.json` | 三路原始新闻 | 627 | `yf_news.py` |
| `yf_smoke_report.json` | 可用字段探测报告 | — | `yf_smoke.py` |
| `edgar_sic.json` | 官方行业码 | 31 | `edgar_sic.py` |
| `events_extracted.json` | 事件（30 条小样本） | 30 | `news_to_event.py` |
| `events_extracted_all.json` | 事件（全量，建图用这个） | 436 | `news_to_event.py` |
| `theme_vocab.json` | 受控主题词表 v1 | 30 | `build_theme_vocab.py` |
| `theme_map.json` | 原始主题 → 规范主题映射 | 397 | `build_theme_vocab.py` |
| `theme_vocab_pass1.json` | 第一轮归并的中间结果（带输入指纹） | 263 组 | `build_theme_vocab.py` |
| `theme_concept_map.json` | 主题 → 可投资概念 | 21 | `build_theme_concept_map.py` |
| `concepts.json` | 概念骨架（成员、锚定基金） | 12 | `build_concepts.py` |
| `company_alias.json` | 名称 → 公司 id | 148 | `build_kg_yf.py` |
| `company_index.json` | 公司 id → 名称/代码/别名 | 51 | `build_kg_yf.py` |

最终图谱：`src/kg/kg_yf.json`（数据）、`src/kg/kg_yf.html`（交互可视化）。

---

## 6. 关键字段字典

### 6.1 事件（`events_extracted_all.json`）

| 字段 | 含义 | 示例 |
|---|---|---|
| `event_category` | 事件域 | `CorporateEvents` |
| `event_type` | 事件类型（英文短语） | `earnings beat` |
| `event_type_summary` | 事件摘要（中文） | `Okta财报超预期并上调指引，股价大涨` |
| `theme` | 原始主题（未归一） | `identity security demand strength` |
| `asset_class` | 影响的资产类别 | `Equity` |
| `polarity` | 方向 | `positive` / `negative` / `neutral` |
| `affected_tickers` | 影响到的标的（限定在 universe 内，可能是股票代码，也可能是产品代码） | `["NVDA"]`、`["9618.HK","JD"]`、`["SMH"]` |
| `affected_companies` | 落到的公司 id | `["company_nvidia"]` |
| `affected_products` | 落到的产品（ETF）代码 | `["SMH","SOXX"]` |
| `company_source` | 标的从哪来：`text` 新闻原文 / `related` 行情源关联 / `none` 都没抽到 | `text` |
| `unresolved_mentions` | 提到了但不在 universe 里的名称 | `["Okta"]` |
| `rationale_text` | 判断理由 | — |
| `_news_title` / `_from_ticker` | 来源新闻标题 / 抓取入口（ticker 或主题） | — |
| `_from_source` / `_pub_date` | 新闻来源（`yahoo_news` / `yahoo_rss` / `google_news`）与发布日 | `yahoo_rss` |

### 6.2 主题词表（`theme_vocab.json`）

| 字段 | 含义 |
|---|---|
| `version` | 词表版本（当前 v1） |
| `driver_categories` | 驱动类别候选表 |
| `themes{key}` | 规范主题：`name`、`driver_category`、`members`（归并进来的原始主题）、`aliases`、`n_events` |

**驱动类别**以 FinKario 原项目的 `driven_categories.yaml`（8 类）为底，按新闻实际需要补了 `Geopolitics`、`Market Flow`、`Industry Cycle` 三类，共 11 类候选；这批新闻里实际用到 9 类（`Efficiency/Cost`、`Supply` 未出现）。

第一轮归并结果带**输入指纹**：事件主题集合一变，缓存自动失效并重新归并，避免复用上一批事件留下的过期结果。

### 6.3 概念（`concepts.json`）

| 字段 | 含义 |
|---|---|
| `concept_type` | `stock`（走概念→股票→基金）或 `asset`（走概念→资产类别→基金） |
| `members` | 成分股（股票型概念） |
| `member_weights` | 成分股在锚定基金里的权重 |
| `anchor_funds` | 定义这个概念的 ETF |
| `markets` | 成分股市场分布（US/HK/CN/JP/KR/CH） |

---

## 7. 数据规模

**图谱**：626 个节点 / 1358 条边。

| 节点类型 | 数量 |
|---|---|
| Event | 436 |
| Theme | 33（30 个规范主题 + 3 个未归并到词表的原始主题兜底） |
| Stock | 53 |
| Company | 51 |
| Industry | 14 |
| Fund | 13 |
| Concept | 12 |
| DriverCategory | 9 |
| AssetClass | 5 |

**事件来源**：Yahoo News 158 条 + Yahoo RSS 158 条 + Google News 120 条 = 436 条。

**事件分布**：资产类别 Equity 360 / FixedIncome 31 / Commodity 33 / FX 12；方向 positive 175 / negative 152 / neutral 109；436 条中 **278 条**点名了具体标的（181 条来自新闻原文，97 条来自行情源关联，158 条两者都没有）。

**主题**：400 个原始主题归并为 30 个规范主题（覆盖 397/400，剩 3 个按原始主题兜底成节点），其中 21 个能映射到可投资概念——但这 21 个主题**覆盖了 98% 的事件**，没有概念映射的都是单条事件的宇宙外话题（大麻、减肥药、比特币、汽车维修指引等）。

**概念**：12 个，7 个股票型（半导体 13 只成员、人工智能 11、中概互联网 11、其余各 10）+ 5 个资产型（黄金、原油、美国长期国债、美国综合债券、美元指数）。

**事件可达性**：436 条事件中 **429 条**能沿 `事件 → 主题 → 概念 → 股票 → 产品`（或直接边）走到产品，13 只产品全部被触达；到不了的 7 条是既没点名任何公司、主题也没有对应产品的新闻。

---

## 8. 口径说明与已知问题

1. **权重是小数**：`weight = 0.2262537` 表示 22.63%。可视化与文档里统一换算成百分比显示。
2. **只有前十大持仓**：Yahoo 的免费接口只给每只 ETF 的前十大持仓，因此 8 只股票型 ETF 各只有 10 条"产品→成分股"边（合计 80 条）。这些 ETF 实际持有的通常是二三十只，覆盖瓶颈在持仓数据的深度上。
3. **`quoteType` 分布**：EQUITY 53、ETF 13、MONEYMARKET 2、MUTUALFUND 1。其中：
   - MONEYMARKET 的两条（`AGPXX`、`BISXX`）是 ETF 持仓表里的现金类资产，**不建实体**，只表达为产品的现金占比；
   - MUTUALFUND 那条是 `005930.KQ`——Yahoo 把 KOSPI 上的三星错标成了基金条目（`shortName` 里带晨星基金 ID `0P0000B2XZ`），已按名称归一到 `005930.KS`，不做"按代码数字归一"。
4. **名称字段缺失**：`005930.KQ`、`BABA` 等少数 ticker 的 `longName` 为空，这类代码无法参与公司归一化，会单独成点。
5. **事件已带发布日**：436 条事件全部带 `_pub_date`（Yahoo News 的 unix 时间戳与 RSS 的 RFC822 时间都归一到 `YYYY-MM-DD`），日期范围 2025-10-03 ~ 2026-09-14；图谱上对应 Event 节点的 `pub_date` 属性，可按时间切片。
6. **新闻含宇宙外内容**：新闻里有一部分与 universe 无关（如 Cintas 财报、Affirm 评级、比特币 ETF、NHL ETF），抽成事件后有 **7 条**既没点名任何公司、主题也没有对应产品，因此走不到产品。这是新闻相关性过滤的问题，不是图谱结构的问题。
7. **公司业务描述缺失**：`yf_biz.py` 依赖 `assetProfile`，抓取时 Yahoo 已全线限流返回 403，未跑完，因此概念成员目前只来自 ETF 前十大持仓。
8. **13 只产品里 2 只的数据名字不一致**：`BISXX` 在 Yahoo 的两条记录里名称不同（按代码查是 "Bishop Street Funds - Government Money Market Fund"，AGG 持仓表里写的是 "BlackRock Cash Funds Instl SL Agency"）。该条目现已不建实体，仅作记录。
9. **直接边的口径**：模型只把新闻里**真的出现**的公司名、产品名原样写出来，再由脚本解析到 universe，不让模型"凭印象挂标的"。所以 `AFFECTS` 那 479 条直接边条条都有文本依据；提到了但不在 universe 里的名称（208 处）单独记在 `unresolved_mentions` 里，可用于后续扩充宇宙。
10. **新闻源有两家**：Yahoo Finance 与 Google News。三者都带 `_from_source` 标记，如需只保留 Yahoo 数据，按该字段过滤即可（Google News 没有 `relatedTickers`，所以它的事件只能靠正文里的公司名/产品名落地）。

---

## 9. 复现方式

```powershell
cd <本目录>
$env:PYTHONIOENCODING='utf-8'

# 1) 抓数据（无需 LLM，需要联网）
python yf_fetch.py        # 基金与股票数据 -> yf_raw.json / yf_summary.json
python yf_names.py        # 名称与类型     -> yf_names.json
python yf_news.py         # 新闻           -> news_raw.json
python edgar_sic.py       # 行业           -> edgar_sic.json

# 2) 抽取与词表（需要 LLM，key 走环境变量）
$env:OPENAI_API_KEY='sk-...'
python news_to_event.py             # 新闻 -> 事件
python build_theme_vocab.py         # 事件主题 -> 受控词表
python build_theme_concept_map.py   # 主题 -> 概念

# 只升级了实体解析、不想重新调用模型时（结果与重跑一致）
python news_to_event.py --reresolve --out _yf_data/events_extracted_all.json

# 3) 建图与可视化
python build_kg_yf.py               # 建图 + 导出公司索引
python build_concepts.py            # 概念层（依赖上一步的公司索引）
python build_kg_yf.py               # 用概念层重建图
python kg_to_pyvis.py --kg src/kg/kg_yf.json --out src/kg/kg_yf.html
```

注：重新抓取数据会覆盖 `_yf_data/` 下的同名文件；若要保留某次快照，请先备份。

# yfinance 版知识图谱

本目录是"用公开市场数据构建知识图谱"的完整工程：**代码、数据、图谱结果、文档都在这里**，目录内部使用相对路径（`_yf_data/`、`src/kg/`），因此**直接在本目录运行脚本即可**，不需要改任何路径，也不依赖外部文件。

目标链路：`事件 → 主题 → 概念 → 公司股票 → 基金（产品）`

## 目录

| 路径 | 内容 |
|---|---|
| `*.py` | 13 个管线脚本 |
| `_yf_data/` | 中间数据与数据产物（JSON） |
| `src/kg/kg_yf.json` | 知识图谱（529 节点 / 1643 边） |
| `src/kg/kg_yf.html` | 交互式可视化（浏览器打开，可拖拽缩放） |
| `docs/` | 数据说明与项目架构文档 |

**建议先读**：

- `docs/yf_kg_data.md` —— 数据来源、字段含义、规模与已知问题
- `docs/yf_kg_architecture.md` —— 管线架构、每个脚本干什么、图谱 schema

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

**抽取与词表层**（调用 LLM）

| 脚本 | 作用 | 产出 |
|---|---|---|
| `news_to_event.py` | 新闻 → 结构化事件 | `events_extracted*.json` |
| `build_theme_vocab.py` | 主题受控词表（两轮归并） | `theme_vocab.json`、`theme_map.json` |
| `build_theme_concept_map.py` | 主题 → 可投资概念 | `theme_concept_map.json` |

**图谱构建层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `company_norm.py` | 公司名归一化解析器（模块） | — |
| `build_concepts.py` | 概念层骨架 | `concepts.json` |
| `build_kg_yf.py` | 主建图脚本 | `src/kg/kg_yf.json`、`company_alias/index.json` |

**展示层**

| 脚本 | 作用 | 产出 |
|---|---|---|
| `kg_to_pyvis.py` | 交互式可视化 | `src/kg/kg_yf.html` |

## 运行方式

数据已经在本目录里，**只想重建图谱的话跑最后三步就够了**：

```powershell
cd yfinance
$env:PYTHONIOENCODING='utf-8'

python build_kg_yf.py     # 建图（同时导出公司索引）
python build_concepts.py  # 概念层（依赖上一步的公司索引）
python build_kg_yf.py     # 用概念层重建图
python kg_to_pyvis.py --kg src/kg/kg_yf.json --out src/kg/kg_yf.html
```

要从头抓数据并重跑抽取（需要联网 + LLM key）：

```powershell
python yf_fetch.py; python yf_names.py; python yf_news.py; python edgar_sic.py
$env:OPENAI_API_KEY='sk-...'
python news_to_event.py; python build_theme_vocab.py; python build_theme_concept_map.py
python build_kg_yf.py; python build_concepts.py; python build_kg_yf.py
```

## 环境

- Python 3.13
- 第三方库见 `requirements.txt`，一条命令装好：`pip install -r requirements.txt`
  - `openai==0.28.0`：LLM 三步用，**必须锁 0.28.x**（脚本用的是旧接口 `openai.ChatCompletion.create`）
  - `pyvis`：交互式可视化用
  - `yfinance`：只有 `yf_smoke.py` 这个探测脚本用；抓取与建图都不依赖它——它的请求会被 Yahoo 风控拦截，本管线改用自建直连客户端（见 `docs/yf_kg_data.md` 4.1 节）
- LLM：DeepSeek `deepseek-v4-flash-vision-exp`，key 走环境变量 `OPENAI_API_KEY`

## 说明

- 数据为公开市场数据（Yahoo Finance / SEC EDGAR / Google News RSS），抓取时间见 `docs/yf_kg_data.md`。
- 文本抽取、主题归并、主题→概念映射三步需要调用 LLM（DeepSeek），其余步骤都可以离线复算。
- 重跑抓取会覆盖 `_yf_data/` 下的同名文件，若要保留某次快照请先备份。

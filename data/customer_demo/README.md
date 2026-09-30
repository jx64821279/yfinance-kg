# 客户层 demo 数据（合成）

这套数据是为了探索 **新闻 → 产品 → 客户** 这条链路而**合成**的，不是真实客户数据。
真实数据（客户 360 / 持仓 / 交易 / App 行为 / 客户经理交互 / offer）到位后，
只要字段能对上，就能直接替换，下游代码不用改。

生成方式：`python gen_customer_demo.py`（数据全部写死在脚本里，可复现，不依赖随机数）。

## 文件

| 文件 | 行数 | 说明 |
|---|---|---|
| `customers_all_in_one.csv` | 20 | 一行一个客户，20 个客户 |

编码为 UTF-8 with BOM，Excel 直接双击打开不会乱码。

## 表结构（23 列）

一行一个客户。基础属性各占一列，多值的部分收进单元格，
规则是 **`;` 分隔条目，`|` 分隔条目内部字段**。

### 基础属性

| 字段 | 含义 | 说明 |
|---|---|---|
| `customer_id` | 客户编号 | C001 ~ C020 |
| `name` | 姓名 | 虚构 |
| `age` / `gender` / `city` | 年龄 / 性别 / 城市 | |
| `occupation` | 职业 | |
| `annual_income_cny` | 年收入（元） | 退休客户填退休金 |
| `risk_level` | 风险等级 | C1 保守 ~ C5 激进 |
| `invest_years` | 投资年限 | |
| `total_assets_cny` | 在我行的总资产（元） | |
| `idle_cash_cny` | 活期闲钱（元） | 对应老师说的"有多少闲钱" |
| `time_deposit_cny` | 定期存款（元） | |
| `deposit_maturity` | 定期到期月份 | 到期是重要的推荐触发点 |
| `advisor_id` | 客户经理编号 | A01 ~ A06 |

### 多值字段

| 列 | 单元格格式 | 例子 |
|---|---|---|
| `holdings` | `ticker:权重%` | `AIQ:25%;SMH:20%;QQQ:20%;BOTZ:15%;XLK:10%;AGG:10%` |
| `holdings_value_cny` | `ticker:市值` | `AIQ:325000;SMH:260000` |
| `asset_classes` | 去重后的资产类别 | `Equity;FixedIncome` |
| `app_events` | `日期\|行为\|目标类型\|目标` | `2026-09-20\|搜索\|concept\|人工智能` |
| `advisor_interactions` | `日期\|渠道\|摘要` | `2026-09-18\|微信\|客户主动问 AI 主题…` |
| `offers` | `日期\|ticker\|推荐理由\|状态` | `2026-09-18\|BOTZ\|AI + 机器人主题分散配置\|已接受` |
| `transactions` | `日期\|买入/卖出\|ticker\|金额\|渠道` | `2026-06-06\|买入\|QQQ\|156000\|App` |

辅助列：`portfolio_value_cny`（= 持仓市值合计 = 总资产 − 闲钱 − 定期）、`n_holdings`（持仓只数）。

取值约定：

- `app_events` 的行为有 浏览 / 点击 / 搜索 / 咨询客服；目标类型有 `product`（对应 KG 的 Fund）、
  `concept`（对应 Concept）、`asset_class`（对应 AssetClass）。产品用 ticker，概念用中文概念名，
  资产类别用英文名。
- `offers` 的状态有 已接受 / 待定 / 已拒绝；**状态为已接受的产品一定出现在该客户的持仓里**。
- `transactions` 的渠道有 App / 客户经理 / 网点。

`app_events` 的目标之所以分 concept / asset_class 两类，是为了让"客户自己表达的兴趣"
能和 KG 的概念层、资产类别层对上——这是后面做"新闻 → 概念 → 该不该推给这个客户"的关键。

## 数据内部一致性（都被校验过）

1. 每个客户的 `holdings` 权重合计 = 100
2. 每个客户的 `holdings_value_cny` 合计 ≈ 总资产 − 闲钱 − 定期存款（误差 < 60 元，来自逐笔四舍五入）
3. 所有 ticker 都是 KG 里已有的 13 只 ETF（SMH、SOXX、AIQ、BOTZ、KWEB、CQQQ、QQQ、XLK、TLT、AGG、GLD、USO、UUP）
4. `app_events` 里的 concept 目标都在 KG 的 12 个概念里；asset_class 目标都在 KG 的 5 个资产类别里
5. 每一笔持仓都有一条对应的买入流水
6. `已接受` 的 offer 一定在该客户持仓中

## 以后怎么对应到 KG

真实数据接入后，客户层在 KG 里大致是这样长的（现在先只做到数据这一层）：

```
Customer --HOLDS--> Fund        （带 weight_pct、market_value）
Customer --VIEWED--> Fund / Concept / AssetClass   （来自 app_events）
Customer --CONSULTED_ABOUT--> Concept / AssetClass （来自 service_chat）
Customer --RECEIVED_OFFER--> Fund  （来自 offers，带 status）
```

加上已有的 `Event → Theme → Concept → Stock ← Fund`，
就能回答"一条新闻影响哪些产品，这些产品该推给哪些客户"。

客户属性（年龄、收入、风险等级、闲钱、定期到期）建议作为 **Customer 节点的属性**，
不要拆成节点；打分/排序同样不进图，和图分开。

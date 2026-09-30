# -*- coding: utf-8 -*-
"""生成“客户层”合成 demo 数据（用于探索 新闻 → 产品 → 客户 这条链路）。

为什么用合成数据
    真实的客户 360 / 持仓 / 交易 / App 行为 / 客户经理交互 / offer 目前都拿不到，
    先用一份**字段结构一致**的合成数据把链路打通；以后真实数据到位，
    只要字段对得上，直接换文件即可，下游代码不用改。

设计口径
    1. 产品只取 KG 里已有的 13 只 ETF（ticker 与 KG 的 Fund 节点一一对应），
       这样客户层一接进来，就能和 事件 → 主题 → 概念 → 股票 → 基金 那条链咬合。
    2. 金额单位一律人民币（元），假设客户通过 QDII / 跨境通道持有这些美股 ETF。
    3. weight_pct 是“占该客户投资组合的比重”，同一客户各条持仓权重合计 = 100；
       market_value_cny = 组合市值 × weight_pct，四舍五入到元。
    4. App 行为 / 客服咨询的目标分 product / concept / asset_class 三种，
       分别对应 KG 里的 Fund、Concept、AssetClass 节点。
    5. offer 状态为 accepted 的产品，一定出现在该客户的持仓里。
    6. 每一条持仓都配一条买入记录（交易表），另有一些“已卖出”记录；
       持仓市值与累计买入金额不必相等（中间有涨跌）。

输出
    data/customer_demo/customers_all_in_one.csv：**一张表、一行一个客户**，
    多值字段收进单元格（`;` 分隔条目，`|` 分隔条目内部字段）。
    编码 UTF-8 with BOM，Excel 直接打开不乱码。

用法
    python gen_customer_demo.py
"""

import csv
import os
from datetime import date, timedelta

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "customer_demo")

# ---------------------------------------------------------------- 产品表（取自 KG）
# ticker -> (产品全名, 资产类别)
PRODUCTS = {
    "SMH":  ("VanEck Semiconductor ETF", "Equity"),
    "SOXX": ("iShares Semiconductor ETF", "Equity"),
    "AIQ":  ("Global X Artificial Intelligence & Technology ETF", "Equity"),
    "BOTZ": ("Global X Robotics & Artificial Intelligence ETF", "Equity"),
    "KWEB": ("KraneShares CSI China Internet ETF", "Equity"),
    "CQQQ": ("Invesco China Technology ETF", "Equity"),
    "QQQ":  ("Invesco QQQ Trust", "Equity"),
    "XLK":  ("State Street Technology Select Sector SPDR ETF", "Equity"),
    "TLT":  ("iShares 20+ Year Treasury Bond ETF", "FixedIncome"),
    "AGG":  ("iShares Core U.S. Aggregate Bond ETF", "FixedIncome"),
    "GLD":  ("SPDR Gold Shares", "Commodity"),
    "USO":  ("United States Oil Fund, LP", "Commodity"),
    "UUP":  ("Invesco DB US Dollar Index Bullish Fund", "FX"),
}

# ---------------------------------------------------------------- 客户定义
# 20 个客户，覆盖：保守退休 / 稳健中产 / 平衡 / 科技进取 / 跨境偏好 / 高净值多元
CUSTOMERS = [
    {
        "id": "C001", "name": "陈建国", "age": 68, "gender": "男", "city": "上海",
        "occupation": "退休（原国企机械工程师）", "annual_income_cny": 180000,
        "risk_level": "C2", "invest_years": 15,
        "total_assets_cny": 3800000, "idle_cash_cny": 120000,
        "time_deposit_cny": 1500000, "deposit_maturity": "2027-03",
        "advisor_id": "A01",
        "holdings": [("TLT", 45), ("AGG", 30), ("GLD", 15), ("QQQ", 10)],
        "app": [
            ("2026-09-18", "view", "product", "GLD", "点开 SPDR 黄金 ETF 详情页"),
            ("2026-09-22", "service_chat", "asset_class", "FixedIncome", "电话咨询债券类产品的久期与利率风险"),
            ("2026-09-26", "search", "concept", "美国长期国债", "搜索“长债 降息 影响”"),
        ],
        "advisor": [("2026-09-05", "visit", "面谈退休金再配置，客户明确表示厌恶股市波动")],
        "offers": [("2026-09-05", "AGG", "用综合债券替代部分定期存款", "pending")],
    },
    {
        "id": "C002", "name": "王秀兰", "age": 65, "gender": "女", "city": "苏州",
        "occupation": "退休（中学教师）", "annual_income_cny": 150000,
        "risk_level": "C1", "invest_years": 8,
        "total_assets_cny": 2100000, "idle_cash_cny": 60000,
        "time_deposit_cny": 1200000, "deposit_maturity": "2026-12",
        "advisor_id": "A02",
        "holdings": [("AGG", 50), ("TLT", 25), ("GLD", 15), ("UUP", 10)],
        "app": [
            ("2026-09-19", "view", "product", "AGG", "查看历史回撤曲线"),
            ("2026-09-27", "service_chat", "product", "GLD", "咨询黄金 ETF 的最小购买金额"),
        ],
        "advisor": [("2026-09-12", "call", "提醒 12 月定期到期，沟通续存与产品衔接")],
        "offers": [("2026-09-12", "GLD", "建议小额黄金对冲通胀", "accepted")],
    },
    {
        "id": "C003", "name": "李伟", "age": 45, "gender": "男", "city": "北京",
        "occupation": "三甲医院副主任医师", "annual_income_cny": 700000,
        "risk_level": "C3", "invest_years": 12,
        "total_assets_cny": 2600000, "idle_cash_cny": 200000,
        "time_deposit_cny": 600000, "deposit_maturity": "2027-06",
        "advisor_id": "A03",
        "holdings": [("QQQ", 30), ("AGG", 30), ("GLD", 20), ("XLK", 10), ("TLT", 10)],
        "app": [
            ("2026-09-16", "view", "concept", "美国科技", "浏览“美股科技板块”专题页"),
            ("2026-09-21", "click", "product", "QQQ", "从推荐位点击纳指 100 ETF"),
            ("2026-09-25", "search", "concept", "半导体", "搜索“半导体周期 当前位置”"),
        ],
        "advisor": [
            ("2026-09-10", "wechat", "讨论加仓科技类产品，客户想等回调"),
            ("2026-09-24", "call", "沟通组合再平衡"),
        ],
        "offers": [("2026-09-10", "XLK", "科技敞口已够，建议用 XLK 替代个股", "rejected")],
    },
    {
        "id": "C004", "name": "张敏", "age": 42, "gender": "女", "city": "深圳",
        "occupation": "中学教师", "annual_income_cny": 280000,
        "risk_level": "C2", "invest_years": 6,
        "total_assets_cny": 1300000, "idle_cash_cny": 80000,
        "time_deposit_cny": 500000, "deposit_maturity": "2027-01",
        "advisor_id": "A02",
        "holdings": [("AGG", 45), ("GLD", 25), ("QQQ", 20), ("TLT", 10)],
        "app": [
            ("2026-09-17", "view", "product", "GLD", "看黄金 ETF 页面"),
            ("2026-09-23", "search", "asset_class", "Commodity", "搜索“大宗商品 怎么投”"),
        ],
        "advisor": [("2026-09-08", "call", "解释定期到期后的三种处理方式")],
        "offers": [("2026-09-08", "AGG", "稳健打底 + 少量黄金", "pending")],
    },
    {
        "id": "C005", "name": "刘洋", "age": 36, "gender": "男", "city": "杭州",
        "occupation": "互联网后端工程师", "annual_income_cny": 600000,
        "risk_level": "C4", "invest_years": 9,
        "total_assets_cny": 1800000, "idle_cash_cny": 300000,
        "time_deposit_cny": 200000, "deposit_maturity": "2026-11",
        "advisor_id": "A04",
        "holdings": [("AIQ", 25), ("SMH", 20), ("QQQ", 20), ("BOTZ", 15), ("XLK", 10), ("AGG", 10)],
        "app": [
            ("2026-09-15", "view", "product", "AIQ", "查看人工智能 ETF 持仓明细"),
            ("2026-09-20", "search", "concept", "人工智能", "搜索“AI 资本开支 2026”"),
            ("2026-09-28", "service_chat", "concept", "半导体", "咨询半导体 ETF 与买入个股的区别"),
        ],
        "advisor": [("2026-09-18", "wechat", "客户主动问 AI 主题还有没有别的产品")],
        "offers": [("2026-09-18", "BOTZ", "AI + 机器人主题分散配置", "accepted")],
    },
    {
        "id": "C006", "name": "赵雪", "age": 31, "gender": "女", "city": "上海",
        "occupation": "半导体公司产品经理", "annual_income_cny": 550000,
        "risk_level": "C4", "invest_years": 7,
        "total_assets_cny": 1500000, "idle_cash_cny": 250000,
        "time_deposit_cny": 150000, "deposit_maturity": "2027-02",
        "advisor_id": "A04",
        "holdings": [("SMH", 30), ("SOXX", 25), ("AIQ", 20), ("QQQ", 15), ("BOTZ", 10)],
        "app": [
            ("2026-09-14", "view", "concept", "半导体", "浏览半导体行业页"),
            ("2026-09-22", "search", "product", "SMH", "搜索“SMH 与 SOXX 区别”"),
            ("2026-09-27", "click", "product", "SOXX", "从活动页点击"),
        ],
        "advisor": [("2026-09-19", "call", "客户问行业 ETF 与主题 ETF 的差别")],
        "offers": [("2026-09-19", "SOXX", "行业 ETF 分散单一个股风险", "accepted")],
    },
    {
        "id": "C007", "name": "孙强", "age": 39, "gender": "男", "city": "成都",
        "occupation": "自动化设备公司销售总监", "annual_income_cny": 800000,
        "risk_level": "C3", "invest_years": 10,
        "total_assets_cny": 2200000, "idle_cash_cny": 180000,
        "time_deposit_cny": 400000, "deposit_maturity": "2027-05",
        "advisor_id": "A05",
        "holdings": [("BOTZ", 25), ("QQQ", 25), ("XLK", 20), ("AGG", 20), ("GLD", 10)],
        "app": [
            ("2026-09-16", "view", "concept", "机器人与自动化", "看机器人专题"),
            ("2026-09-26", "service_chat", "asset_class", "Equity", "咨询权益类产品的波动范围"),
        ],
        "advisor": [("2026-09-15", "visit", "客户从事自动化行业，想了解机器人主题")],
        "offers": [("2026-09-15", "BOTZ", "结合行业认知配置机器人主题", "accepted")],
    },
    {
        "id": "C008", "name": "周雅婷", "age": 29, "gender": "女", "city": "广州",
        "occupation": "跨境电商创业者", "annual_income_cny": 450000,
        "risk_level": "C4", "invest_years": 5,
        "total_assets_cny": 900000, "idle_cash_cny": 220000,
        "time_deposit_cny": 100000, "deposit_maturity": "2026-12",
        "advisor_id": "A06",
        "holdings": [("KWEB", 40), ("CQQQ", 30), ("QQQ", 20), ("AGG", 10)],
        "app": [
            ("2026-09-18", "view", "product", "KWEB", "查看中概互联 ETF"),
            ("2026-09-25", "search", "concept", "中概互联网", "搜索“中概股 监管”"),
        ],
        "advisor": [("2026-09-11", "wechat", "客户关注中概板块的反弹机会")],
        "offers": [("2026-09-11", "CQQQ", "中概敞口已集中，建议用 CQQQ 分散单一个股", "pending")],
    },
    {
        "id": "C009", "name": "吴海峰", "age": 52, "gender": "男", "city": "宁波",
        "occupation": "外贸公司股东", "annual_income_cny": 3000000,
        "risk_level": "C5", "invest_years": 18,
        "total_assets_cny": 12000000, "idle_cash_cny": 1200000,
        "time_deposit_cny": 3000000, "deposit_maturity": "2027-04",
        "advisor_id": "A01",
        "holdings": [("QQQ", 25), ("AGG", 20), ("TLT", 15), ("GLD", 15), ("AIQ", 10), ("SMH", 10), ("USO", 5)],
        "app": [
            ("2026-09-17", "view", "product", "USO", "看原油基金"),
            ("2026-09-24", "search", "concept", "原油", "搜索“油价 OPEC 减产”"),
            ("2026-09-28", "service_chat", "asset_class", "FX", "咨询美元资产的配置比例"),
        ],
        "advisor": [("2026-09-09", "visit", "高净值客户季度回顾，讨论汇率对冲")],
        "offers": [("2026-09-09", "UUP", "美元敞口对冲工具", "rejected")],
    },
    {
        "id": "C010", "name": "郑丽华", "age": 47, "gender": "女", "city": "南京",
        "occupation": "银行中层管理", "annual_income_cny": 650000,
        "risk_level": "C3", "invest_years": 14,
        "total_assets_cny": 3200000, "idle_cash_cny": 260000,
        "time_deposit_cny": 800000, "deposit_maturity": "2027-08",
        "advisor_id": "A03",
        "holdings": [("AGG", 30), ("QQQ", 30), ("XLK", 15), ("GLD", 15), ("TLT", 10)],
        "app": [
            ("2026-09-19", "view", "product", "XLK", "查看科技 ETF"),
            ("2026-09-26", "search", "concept", "美国科技", "搜索“科技股 估值”"),
        ],
        "advisor": [("2026-09-13", "call", "同业客户，关注产品费率")],
        "offers": [("2026-09-13", "XLK", "费率更低的科技敞口", "pending")],
    },
    {
        "id": "C011", "name": "冯涛", "age": 41, "gender": "男", "city": "西安",
        "occupation": "汽车零部件企业主", "annual_income_cny": 1800000,
        "risk_level": "C4", "invest_years": 11,
        "total_assets_cny": 5000000, "idle_cash_cny": 500000,
        "time_deposit_cny": 1000000, "deposit_maturity": "2027-03",
        "advisor_id": "A05",
        "holdings": [("QQQ", 20), ("AIQ", 15), ("GLD", 15), ("AGG", 15), ("SMH", 10), ("USO", 10), ("TLT", 10), ("UUP", 5)],
        "app": [
            ("2026-09-15", "view", "concept", "黄金", "浏览黄金专题"),
            ("2026-09-23", "search", "asset_class", "Commodity", "搜索“大宗商品配置比例”"),
            ("2026-09-27", "view", "product", "GLD", "看黄金 ETF 行情"),
        ],
        "advisor": [("2026-09-16", "visit", "企业主客户，关注实物资产与汇率")],
        "offers": [("2026-09-16", "GLD", "用实物资产对冲通胀与汇率", "accepted")],
    },
    {
        "id": "C012", "name": "何静", "age": 33, "gender": "女", "city": "武汉",
        "occupation": "高校科研人员", "annual_income_cny": 300000,
        "risk_level": "C2", "invest_years": 4,
        "total_assets_cny": 700000, "idle_cash_cny": 90000,
        "time_deposit_cny": 250000, "deposit_maturity": "2026-10",
        "advisor_id": "A02",
        "holdings": [("AGG", 40), ("QQQ", 30), ("GLD", 30)],
        "app": [
            ("2026-09-20", "view", "product", "QQQ", "看纳指 ETF"),
            ("2026-09-28", "service_chat", "product", "AGG", "咨询债券基金会不会亏本金"),
        ],
        "advisor": [("2026-09-14", "call", "解释定期到期与理财产品的区别")],
        "offers": [("2026-09-14", "AGG", "稳健打底", "accepted")],
    },
    {
        "id": "C013", "name": "徐鹏", "age": 55, "gender": "男", "city": "天津",
        "occupation": "国企中层管理", "annual_income_cny": 520000,
        "risk_level": "C3", "invest_years": 16,
        "total_assets_cny": 4100000, "idle_cash_cny": 220000,
        "time_deposit_cny": 1500000, "deposit_maturity": "2027-07",
        "advisor_id": "A06",
        "holdings": [("TLT", 30), ("AGG", 30), ("QQQ", 20), ("GLD", 20)],
        "app": [
            ("2026-09-18", "view", "concept", "美国长期国债", "看长债分析文章"),
            ("2026-09-25", "search", "asset_class", "FixedIncome", "搜索“降息 债券”"),
        ],
        "advisor": [("2026-09-12", "visit", "讨论利率下行周期的债券配置")],
        "offers": [("2026-09-12", "TLT", "久期拉长博降息", "accepted")],
    },
    {
        "id": "C014", "name": "朱雯", "age": 26, "gender": "女", "city": "杭州",
        "occupation": "互联网运营", "annual_income_cny": 260000,
        "risk_level": "C2", "invest_years": 2,
        "total_assets_cny": 350000, "idle_cash_cny": 70000,
        "time_deposit_cny": 50000, "deposit_maturity": "2026-12",
        "advisor_id": "A04",
        "holdings": [("QQQ", 60), ("AIQ", 40)],
        "app": [
            ("2026-09-21", "view", "product", "QQQ", "看纳指 ETF"),
            ("2026-09-27", "click", "product", "AIQ", "从首页推荐位点击"),
            ("2026-09-28", "search", "concept", "人工智能", "搜索“AI ETF 推荐”"),
        ],
        "advisor": [("2026-09-17", "wechat", "新手客户，讲解定投")],
        "offers": [("2026-09-17", "AIQ", "小额定投 AI 主题", "accepted")],
    },
    {
        "id": "C015", "name": "高志远", "age": 34, "gender": "男", "city": "北京",
        "occupation": "量化研究员", "annual_income_cny": 1200000,
        "risk_level": "C4", "invest_years": 8,
        "total_assets_cny": 2800000, "idle_cash_cny": 400000,
        "time_deposit_cny": 300000, "deposit_maturity": "2027-02",
        "advisor_id": "A03",
        "holdings": [("AIQ", 25), ("QQQ", 25), ("SMH", 20), ("SOXX", 15), ("BOTZ", 15)],
        "app": [
            ("2026-09-16", "view", "product", "BOTZ", "看机器人 ETF"),
            ("2026-09-22", "search", "concept", "半导体", "搜索“半导体设备 周期”"),
            ("2026-09-28", "service_chat", "asset_class", "Equity", "咨询行业 ETF 的跟踪误差"),
        ],
        "advisor": [("2026-09-20", "wechat", "专业客户，讨论因子暴露")],
        "offers": [("2026-09-20", "SOXX", "补齐设备环节敞口", "pending")],
    },
    {
        "id": "C016", "name": "林晓芸", "age": 44, "gender": "女", "city": "厦门",
        "occupation": "民营医院管理", "annual_income_cny": 700000,
        "risk_level": "C3", "invest_years": 9,
        "total_assets_cny": 2400000, "idle_cash_cny": 150000,
        "time_deposit_cny": 700000, "deposit_maturity": "2027-06",
        "advisor_id": "A05",
        "holdings": [("AGG", 35), ("QQQ", 25), ("GLD", 20), ("XLK", 10), ("TLT", 10)],
        "app": [
            ("2026-09-19", "view", "product", "GLD", "看黄金 ETF"),
            ("2026-09-26", "view", "product", "AGG", "看债券 ETF"),
            ("2026-09-28", "search", "asset_class", "Commodity", "搜索“黄金 避险”"),
        ],
        "advisor": [("2026-09-15", "call", "组合波动偏大，客户想降波动")],
        "offers": [("2026-09-15", "GLD", "用黄金替代部分权益", "accepted")],
    },
    {
        "id": "C017", "name": "黄伟民", "age": 60, "gender": "男", "city": "佛山",
        "occupation": "制造业退休返聘", "annual_income_cny": 360000,
        "risk_level": "C2", "invest_years": 20,
        "total_assets_cny": 3000000, "idle_cash_cny": 100000,
        "time_deposit_cny": 1400000, "deposit_maturity": "2027-01",
        "advisor_id": "A01",
        "holdings": [("AGG", 40), ("TLT", 30), ("GLD", 20), ("QQQ", 10)],
        "app": [
            ("2026-09-20", "view", "product", "TLT", "看长债 ETF"),
            ("2026-09-28", "service_chat", "asset_class", "FixedIncome", "咨询债券类产品的安全性"),
        ],
        "advisor": [("2026-09-10", "call", "沟通月度对账")],
        "offers": [("2026-09-10", "AGG", "以债为主的稳健组合", "accepted")],
    },
    {
        "id": "C018", "name": "罗静怡", "age": 37, "gender": "女", "city": "重庆",
        "occupation": "会计师事务所合伙人", "annual_income_cny": 1500000,
        "risk_level": "C4", "invest_years": 10,
        "total_assets_cny": 3600000, "idle_cash_cny": 320000,
        "time_deposit_cny": 600000, "deposit_maturity": "2027-09",
        "advisor_id": "A06",
        "holdings": [("QQQ", 25), ("KWEB", 15), ("AIQ", 15), ("SMH", 15), ("AGG", 15), ("GLD", 15)],
        "app": [
            ("2026-09-18", "search", "concept", "中国科技", "搜索“中国科技股 估值”"),
            ("2026-09-26", "view", "product", "KWEB", "看中概 ETF"),
        ],
        "advisor": [("2026-09-11", "visit", "客户希望组合更分散")],
        "offers": [("2026-09-11", "CQQQ", "分散中概敞口", "rejected")],
    },
    {
        "id": "C019", "name": "邓浩", "age": 30, "gender": "男", "city": "深圳",
        "occupation": "芯片设计工程师", "annual_income_cny": 480000,
        "risk_level": "C4", "invest_years": 5,
        "total_assets_cny": 1100000, "idle_cash_cny": 200000,
        "time_deposit_cny": 100000, "deposit_maturity": "2027-03",
        "advisor_id": "A04",
        "holdings": [("SMH", 35), ("AIQ", 30), ("QQQ", 20), ("BOTZ", 15)],
        "app": [
            ("2026-09-17", "search", "concept", "半导体", "搜索“晶圆厂 扩产”"),
            ("2026-09-27", "view", "product", "SMH", "看半导体 ETF"),
        ],
        "advisor": [("2026-09-19", "wechat", "客户主动问半导体 ETF 能不能定投")],
        "offers": [("2026-09-19", "SOXX", "与 SMH 互补的行业 ETF", "pending")],
    },
    {
        "id": "C020", "name": "杨帆", "age": 48, "gender": "男", "city": "青岛",
        "occupation": "港口物流公司财务总监", "annual_income_cny": 900000,
        "risk_level": "C3", "invest_years": 13,
        "total_assets_cny": 3300000, "idle_cash_cny": 240000,
        "time_deposit_cny": 900000, "deposit_maturity": "2027-05",
        "advisor_id": "A05",
        "holdings": [("QQQ", 30), ("AGG", 25), ("TLT", 15), ("GLD", 15), ("XLK", 15)],
        "app": [
            ("2026-09-16", "view", "product", "QQQ", "看纳指 ETF"),
            ("2026-09-25", "search", "concept", "原油", "搜索“油价 航运成本”"),
        ],
        "advisor": [("2026-09-12", "call", "讨论油价对物流成本的影响与对冲")],
        "offers": [("2026-09-12", "USO", "油价上行对冲", "rejected")],
    },
]

# 已卖出记录（对应产品在这些人当前持仓里已经没有了）
SOLD = [
    ("C003", "KWEB", "2026-07-08", 60000),
    ("C005", "USO", "2026-07-22", 45000),
    ("C009", "BOTZ", "2026-06-30", 180000),
    ("C011", "CQQQ", "2026-08-05", 120000),
    ("C012", "AIQ", "2026-07-15", 35000),
    ("C014", "GLD", "2026-08-12", 28000),
    ("C015", "KWEB", "2026-06-25", 90000),
    ("C018", "USO", "2026-08-18", 150000),
]

# 交易渠道
CHANNELS = ["app", "advisor", "branch"]


def portfolio_value(c):
    return c["total_assets_cny"] - c["idle_cash_cny"] - c["time_deposit_cny"]


def build_flat(customers, holdings, transactions, app_events, advisor_rows, offer_rows):
    """把六张表压成一张表：一行一个客户，多值字段收进单元格。"""
    BEHAVIOR_ZH = {"view": "浏览", "click": "点击", "search": "搜索", "service_chat": "咨询客服"}
    STATUS_ZH = {"accepted": "已接受", "pending": "待定", "rejected": "已拒绝"}
    CHANNEL_ZH = {"visit": "面访", "call": "电话", "wechat": "微信", "email": "邮件",
                  "app": "App", "advisor": "客户经理", "branch": "网点"}

    def group(rows, cid_key="customer_id"):
        g = {}
        for r in rows:
            g.setdefault(r[cid_key], []).append(r)
        return g

    hp, tp = group(holdings), group(transactions)
    ap, dp, op = group(app_events), group(advisor_rows), group(offer_rows)

    flat = []
    for c in customers:
        cid = c["customer_id"]
        hs = hp.get(cid, [])
        ts = tp.get(cid, [])
        flat.append({
            "customer_id": cid,
            "name": c["name"],
            "age": c["age"],
            "gender": c["gender"],
            "city": c["city"],
            "occupation": c["occupation"],
            "annual_income_cny": c["annual_income_cny"],
            "risk_level": c["risk_level"],
            "invest_years": c["invest_years"],
            "total_assets_cny": c["total_assets_cny"],
            "idle_cash_cny": c["idle_cash_cny"],
            "time_deposit_cny": c["time_deposit_cny"],
            "deposit_maturity": c["deposit_maturity"],
            "advisor_id": c["advisor_id"],
            "portfolio_value_cny": sum(int(h["market_value_cny"]) for h in hs),
            "n_holdings": len(hs),
            "holdings": ";".join("%s:%s%%" % (h["ticker"], h["weight_pct"]) for h in hs),
            "holdings_value_cny": ";".join("%s:%s" % (h["ticker"], h["market_value_cny"]) for h in hs),
            "asset_classes": ";".join(sorted({h["asset_class"] for h in hs})),
            "app_events": ";".join(
                "%s|%s|%s|%s" % (a["date"], BEHAVIOR_ZH.get(a["behavior_type"], a["behavior_type"]),
                                 a["target_type"], a["target"])
                for a in ap.get(cid, [])),
            "advisor_interactions": ";".join(
                "%s|%s|%s" % (d["date"], CHANNEL_ZH.get(d["channel"], d["channel"]), d["summary"])
                for d in dp.get(cid, [])),
            "offers": ";".join(
                "%s|%s|%s|%s" % (o["date"], o["ticker"], o["reason"], STATUS_ZH.get(o["status"], o["status"]))
                for o in op.get(cid, [])),
            "transactions": ";".join(
                "%s|%s|%s|%s|%s" % (t["date"], "买入" if t["action"] == "buy" else "卖出",
                                     t["ticker"], t["amount_cny"],
                                     CHANNEL_ZH.get(t["channel"], t["channel"]))
                for t in ts),
        })
    return flat


def build_rows():
    customers, holdings, transactions = [], [], []

    for i, c in enumerate(CUSTOMERS):
        customers.append({
            "customer_id": c["id"],
            "name": c["name"],
            "age": c["age"],
            "gender": c["gender"],
            "city": c["city"],
            "occupation": c["occupation"],
            "annual_income_cny": c["annual_income_cny"],
            "risk_level": c["risk_level"],
            "invest_years": c["invest_years"],
            "total_assets_cny": c["total_assets_cny"],
            "idle_cash_cny": c["idle_cash_cny"],
            "time_deposit_cny": c["time_deposit_cny"],
            "deposit_maturity": c["deposit_maturity"],
            "advisor_id": c["advisor_id"],
        })

        total = portfolio_value(c)
        for j, (ticker, weight) in enumerate(c["holdings"]):
            name, ac = PRODUCTS[ticker]
            mv = round(total * weight / 100)
            buy_date = date(2026, 5, 11) + timedelta(days=i * 3 + j * 7)
            holdings.append({
                "customer_id": c["id"],
                "ticker": ticker,
                "product_name": name,
                "asset_class": ac,
                "weight_pct": weight,
                "market_value_cny": mv,
                "first_buy_date": buy_date.isoformat(),
            })
            # 每一笔持仓配一条买入记录（买入金额约为当前市值的 4~6 成，差额是账面涨跌）
            factor = (0.4, 0.5, 0.6)[j % 3]
            transactions.append({
                "customer_id": c["id"],
                "date": buy_date.isoformat(),
                "action": "buy",
                "ticker": ticker,
                "product_name": name,
                "asset_class": ac,
                "amount_cny": round(mv * factor),
                "channel": CHANNELS[(i + j) % 3],
            })

    # 卖出记录（含更早的一笔买入，用来保证链路自洽）
    for k, (cid, ticker, d, amount) in enumerate(SOLD):
        name, ac = PRODUCTS[ticker]
        sell_date = date.fromisoformat(d)
        transactions.append({
            "customer_id": cid,
            "date": (sell_date - timedelta(days=60 + k * 3)).isoformat(),
            "action": "buy",
            "ticker": ticker,
            "product_name": name,
            "asset_class": ac,
            "amount_cny": round(amount * 1.1),
            "channel": CHANNELS[k % 3],
        })
        transactions.append({
            "customer_id": cid,
            "date": d,
            "action": "sell",
            "ticker": ticker,
            "product_name": name,
            "asset_class": ac,
            "amount_cny": amount,
            "channel": CHANNELS[(k + 1) % 3],
        })

    # 交易按客户 + 日期排序，编号
    transactions.sort(key=lambda r: (r["customer_id"], r["date"]))
    for n, r in enumerate(transactions, 1):
        r["trade_id"] = "TRD-%04d" % n

    app_events = []
    for c in CUSTOMERS:
        for d, beh, ttype, target, detail in c["app"]:
            app_events.append({
                "customer_id": c["id"],
                "date": d,
                "behavior_type": beh,
                "target_type": ttype,
                "target": target,
                "detail": detail,
            })
    app_events.sort(key=lambda r: (r["date"], r["customer_id"]))
    for n, r in enumerate(app_events, 1):
        r["event_id"] = "APP-%04d" % n

    advisor_rows, offer_rows = [], []
    for c in CUSTOMERS:
        for d, ch, summary in c["advisor"]:
            advisor_rows.append({
                "customer_id": c["id"],
                "advisor_id": c["advisor_id"],
                "date": d,
                "channel": ch,
                "summary": summary,
            })
        for d, ticker, reason, status in c["offers"]:
            offer_rows.append({
                "customer_id": c["id"],
                "advisor_id": c["advisor_id"],
                "date": d,
                "ticker": ticker,
                "product_name": PRODUCTS[ticker][0],
                "reason": reason,
                "status": status,
            })
    advisor_rows.sort(key=lambda r: (r["date"], r["customer_id"]))
    for n, r in enumerate(advisor_rows, 1):
        r["interaction_id"] = "ADV-%04d" % n
    offer_rows.sort(key=lambda r: (r["date"], r["customer_id"]))
    for n, r in enumerate(offer_rows, 1):
        r["offer_id"] = "OFR-%04d" % n

    return customers, holdings, transactions, app_events, advisor_rows, offer_rows


FLAT_SCHEMA = [
    "customer_id", "name", "age", "gender", "city", "occupation",
    "annual_income_cny", "risk_level", "invest_years",
    "total_assets_cny", "idle_cash_cny", "time_deposit_cny",
    "deposit_maturity", "advisor_id",
    "portfolio_value_cny", "n_holdings", "holdings", "holdings_value_cny", "asset_classes",
    "app_events", "advisor_interactions", "offers", "transactions",
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    flat = build_flat(*build_rows())
    path = os.path.join(OUT_DIR, "customers_all_in_one.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FLAT_SCHEMA)
        w.writeheader()
        w.writerows(flat)
    print("%-26s %4d rows  %4d cols" % ("customers_all_in_one.csv", len(flat), len(FLAT_SCHEMA)))


if __name__ == "__main__":
    main()

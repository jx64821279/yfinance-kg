"""
把 KG（如 src/kg/kg_yf.json）转成 pyvis 交互式 HTML。
浏览器里可拖拽、缩放、点节点看属性；支持按类型筛选、物理布局开关。
运行：python kg_to_pyvis.py [--kg src/kg/kg_yf.json] [--out src/kg/kg_yf.html]
"""
import os
import json
import argparse
import math

from pyvis.network import Network

parser = argparse.ArgumentParser()
parser.add_argument("--kg", default=os.path.join("src", "kg", "kg_yf.json"))
parser.add_argument("--out", default=os.path.join("src", "kg", "kg_yf.html"))
parser.add_argument("--title", default=None)
args, _ = parser.parse_known_args()

COLORS = {
    # 9 类节点各占一个色相，避免同类色相互相混淆
    "Event": "#E8590C",          # 橘
    "Theme": "#26C6DA",          # 亮青（原本也是橙色系，与 Event 撞色）
    "DriverCategory": "#607D8B",  # 蓝灰
    "Concept": "#D81B60",        # 洋红
    "Industry": "#795548",       # 棕
    "Stock": "#2E7D32",          # 绿
    "Company": "#1E88E5",        # 蓝
    "Fund": "#7B1FA2",           # 紫
    "AssetClass": "#263238",     # 近黑
    # 以下类型用于其它版本的图谱，保留备用
    "CompanyProduct": "#9370DB", "Bond": "#8B5A2B", "Money": "#5D6D7E",
    "Commodity": "#B8860B", "FX": "#0B7A8A", "Value": "#A9A9A9",
}
SHAPES = {"Event": "dot", "Theme": "hexagon", "AssetClass": "square", "Fund": "triangle",
          "Stock": "dot", "Company": "dot", "Industry": "triangleDown",
          "DriverCategory": "diamond", "Concept": "star"}


def build_legend(counts):
    order = ["Event", "Theme", "DriverCategory", "AssetClass", "Fund", "Stock", "Company",
             "Industry", "Concept", "CompanyProduct", "Bond", "Money", "Commodity", "FX", "Value"]
    rows = []
    for t in order:
        if t not in counts:
            continue
        c = COLORS.get(t, "#999999")
        marker = {"square": "■", "triangle": "▲", "triangleDown": "▼", "diamond": "◆",
                  "hexagon": "⬢", "star": "★"}.get(SHAPES.get(t, "dot"), "●")
        rows.append(f"<div style='white-space:nowrap'><span style='color:{c}'>{marker}</span> "
                    f"<span style='color:#333'>{t}</span> "
                    f"<span style='color:#999'>({counts[t]})</span></div>")
    tip = ("<div style='color:#666;margin-top:6px;border-top:1px solid #eee;padding-top:6px'>"
           "节点大小 = 连接数<br>边 = 关系（悬停查看）<br>拖动 / 滚轮缩放 / 框选</div>")
    grip = ("<div id='kg-legend-h' style='font-weight:700;margin-bottom:4px;cursor:move;"
            "user-select:none;-webkit-user-select:none'>⣿ 图例 · 节点类型（按住可拖动）</div>")
    box = ("<div id='kg-legend' style='position:fixed;top:12px;left:12px;z-index:9999;"
            "background:rgba(255,255,255,.94);border:1px solid #dddddd;border-radius:10px;"
            "padding:10px 14px;font:12px/1.7 Microsoft YaHei,SimHei,sans-serif;"
            "box-shadow:0 2px 10px rgba(0,0,0,.12)'>"
            + grip
            + "<div style='max-height:64vh;overflow:auto'>" + "".join(rows) + "</div>"
            + tip + "</div>")
    script = """<script>
(function(){
  var el=document.getElementById('kg-legend'), h=document.getElementById('kg-legend-h');
  if(!el||!h){return;}
  var dragging=false,sx=0,sy=0,ox=0,oy=0;
  function start(e){
    var p=e.touches?e.touches[0]:e, r=el.getBoundingClientRect();
    el.style.left=r.left+'px'; el.style.top=r.top+'px'; el.style.right='auto';
    dragging=true; sx=p.clientX; sy=p.clientY; ox=r.left; oy=r.top;
    if(e.cancelable){e.preventDefault();}
  }
  function move(e){
    if(!dragging){return;}
    var p=e.touches?e.touches[0]:e;
    el.style.left=(ox+p.clientX-sx)+'px';
    el.style.top =(oy+p.clientY-sy)+'px';
    if(e.cancelable){e.preventDefault();}
  }
  function end(){ dragging=false; }
  h.addEventListener('mousedown',start);
  h.addEventListener('touchstart',start,{passive:false});
  document.addEventListener('mousemove',move);
  document.addEventListener('touchmove',move,{passive:false});
  document.addEventListener('mouseup',end);
  document.addEventListener('touchend',end);
})();
</script>"""
    return box + script


def esc(v):
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


LABELS = {
    "theme": "主题", "event_category": "事件域", "polarity": "方向", "asset_class": "资产类别",
    "rationale": "概括", "news_title": "新闻", "driver_category": "驱动类别", "aliases": "别名",
    "concept_type": "概念类型", "markets": "市场", "n_members": "成员数", "ticker": "代码",
    "family": "基金公司", "category": "类别", "key": "键", "standard": "分类标准", "sic": "SIC",
    "news_source": "新闻来源", "pub_date": "发布日",
    "tickers": "代码", "date": "日期", "source": "来源", "positions": "资产配置",
    "ts_code": "股票代码", "market": "交易所", "confidence": "置信度", "cause": "原因",
    "effect": "影响", "code": "代码", "name": "名称",
}
# 每种类型只展示这些字段（按顺序），不再把所有字段倒出来
TOOLTIP_FIELDS = {
    "Event": ["date", "pub_date", "event_category", "theme", "asset_class", "polarity",
              "rationale", "news_source", "news_title"],
    "Theme": ["driver_category", "key", "aliases"],
    "DriverCategory": [],
    "Concept": ["concept_type", "markets", "n_members", "asset_class"],
    "Fund": ["ticker", "family", "category", "asset_class", "positions"],
    "Stock": ["ticker", "ts_code", "asset_class", "market", "date"],
    "Company": ["tickers", "aliases"],
    "Industry": ["standard", "sic"],
    "AssetClass": ["code"],
}


def _fmt_positions(v):
    try:
        d = json.loads(v) if isinstance(v, str) else v
    except Exception:
        return str(v)
    if not isinstance(d, dict):
        return str(v)
    lab = {"stockPosition": "股票", "bondPosition": "债券", "cashPosition": "现金",
           "otherPosition": "其他", "preferredPosition": "优先股", "convertiblePosition": "可转债"}
    out = []
    for k, x in d.items():
        if x is None:
            continue
        try:
            out.append(f"{lab.get(k, k)} {float(x) * 100:.1f}%")
        except Exception:
            out.append(f"{lab.get(k, k)} {x}")
    return " · ".join(out)


def tooltip(node):
    """注意：vis-network 的 tooltip 用 innerText 渲染（不是 innerHTML），
    所以这里必须输出【纯文本】，用 \\n 换行；写 HTML 会被原样显示成一堆标签。"""
    t = node.get("type", "")
    lines = [f"{node.get('name') or node.get('id') or ''}   [{t}]"]
    for k in TOOLTIP_FIELDS.get(t, []):
        v = node.get(k)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        if v in (None, ""):
            continue
        if k == "positions":
            v = _fmt_positions(v)
        s = str(v)
        if len(s) > 180:
            s = s[:177] + "…"
        lines.append(f"{LABELS.get(k, k)}: {s}")
    return "\n".join(lines)


# 关系的中文说明（边悬停时显示，关系名本身保留英文）
REL_HINT = {
    "HAS_THEME": "事件的主题",
    "MAPS_TO": "主题对应的可投资概念",
    "IMPACTS": "主题影响的资产类别",
    "AFFECTS": "事件直接影响的标的",
    "IS_CLASS": "属于该资产类别",
    "CONSTITUENT": "基金持有的成分",
    "HAS_CASH": "产品里的现金类资产",
    "CORRESPONDS_TO": "公司对应的上市股票",
    "IN_INDUSTRY": "所属行业 · EDGAR SIC",
    "FOCUSES_ON": "基金聚焦的概念",
    "INCLUDES": "概念包含的成分股",
    "BELONGS_TO": "主题所属的驱动类别",
    "DRIVES": "事件驱动",
}
EDGE_LABELS = {"weight": "权重", "polarity": "方向", "news_source": "新闻来源",
               "mapping_source": "映射来源", "confidence": "置信度", "date": "日期"}


def _fmt_weight(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{x * 100:.2f}%" if 0 <= x <= 1 else f"{x:.2f}%"


def edge_tooltip(ed, names):
    """边悬停提示，同样必须是纯文本（vis-network 用 innerText 渲染），
    写 <b> 之类会被当成字面标签显示。"""
    def short(n, k=34):
        n = str(n)
        return n if len(n) <= k else n[:k - 1] + "…"

    rel = ed["relation"]
    first = f"{rel}（{REL_HINT[rel]}）" if rel in REL_HINT else rel
    lines = [first, f"{short(names.get(ed['source'], ed['source']))} → "
                    f"{short(names.get(ed['target'], ed['target']))}"]
    for k, v in ed.items():
        if k in ("source", "relation", "target") or v in (None, ""):
            continue
        s = _fmt_weight(v) if k == "weight" else str(v)
        lines.append(f"{EDGE_LABELS.get(k, k)}: {s}")
    return "\n".join(lines)


def text_color(hexc):
    """深色底用白字、浅色底用黑字。"""
    try:
        r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
    except Exception:
        return "#111111"
    return "#FFFFFF" if (0.299 * r + 0.587 * g + 0.114 * b) < 150 else "#111111"


def main():
    kg = json.load(open(args.kg, encoding="utf-8"))
    entities = kg["entities"]
    edges = kg["edges"]
    names = {e["id"]: (e.get("name") or e["id"]) for e in entities}

    deg = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1

    net = Network(height="900px", width="100%", directed=True, notebook=False,
                  cdn_resources="in_line", bgcolor="#ffffff", font_color="#222222",
                  select_menu=True, filter_menu=True)

    for e in entities:
        nid = e["id"]
        name = e.get("name") or nid
        label = name if len(name) <= 30 else name[:29] + "…"
        d = deg.get(nid, 0)
        net.add_node(
            nid,
            label=label,
            title=tooltip(e),
            size=8 + 3.2 * math.log(d + 1),
            shape=SHAPES.get(e["type"], "dot"),
            group=e["type"],
        )

    for ed in edges:
        props = {k: v for k, v in ed.items() if k not in ("source", "relation", "target")}
        try:
            wnum = float(props.get("weight"))
        except (TypeError, ValueError):
            wnum = 0.0
        if wnum > 1:                     # 有的 KG 里权重存的是百分数
            wnum = wnum / 100.0
        net.add_edge(ed["source"], ed["target"],
                     title=edge_tooltip(ed, names), arrows="to",
                     color={"color": "#C4C9D4", "highlight": "#FF8C00", "opacity": 0.7},
                     width=1.0 + 3.0 * wnum)

    # 用 group 样式上色（pyvis 传了 group 就会忽略节点级 color）
    groups_opts = {}
    for t, c in COLORS.items():
        groups_opts[t] = {
            "color": {"background": c, "border": "#555555",
                      "highlight": {"background": "#FFD700", "border": "#333333"}},
            "shape": SHAPES.get(t, "dot"),
            "font": {"color": text_color(c), "size": 13, "face": "Microsoft YaHei"},
        }

    net.set_options(json.dumps({
        "configure": {"enabled": True, "filter": ["physics", "nodes", "edges", "layout"],
                      "showButton": True},
        "groups": groups_opts,
        "physics": {"barnesHut": {"gravitationalConstant": -8000, "springLength": 120,
                                  "springConstant": 0.03, "damping": 0.5},
                    "stabilization": {"iterations": 600 if len(entities) < 400 else 250,
                                      "updateInterval": 50}},
        "interaction": {"hover": True, "tooltipDelay": 120, "navigationButtons": True,
                        "keyboard": {"enabled": True}, "multiselect": True},
        "nodes": {"borderWidth": 1.2, "font": {"size": 13, "face": "Microsoft YaHei"}},
        "edges": {"smooth": {"type": "dynamic"}},
    }))

    counts = {}
    for e in entities:
        counts[e["type"]] = counts.get(e["type"], 0) + 1

    html = net.generate_html(notebook=False)
    html = html.replace("<body>", "<body>\n" + build_legend(counts), 1)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[INFO] nodes {len(entities)} | edges {len(edges)}")
    print(f"[INFO] -> {args.out}")


if __name__ == "__main__":
    main()

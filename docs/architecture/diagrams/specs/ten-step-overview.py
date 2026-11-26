"""Hand-laid draw.io for the README ten-step overview (ex-Mermaid chart)."""
import sys
from xml.sax.saxutils import escape

C = {  # fill, stroke  (same palette as the other three diagrams)
 "gray": ("#F5F5F5", "#666666"), "orange": ("#FFE6CC", "#D79B00"), "red": ("#F8CECC", "#B85450"),
 "purple": ("#E8DFF5", "#9673A6"), "blue": ("#DAE8FC", "#6C8EBF"), "green": ("#D5E8D4", "#82B366"),
 "yellow": ("#FFF2CC", "#D6B656"), "redstrong": ("#B85450", "#B85450"),
}
cells = []
def esc(s): return escape(s, {'"': "&quot;", "\n": "&#10;"})
def node(id, label, x, y, w, h, color, shape="", bold=False, dashed=False, fontcolor="#000000"):
    f, s = C[color]
    st = f"rounded=1;whiteSpace=wrap;html=1;arcSize=8;fillColor={f};strokeColor={s};fontColor={fontcolor};fontSize=12;"
    if shape == "rhombus": st = f"rhombus;whiteSpace=wrap;html=1;fillColor={f};strokeColor={s};fontColor={fontcolor};fontSize=12;"
    if shape == "cyl": st = f"shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;size=8;fillColor={f};strokeColor={s};fontColor={fontcolor};fontSize=12;"
    if bold: st += "fontStyle=1;"
    if dashed: st += "dashed=1;"
    cells.append(f'<mxCell id="{id}" value="{esc(label)}" style="{st}" vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
def edge(id, src, dst, label="", color="#333333", dashed=False, pts=(), exit=None, entry=None, lx=None):
    st = f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;strokeColor={color};strokeWidth=1.5;endArrow=block;fontSize=11;fontColor=#333333;labelBackgroundColor=#FFFFFF;"
    if dashed: st += "dashed=1;"
    if exit: st += f"exitX={exit[0]};exitY={exit[1]};exitDx=0;exitDy=0;"
    if entry: st += f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;"
    p = "".join(f'<mxPoint x="{a}" y="{b}"/>' for a, b in pts)
    arr = f'<Array as="points">{p}</Array>' if pts else ""
    cells.append(f'<mxCell id="{id}" value="{esc(label)}" style="{st}" edge="1" parent="1" source="{src}" target="{dst}"><mxGeometry {f'x="{lx}" ' if lx is not None else ''}relative="1" as="geometry">{arr}</mxGeometry></mxCell>')

W, MX = 240, 420          # main column width / x
cx = MX + W // 2
cells.append(f'<mxCell id="title" value="RouteForge — every call, ten steps (policy first, eligible set before scoring)" style="text;html=1;align=center;verticalAlign=middle;fontSize=18;fontStyle=1;fontColor=#222222;" vertex="1" parent="1"><mxGeometry x="20" y="10" width="1100" height="30" as="geometry"/></mxCell>')
cells.append('<mxCell id="rf" value="RouteForge (self-hosted)" style="rounded=1;whiteSpace=wrap;html=1;arcSize=2;fillColor=#FBFBFB;strokeColor=#9AA0A6;dashed=1;verticalAlign=top;align=left;spacingLeft=10;spacingTop=4;fontStyle=1;fontSize=14;fontColor=#444444" vertex="1" parent="1"><mxGeometry x="300" y="160" width="460" height="1080" as="geometry"/></mxCell>')

node("app", "Application\nany OpenAI SDK · rf_live_ token", MX, 60, W, 60, "gray", bold=True)
steps = [
 ("s1", "1 · API\n/v1/chat/completions", "orange", 200),
 ("s2", "2 · Policy gate\nidentity · PII/PHI detect · compile policy", "red", 290),
 ("s3", "3 · Execution context\nimmutable, per request", "purple", 380),
 ("s4", "4 · Task mode\nalways direct", "blue", 470),
 ("s5", "5 · Context optimisation\nHeadroom + local CCR (opt-in)", "purple", 560),
]
for id, l, c, y in steps: node(id, l, MX, y, W, 60, c, bold=(id == "s1"))
node("e", "Eligible set E(r)\nresidency · regions · retention\nallowlists · cost cap", MX - 20, 650, W + 40, 130, "yellow", shape="rhombus", bold=True)
tail = [
 ("s6", "6 · Router\nscore only inside E(r)", "orange", 820),
 ("s7", "7 · Gateway\nquotas · breakers · failover\n(re-checks E on every attempt)", "orange", 910),
 ("s9", "9 · Output controls\nleakage check · verification", "red", 1030),
 ("s10", "10 · Governed response\n+ decision trace", "green", 1120),
]
for id, l, c, y in tail: node(id, l, MX, y, W, 70 if id == "s7" else 60, c, bold=(id == "s10"))

# left: rejections
node("rej", "Reject\n(fail closed)", 70, 290, 190, 60, "redstrong", bold=True, fontcolor="#FFFFFF")
node("core", "422 + minimal\nconflicting core", 70, 685, 190, 60, "redstrong", bold=True, fontcolor="#FFFFFF")
# right: registry, backends, sinks
node("reg", "Model registry\nendpoint declarations", 840, 680, 220, 70, "purple", shape="cyl")
node("onp", "8 · On-prem endpoints\nvLLM · Dynamo · NIM\n(onboarded at runtime)", 840, 860, 220, 70, "green")
node("cld", "8 · Cloud providers\nAzure · Bedrock · aggregators", 840, 960, 220, 60, "yellow", dashed=True)
node("db", "Routing outcomes\n→ replay simulator", 840, 1060, 220, 70, "gray", shape="cyl")
node("lf", "Langfuse / OTel", 840, 1160, 220, 60, "purple", shape="cyl")

chain = ["app", "s1", "s2", "s3", "s4", "s5", "e"]
for a, b in zip(chain, chain[1:]): edge(f"{a}_{b}", a, b, exit=(0.5, 1), entry=(0.5, 0))
edge("e_s6", "e", "s6", "non-empty", exit=(0.5, 1), entry=(0.5, 0))
edge("s6_s7", "s6", "s7", exit=(0.5, 1), entry=(0.5, 0))
edge("s7_s9", "s7", "s9", "completion", color="#82B366", exit=(0.5, 1), entry=(0.5, 0))
edge("s9_s10", "s9", "s10", exit=(0.5, 1), entry=(0.5, 0))
edge("s2_rej", "s2", "rej", "gate error", color="#B85450", exit=(0, 0.5), entry=(1, 0.5))
edge("e_core", "e", "core", "empty", color="#B85450", exit=(0, 0.5), entry=(1, 0.5))
edge("reg_e", "reg", "e", "declarations", color="#9673A6", exit=(0, 0.5), entry=(1, 0.5))
edge("s7_onp", "s7", "onp", "dispatch", color="#D79B00", exit=(1, 0.3), entry=(0, 0.5), pts=[(805, 931), (805, 895)], lx=-0.45)
edge("s7_cld", "s7", "cld", "only if policy allows", color="#D79B00", dashed=True, exit=(1, 0.7), entry=(0, 0.5), pts=[(805, 959), (805, 990)], lx=-0.45)
edge("s10_db", "s10", "db", "content-free record", color="#999999", dashed=True, exit=(1, 0.3), entry=(0, 0.5), pts=[(805, 1138), (805, 1095)], lx=-0.45)
edge("s10_lf", "s10", "lf", "redacted on export", color="#9673A6", dashed=True, exit=(1, 0.7), entry=(0, 0.5), pts=[(805, 1162), (805, 1190)], lx=-0.45)
edge("s10_app", "s10", "app", "governed response", color="#82B366", exit=(0, 0.5), entry=(0, 0.5), pts=[(40, 1150), (40, 90)])

xml = f'''<mxfile host="app.diagrams.net" agent="routeforge-overview">
  <diagram id="overview" name="Ten-step overview">
    <mxGraphModel dx="1200" dy="1300" grid="1" gridSize="10" guides="1" page="1" pageScale="1" pageWidth="1120" pageHeight="1260" math="0" shadow="0">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        {chr(10).join("        " + c for c in cells).lstrip()}
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
'''
open(sys.argv[1], "w").write(xml)

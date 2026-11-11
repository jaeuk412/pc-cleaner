"""발표 자료(PPTX) 생성 스크립트.

    pip install python-pptx pillow
    python docs/slides/build_pptx.py            # docs/presentation.pptx 생성
    python docs/slides/build_pptx.py --preview  # 미리보기 PNG도 생성 (docs/slides/preview/)

좌표는 1280×720 px 기준(1px = 1/96인치)이고 PPTX 슬라이드는 13.333×7.5인치(16:9)입니다.
같은 정의로 PPTX와 미리보기 이미지를 함께 그리므로, PowerPoint가 없어도 배치와 글자 넘침을 확인할 수 있습니다.
생성된 PPTX는 모든 글자가 일반 텍스트 상자라서 PowerPoint에서 바로 수정할 수 있습니다.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from pathlib import Path

from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
EMU_PX = 9525  # 1px @96dpi

# ---------------------------------------------------------------- palette
INK, TEAL, TEAL_D, MINT, MINT_L = "0B2530", "0E7C86", "0A5C64", "2EC4A6", "E3F6F2"
MIST, LINE, MUTED, BODY, WHITE = "F1F6F7", "D5E2E5", "5B7078", "33494F", "FFFFFF"
WARN_BG, WARN, DARK2, ON_DARK, MUTED_DARK = "FDE6DF", "B23A18", "133543", "CFE2E6", "8FB3BA"
KR_FONT, MONO_FONT = "Malgun Gothic", "Consolas"

FONT_FILES = {
    ("kr", False): r"C:\Windows\Fonts\malgun.ttf",
    ("kr", True): r"C:\Windows\Fonts\malgunbd.ttf",
    ("mono", False): r"C:\Windows\Fonts\consola.ttf",
    ("mono", True): r"C:\Windows\Fonts\consolab.ttf",
}


# ---------------------------------------------------------------- deck model
class Slide:
    def __init__(self, bg=WHITE, notes=""):
        self.bg, self.notes, self.items = bg, notes, []

    def rect(self, x, y, w, h, fill=None, radius=0, line=None, name=None):
        self.items.append(("rect", dict(x=x, y=y, w=w, h=h, fill=fill, radius=radius, line=line, name=name)))

    def oval(self, x, y, d, fill, name=None):
        self.items.append(("oval", dict(x=x, y=y, w=d, h=d, fill=fill, name=name)))

    def text(self, x, y, w, h, paras, size=14, color=BODY, bold=False, font="kr", align="l", valign="t",
             ls=1.0, after=0, bullet=None, name=None):
        """paras: str | list[str | list[(text, opts)]]"""
        if isinstance(paras, str):
            paras = [paras]
        norm = []
        for p in paras:
            norm.append([(p, {})] if isinstance(p, str) else [(t, dict(o)) for t, o in p])
        self.items.append(("text", dict(x=x, y=y, w=w, h=h, paras=norm, size=size, color=color, bold=bold,
                                        font=font, align=align, valign=valign, ls=ls, after=after,
                                        bullet=bullet, name=name)))

    def line(self, pts, color=TEAL, width=2, arrow=True, dash=False):
        self.items.append(("line", dict(pts=pts, color=color, width=width, arrow=arrow, dash=dash)))

    def image(self, x, y, w, h, path, name=None):
        self.items.append(("image", dict(x=x, y=y, w=w, h=h, path=str(path), name=name)))

    def donut(self, x, y, d, value, colors, hole=72):
        self.items.append(("donut", dict(x=x, y=y, w=d, h=d, value=value, colors=colors, hole=hole)))


# ---------------------------------------------------------------- PPTX writer
def _emu(px):
    return Emu(int(round(px * EMU_PX)))


def _rgb(hexstr):
    return RGBColor.from_string(hexstr)


def _set_run_font(run, size, color, bold, font):
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.color.rgb = _rgb(color)
    face = MONO_FONT if font == "mono" else KR_FONT
    f.name = face
    # 한글은 동아시아(ea) 글꼴로 렌더링되므로 ea에도 맑은 고딕을 지정 (latin 바로 뒤에 위치해야 함)
    rpr = run._r.get_or_add_rPr()
    prev = rpr.find(qn("a:latin"))
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = etree.Element(qn(tag))
            prev.addnext(el)
        el.set("typeface", KR_FONT)
        prev = el


def _set_bullet(paragraph, char, color, indent_px):
    ppr = paragraph._p.get_or_add_pPr()
    ppr.set("marL", str(int(indent_px * EMU_PX)))
    ppr.set("indent", str(int(-indent_px * EMU_PX)))
    for tag in ("a:buClr", "a:buFont", "a:buChar", "a:buNone"):
        old = ppr.find(qn(tag))
        if old is not None:
            ppr.remove(old)
    clr = etree.SubElement(ppr, qn("a:buClr"))
    etree.SubElement(clr, qn("a:srgbClr")).set("val", color)
    # Arial에는 ✓ 글리프가 없으므로 기호용 글꼴 사용 (Windows 기본 탑재)
    etree.SubElement(ppr, qn("a:buFont")).set("typeface", "Segoe UI Symbol" if char == "✓" else "Arial")
    etree.SubElement(ppr, qn("a:buChar")).set("char", char)


def _name(shape, name):
    if name:
        shape.name = name


def write_pptx(slides, out_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = _emu(1280), _emu(720)
    prs.core_properties.title = "PC Cleaner: 용량 확보 및 백업 자동화"
    prs.core_properties.author = "jaeuk"
    blank = prs.slide_layouts[6]
    for sd in slides:
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = _rgb(sd.bg)
        for kind, a in sd.items:
            if kind in ("rect", "oval"):
                if kind == "oval":
                    shp_type = MSO_SHAPE.OVAL
                else:
                    shp_type = MSO_SHAPE.ROUNDED_RECTANGLE if a["radius"] else MSO_SHAPE.RECTANGLE
                shp = s.shapes.add_shape(shp_type, _emu(a["x"]), _emu(a["y"]), _emu(a["w"]), _emu(a["h"]))
                if kind == "rect" and a["radius"]:
                    shp.adjustments[0] = min(0.5, a["radius"] / min(a["w"], a["h"]))
                if a.get("fill"):
                    shp.fill.solid()
                    shp.fill.fore_color.rgb = _rgb(a["fill"])
                else:
                    shp.fill.background()
                if a.get("line"):
                    shp.line.color.rgb = _rgb(a["line"])
                    shp.line.width = Pt(1.25)
                else:
                    shp.line.fill.background()
                shp.shadow.inherit = False
                _name(shp, a.get("name"))
            elif kind == "text":
                tb = s.shapes.add_textbox(_emu(a["x"]), _emu(a["y"]), _emu(a["w"]), _emu(a["h"]))
                tf = tb.text_frame
                tf.word_wrap = True
                tf.auto_size = None
                tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
                tf.vertical_anchor = {"t": MSO_ANCHOR.TOP, "m": MSO_ANCHOR.MIDDLE, "b": MSO_ANCHOR.BOTTOM}[a["valign"]]
                for i, runs in enumerate(a["paras"]):
                    p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                    p.alignment = {"l": PP_ALIGN.LEFT, "c": PP_ALIGN.CENTER, "r": PP_ALIGN.RIGHT}[a["align"]]
                    p.line_spacing = a["ls"]
                    if a["after"] and i < len(a["paras"]) - 1:
                        p.space_after = Pt(a["after"])
                    if a["bullet"]:
                        _set_bullet(p, *a["bullet"])
                    for t, o in runs:
                        r = p.add_run()
                        r.text = t
                        _set_run_font(r, o.get("size", a["size"]), o.get("color", a["color"]),
                                      o.get("bold", a["bold"]), o.get("font", a["font"]))
                _name(tb, a.get("name"))
            elif kind == "line":
                pts = a["pts"]
                for j in range(len(pts) - 1):
                    (x1, y1), (x2, y2) = pts[j], pts[j + 1]
                    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, _emu(x1), _emu(y1), _emu(x2), _emu(y2))
                    c.line.color.rgb = _rgb(a["color"])
                    c.line.width = Pt(a["width"] * 0.75)
                    ln = c.line._get_or_add_ln()
                    if a["dash"]:
                        etree.SubElement(ln, qn("a:prstDash")).set("val", "dash")
                    if a["arrow"] and j == len(pts) - 2:
                        tail = etree.SubElement(ln, qn("a:tailEnd"))
                        tail.set("type", "triangle")
                        tail.set("w", "med")
                        tail.set("len", "med")
            elif kind == "image":
                pic = s.shapes.add_picture(a["path"], _emu(a["x"]), _emu(a["y"]), _emu(a["w"]), _emu(a["h"]))
                _name(pic, a.get("name"))
                pic._element.nvPicPr.cNvPr.set("descr", a.get("name") or "")
            elif kind == "donut":
                data = CategoryChartData()
                data.categories = ["확보 가능", "나머지"]
                data.add_series("용량", (a["value"], 100 - a["value"]))
                gf = s.shapes.add_chart(XL_CHART_TYPE.DOUGHNUT, _emu(a["x"]), _emu(a["y"]), _emu(a["w"]), _emu(a["h"]), data)
                chart = gf.chart
                chart.has_legend = False
                chart.has_title = False
                plot = chart.plots[0]
                plot.vary_by_categories = True
                plot.has_data_labels = False
                for i, col in enumerate(a["colors"]):
                    pt = plot.series[0].points[i]
                    pt.format.fill.solid()
                    pt.format.fill.fore_color.rgb = _rgb(col)
                    pt.format.line.fill.background()
                dn = chart._chartSpace.find(".//" + qn("c:doughnutChart"))
                hs = dn.find(qn("c:holeSize"))
                if hs is None:
                    hs = etree.SubElement(dn, qn("c:holeSize"))
                hs.set("val", str(a["hole"]))
                # 차트 배경 투명
                cs = chart._chartSpace
                sppr = etree.Element(qn("c:spPr"))
                etree.SubElement(sppr, qn("a:noFill"))
                ln = etree.SubElement(sppr, qn("a:ln"))
                etree.SubElement(ln, qn("a:noFill"))
                cs.find(qn("c:chart")).addnext(sppr)
                autotitle = cs.find(qn("c:chart")).find(qn("c:autoTitleDeleted"))
                if autotitle is not None:
                    autotitle.set("val", "1")
        if sd.notes:
            s.notes_slide.notes_text_frame.text = sd.notes
    prs.save(out_path)


# ---------------------------------------------------------------- preview renderer
_font_cache = {}


def _font(kind, bold, size_px):
    key = (kind, bold, round(size_px * 4))
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(FONT_FILES[(kind, bold)], max(1, int(round(size_px * SCALE))))
    return _font_cache[key]


SCALE = 2  # 미리보기 해상도 배율


def _is_hangul(ch):
    return ord(ch) >= 0x1100


def _segments(text, kind):
    """mono 글꼴에는 한글 글리프가 없으므로 한글 구간은 kr 글꼴로 측정/그리기."""
    if kind != "mono":
        return [(text, "kr")]
    segs, cur, cur_k = [], "", None
    for ch in text:
        k = "kr" if _is_hangul(ch) else "mono"
        if k != cur_k and cur:
            segs.append((cur, cur_k))
            cur = ""
        cur, cur_k = cur + ch, k
    if cur:
        segs.append((cur, cur_k))
    return segs


def _measure(text, kind, bold, size_px):
    return sum(_font(k, bold, size_px).getlength(seg) for seg, k in _segments(text, kind)) / SCALE


def _layout_para(runs, a, width):
    """단어 단위 줄바꿈. 반환: [[(text, opts)...] per line]"""
    tokens = []
    for t, o in runs:
        parts = t.split(" ")
        for i, part in enumerate(parts):
            tok = part + (" " if i < len(parts) - 1 else "")
            if tok:
                tokens.append((tok, o))
    lines, cur, cur_w = [], [], 0.0
    for tok, o in tokens:
        size_px = o.get("size", a["size"]) * 4 / 3
        kind, bold = o.get("font", a["font"]), o.get("bold", a["bold"])
        w = _measure(tok.rstrip(" "), kind, bold, size_px)
        w_full = _measure(tok, kind, bold, size_px)
        if cur and cur_w + w > width + 0.5:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur.append((tok, o))
        cur_w += w_full
    lines.append(cur)
    return lines


def render_preview(slides, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    problems = []
    for idx, sd in enumerate(slides, 1):
        img = Image.new("RGB", (1280 * SCALE, 720 * SCALE), "#" + sd.bg)
        d = ImageDraw.Draw(img)
        S = lambda v: int(round(v * SCALE))  # noqa: E731
        for kind, a in sd.items:
            if kind in ("rect", "oval"):
                box = [S(a["x"]), S(a["y"]), S(a["x"] + a["w"]), S(a["y"] + a["h"])]
                fill = "#" + a["fill"] if a.get("fill") else None
                outline = "#" + a["line"] if a.get("line") else None
                if kind == "oval":
                    d.ellipse(box, fill=fill, outline=outline)
                elif a["radius"]:
                    d.rounded_rectangle(box, radius=S(a["radius"]), fill=fill, outline=outline, width=S(1.5))
                else:
                    d.rectangle(box, fill=fill, outline=outline, width=S(1.5))
            elif kind == "line":
                pts = [(S(x), S(y)) for x, y in a["pts"]]
                col = "#" + a["color"]
                for j in range(len(pts) - 1):
                    if a["dash"]:
                        (x1, y1), (x2, y2) = pts[j], pts[j + 1]
                        n = max(1, int(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 // S(8)))
                        for k in range(0, n, 2):
                            d.line([(x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n),
                                    (x1 + (x2 - x1) * (k + 1) / n, y1 + (y2 - y1) * (k + 1) / n)], fill=col, width=S(a["width"]))
                    else:
                        d.line([pts[j], pts[j + 1]], fill=col, width=S(a["width"]))
                if a["arrow"]:
                    (x1, y1), (x2, y2) = pts[-2], pts[-1]
                    import math
                    ang = math.atan2(y2 - y1, x2 - x1)
                    L, W = S(11), S(5)
                    p1 = (x2 - L * math.cos(ang) + W * math.sin(ang), y2 - L * math.sin(ang) - W * math.cos(ang))
                    p2 = (x2 - L * math.cos(ang) - W * math.sin(ang), y2 - L * math.sin(ang) + W * math.cos(ang))
                    d.polygon([(x2, y2), p1, p2], fill=col)
            elif kind == "image":
                im = Image.open(a["path"]).convert("RGBA").resize((S(a["w"]), S(a["h"])), Image.LANCZOS)
                img.paste(im, (S(a["x"]), S(a["y"])), im)  # 투명 배경 유지
            elif kind == "donut":
                box = [S(a["x"]), S(a["y"]), S(a["x"] + a["w"]), S(a["y"] + a["h"])]
                ring = int(a["w"] * (100 - a["hole"]) / 200 * SCALE)
                d.arc(box, -90, 270, fill="#" + a["colors"][1], width=ring)
                d.arc(box, -90, -90 + 360 * a["value"] / 100, fill="#" + a["colors"][0], width=ring)
            elif kind == "text":
                width = a["w"] - (a["bullet"][2] if a["bullet"] else 0)
                laid = []  # (line_runs, line_height_px, is_para_end)
                for pi, runs in enumerate(a["paras"]):
                    lines = _layout_para(runs, a, width)
                    for li, ln in enumerate(lines):
                        size_px = max((o.get("size", a["size"]) for _, o in ln), default=a["size"]) * 4 / 3
                        lh = size_px * 1.2 * a["ls"]
                        last = li == len(lines) - 1
                        extra = a["after"] * 4 / 3 if last and pi < len(a["paras"]) - 1 else 0
                        laid.append((ln, lh, extra, li == 0))
                total = sum(lh + ex for _, lh, ex, _ in laid)
                if total > a["h"] + 1:
                    first = "".join(t for t, _ in a["paras"][0])[:30]
                    problems.append(f"slide {idx}: 텍스트 넘침 {total:.0f}px > {a['h']}px  '{first}'")
                y = a["y"] + {"t": 0, "m": (a["h"] - total) / 2, "b": a["h"] - total}[a["valign"]]
                for ln, lh, extra, para_start in laid:
                    line_w = sum(_measure(t, o.get("font", a["font"]), o.get("bold", a["bold"]), o.get("size", a["size"]) * 4 / 3) for t, o in ln)
                    x0 = a["x"] + (a["bullet"][2] if a["bullet"] else 0)
                    if a["align"] == "c":
                        x0 = a["x"] + (a["w"] - line_w) / 2
                    elif a["align"] == "r":
                        x0 = a["x"] + a["w"] - line_w
                    if a["bullet"] and para_start:
                        bsz = a["size"] * 4 / 3
                        bf = ImageFont.truetype(r"C:\Windows\Fonts\seguisym.ttf", int(bsz * SCALE)) \
                            if a["bullet"][0] == "✓" else _font("kr", True, bsz)
                        d.text((S(a["x"]), S(y + (lh - bsz * 1.2) / 2)), a["bullet"][0], font=bf, fill="#" + a["bullet"][1])
                    x = x0
                    for t, o in ln:
                        size_px = o.get("size", a["size"]) * 4 / 3
                        kind_f, bold = o.get("font", a["font"]), o.get("bold", a["bold"])
                        col = "#" + o.get("color", a["color"])
                        for seg, k in _segments(t, kind_f):
                            f = _font(k, bold, size_px)
                            base = y + (lh - size_px * 1.2) / 2 + size_px * 0.98  # 공통 기준선
                            d.text((S(x), S(base)), seg, font=f, fill=col, anchor="ls")
                            x += f.getlength(seg) / SCALE
                    y += lh + extra
        img.resize((1280, 720), Image.LANCZOS).save(out_dir / f"slide-{idx}.png")
    return problems


# ---------------------------------------------------------------- icons (PNG, PIL로 그림)
def make_icon(name, path, color=TEAL_D):
    n = 256
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = "#" + color
    w = 18
    if name == "copy":
        d.rounded_rectangle([86, 86, 214, 214], 18, outline=c, width=w)
        d.line([(42, 170), (42, 62), (52, 46), (66, 42), (170, 42)], fill=c, width=w, joint="curve")
    elif name == "clock":
        d.ellipse([30, 30, 226, 226], outline=c, width=w)
        d.line([(128, 72), (128, 132), (168, 156)], fill=c, width=w, joint="curve")
    elif name == "download":
        d.line([(128, 30), (128, 160)], fill=c, width=w)
        d.line([(70, 104), (128, 162), (186, 104)], fill=c, width=w, joint="curve")
        d.line([(46, 218), (210, 218)], fill=c, width=w)
    elif name == "chart":
        for x, top in ((60, 120), (128, 50), (196, 150)):
            d.line([(x, 210), (x, top)], fill=c, width=w + 4)
        d.line([(24, 218), (232, 218)], fill=c, width=w)
    elif name == "trash":
        d.line([(36, 70), (220, 70)], fill=c, width=w)
        d.line([(96, 70), (96, 36), (160, 36), (160, 70)], fill=c, width=w, joint="curve")
        d.polygon([(62, 78), (194, 78), (180, 222), (76, 222)], outline=c, width=w)
    elif name == "archive":
        d.rectangle([28, 40, 228, 96], outline=c, width=w)
        d.line([(48, 96), (48, 220), (208, 220), (208, 96)], fill=c, width=w, joint="curve")
        d.line([(102, 140), (154, 140)], fill=c, width=w)
    elif name == "calendar":
        d.rounded_rectangle([32, 52, 224, 226], 18, outline=c, width=w)
        d.line([(32, 112), (224, 112)], fill=c, width=w)
        d.line([(86, 26), (86, 76)], fill=c, width=w)
        d.line([(170, 26), (170, 76)], fill=c, width=w)
    im.save(path)
    return path


# ---------------------------------------------------------------- deck content
def build_deck(assets: Path):
    icons = {k: make_icon(k, assets / f"icon_{k}.png") for k in
             ("copy", "clock", "download", "chart", "trash", "archive", "calendar")}
    report_img = HERE / "report_top.png"
    slides = []

    def header(s, kicker, title, dark=False):
        s.text(64, 48, 900, 24, kicker, size=12, bold=True, color=MINT if dark else TEAL, name="Kicker")
        s.text(64, 80, 1152, 56, title, size=32, bold=True, color=WHITE if dark else INK, name="Title")

    def pno(s, n, dark=False):
        s.text(1160, 684, 56, 18, str(n), size=10, color=MUTED_DARK if dark else MUTED, align="r", name="Page number")

    def badge(s, x, y, icon, d=52):
        s.oval(x, y, d, MINT_L, name="Icon circle")
        pad = d * 0.25
        s.image(x + pad, y + pad, d - 2 * pad, d - 2 * pad, icons[icon], name=f"{icon} icon")

    # 1. 표지 ------------------------------------------------------------
    s = Slide(bg=INK, notes="PC 용량 확보 및 백업 자동화 도구 PC Cleaner를 소개합니다. "
              "Claude Code를 개발 도구로 사용해 만들었고, 샘플 데이터에서 용량의 52%를 정리할 수 있었습니다.")
    s.text(96, 196, 720, 24, "기말 프로젝트 · LLM 코딩 에이전트 활용", size=13, bold=True, color=MINT)
    s.text(96, 232, 760, 170, ["PC Cleaner", "용량 확보 및 백업 자동화"], size=44, bold=True, color=WHITE, ls=1.0)
    s.text(96, 414, 640, 64, "중복·임시 파일을 안전하게 정리하고, 중요 폴더를 매일 자동으로 백업하는 Python CLI",
           size=16, color="BCD6DB", ls=1.1)
    s.text(96, 520, 640, 24, "jaeuk · 2026.12 · Claude Code로 개발", size=13, color=MUTED_DARK)
    s.donut(880, 196, 330, 52.4, [MINT, "18414D"], hole=74)
    s.text(880, 316, 330, 62, "52%", size=36, bold=True, color=WHITE, align="c")
    s.text(880, 378, 330, 24, "확보 가능 (샘플)", size=13, color=MUTED_DARK, align="c")
    slides.append(s)

    # 2. 기획 의도 --------------------------------------------------------
    s = Slide(notes="누구나 겪는 문제에서 출발했습니다. 같은 파일이 여러 번 저장되고, 임시 파일이 쌓이고, 백업은 생각날 때만 합니다. "
              "샘플 폴더 기준으로 52.4%를 정리할 수 있었고, 목표는 찾기, 안전한 정리, 자동 백업 세 가지입니다.")
    header(s, "01 기획 의도", "C 드라이브 정리는 매번 수동으로 한다")
    pains = [
        ("copy", "같은 파일이 여러 번 저장됨", "다운로드 폴더의 IMG_0001 (1).jpg, 바탕화면의 보고서_최종.docx 복사본"),
        ("clock", "오래된 임시 파일이 쌓임", "몇 달 지난 .tmp, .log, .bak, __pycache__. 지워도 되는지 판단이 어려움"),
        ("download", "백업은 생각날 때만", "중요 폴더를 날짜별로 남기지 않아 실수 한 번에 복구가 불가능"),
    ]
    for i, (ic, h, p) in enumerate(pains):
        y = 190 + i * 140
        badge(s, 64, y, ic)
        s.text(136, y + 2, 540, 30, h, size=18, bold=True, color=INK)
        s.text(136, y + 38, 540, 60, p, size=14, color=MUTED, ls=1.1)
    s.rect(724, 184, 492, 420, fill=INK, radius=18, name="Stat card")
    s.text(764, 210, 420, 100, "52.4%", size=60, bold=True, color=MINT)
    s.text(764, 312, 420, 48, "샘플 폴더 20.7 MB 중 정리 가능한 용량 (10.9 MB)", size=14, color=ON_DARK, ls=1.1)
    s.line([(764, 380), (1176, 380)], color="24505B", width=1, arrow=False)
    s.text(764, 400, 420, 180, [
        [("목표 1  ", {"bold": True, "color": WHITE}), ("공간을 차지하는 파일을 정확하게 찾는다", {})],
        [("목표 2  ", {"bold": True, "color": WHITE}), ("실수해도 되돌릴 수 있게 정리한다", {})],
        [("목표 3  ", {"bold": True, "color": WHITE}), ("중요 폴더를 매일 자동으로 백업한다", {})],
    ], size=14, color=ON_DARK, after=14, ls=1.1)
    pno(s, 2)
    slides.append(s)

    # 3. 주요 기능 --------------------------------------------------------
    s = Slide(notes="기능은 여섯 가지입니다. 분석, 중복 탐지, 임시 파일 탐지, 안전한 정리와 복구, 백업, 예약입니다. "
              "정리는 기본이 미리보기이고 실행해도 삭제하지 않고 격리 폴더로 옮깁니다.")
    header(s, "02 주요 기능", "명령 7개로 분석부터 정리, 백업, 예약까지")
    feats = [
        ("chart", "scan", "확장자·폴더별 용량을 집계하고 그래프가 있는 HTML 리포트를 생성"),
        ("copy", "중복 탐지", "크기 → 앞 64KB 해시 → SHA-256 3단계 비교. 가장 오래된 파일을 원본으로 보존"),
        ("clock", "임시 파일 탐지", "*.tmp, *.log, ~$*, __pycache__ 중 30일 이상 지난 파일만 선별"),
        ("trash", "clean · undo · purge", "기본은 미리보기. --apply는 격리 이동, undo로 복구, purge로 영구 삭제"),
        ("archive", "backup", "날짜별 zip 생성 후 CRC 검사, 최근 N개만 보관. 클라우드 폴더 지정 가능"),
        ("calendar", "schedule", "Windows 작업 스케줄러 등록 명령을 만들어 매일 자동 백업"),
    ]
    for i, (ic, cmd, desc) in enumerate(feats):
        x, y = 64 + (i % 3) * 392, 172 + (i // 3) * 240
        s.rect(x, y, 368, 220, fill=MIST, radius=14, name=f"{cmd} card")
        badge(s, x + 24, y + 22, ic, d=48)
        s.text(x + 24, y + 86, 320, 24, cmd, size=14, bold=True, color=TEAL, font="mono")
        s.text(x + 24, y + 120, 320, 84, desc, size=14, color=BODY, ls=1.1)
    pno(s, 3)
    slides.append(s)

    # 4. 시스템 구조 ------------------------------------------------------
    s = Slide(notes="scanner가 파일을 모으면 duplicates와 temp_finder가 정리 후보를 찾고, cleaner가 정리 계획을 만듭니다. "
              "미리보기면 리포트만 만들고, 실행하면 격리 폴더로 옮깁니다. 백업은 별도 흐름으로 zip을 만들어 동기화 폴더에 저장합니다.")
    header(s, "03 시스템 구조", "모듈 7개, 표준 라이브러리만으로 구성")

    def box(x, y, w, h, title, sub, fill=MINT_L, line=TEAL, tcol=INK, scol=MUTED):
        s.rect(x, y, w, h, fill=fill, radius=12, line=line)
        s.text(x + 8, y + 10, w - 16, 26, title, size=14, bold=True, color=tcol, align="c")
        s.text(x + 8, y + h - 30, w - 16, 20, sub, size=11, color=scol, align="c")

    box(64, 310, 150, 76, "대상 폴더", "사용자 PC", fill=INK, line=None, tcol=WHITE, scol=MUTED_DARK)
    box(264, 310, 180, 76, "scanner", "파일 수집·용량 집계")
    box(494, 210, 200, 76, "duplicates", "3단계 해시 비교")
    box(494, 410, 200, 76, "temp_finder", "패턴 + 경과 일수")
    box(744, 310, 170, 76, "cleaner", "정리 계획")
    box(964, 170, 252, 64, "report", "dry-run · HTML/JSON", fill=MIST, line=LINE)
    box(964, 316, 252, 64, "격리 폴더", "--apply · manifest.json", fill=MIST, line=LINE)
    s.rect(964, 420, 118, 48, fill=WHITE, radius=10, line=MINT)
    s.text(964, 420, 118, 48, "undo", size=14, bold=True, color=TEAL_D, align="c", valign="m")
    s.rect(1098, 420, 118, 48, fill=WHITE, radius=10, line="E4572E")
    s.text(1098, 420, 118, 48, "purge", size=14, bold=True, color=WARN, align="c", valign="m")
    s.text(964, 476, 118, 20, "원위치 복구", size=11, color=MUTED, align="c")
    s.text(1098, 476, 118, 20, "실제 용량 확보", size=11, color=MUTED, align="c")
    box(264, 560, 180, 76, "backup", "zip · CRC · 보관 N개", fill=TEAL, line=None, tcol=WHITE, scol="D4EFEF")
    box(494, 560, 380, 76, "백업 폴더 / 클라우드 동기화 폴더", "OneDrive · Google Drive · Dropbox", fill=MIST, line=LINE)
    box(924, 560, 200, 76, "schedule", "작업 스케줄러 매일 실행", fill=MIST, line=LINE)
    s.line([(214, 348), (262, 348)])
    s.line([(444, 335), (469, 335), (469, 248), (492, 248)])
    s.line([(444, 361), (469, 361), (469, 448), (492, 448)])
    s.line([(694, 248), (719, 248), (719, 335), (742, 335)])
    s.line([(694, 448), (719, 448), (719, 361), (742, 361)])
    s.line([(914, 335), (939, 335), (939, 202), (962, 202)])
    s.line([(914, 348), (962, 348)])
    s.line([(1023, 380), (1023, 418)])
    s.line([(1157, 380), (1157, 418)])
    s.line([(139, 386), (139, 598), (262, 598)])
    s.line([(444, 598), (492, 598)])
    s.line([(924, 598), (876, 598)], dash=True)
    pno(s, 4)
    slides.append(s)

    # 5. 핵심 설계 --------------------------------------------------------
    s = Slide(notes="중복 탐지는 크기, 앞부분 해시, 전체 해시 순으로 후보를 줄여서 빠릅니다. "
              "정리는 미리보기, 격리 이동, 복구 순서이고 실제 삭제는 purge에서만 일어납니다. 실행 직전에 파일을 다시 확인합니다.")
    header(s, "04 핵심 설계", "빠르게 찾고, 절대 함부로 지우지 않는다")
    s.text(64, 176, 540, 30, "중복 탐지: 후보를 단계별로 줄이기", size=18, bold=True, color=INK)
    steps = [(INK, 64, 540, "① 파일 크기 비교", "디스크 읽기 없음"),
             (TEAL_D, 96, 476, "② 앞 64KB 해시", "대부분 여기서 걸러짐"),
             (TEAL, 128, 412, "③ 전체 SHA-256", "중복 확정")]
    for i, (col, x, w, a_, b_) in enumerate(steps):
        y = 224 + i * 80
        s.rect(x, y, w, 64, fill=col, radius=10)
        s.text(x + 22, y, 240, 64, a_, size=15, bold=True, color=WHITE, valign="m")
        s.text(x + w - 222, y, 200, 64, b_, size=12, color="D7EEF0", align="r", valign="m")
    s.text(64, 478, 540, 80, [[("크기만 같거나 앞부분만 같은 파일은 중복이 아님을 테스트로 검증했습니다. 남기는 원본은 ", {}),
                               ("가장 오래된 파일", {"bold": True, "color": INK}), ("입니다.", {})]],
           size=14, color=MUTED, ls=1.15)
    s.text(660, 176, 556, 30, "안전한 정리 흐름", size=18, bold=True, color=INK)
    nodes = [(660, "clean", "미리보기", MIST, LINE, TEAL_D), (850, "--apply", "격리 이동", MIST, LINE, TEAL_D),
             (1040, "undo", "원위치 복구", MIST, LINE, TEAL_D)]
    for x, t, sub, f, l, c in nodes:
        s.rect(x, 224, 150, 72, fill=f, radius=10, line=l)
        s.text(x, 234, 150, 26, t, size=15, bold=True, color=c, align="c")
        s.text(x, 264, 150, 22, sub, size=12, color=BODY, align="c")
    s.line([(810, 260), (848, 260)])
    s.line([(1000, 260), (1038, 260)])
    s.rect(850, 326, 150, 72, fill="FFF6F3", radius=10, line="F3B4A1")
    s.text(850, 336, 150, 26, "purge --yes", size=15, bold=True, color=WARN, align="c")
    s.text(850, 366, 150, 22, "영구 삭제", size=12, color=BODY, align="c")
    s.line([(925, 296), (925, 324)])
    s.text(660, 430, 556, 200, [
        [("실행 직전 ", {}), ("크기·수정 시간 재확인", {"bold": True, "color": INK}), (", 바뀌면 건너뜀", {})],
        [("중복 원본이 사라졌으면 복사본은 ", {}), ("보존", {"bold": True, "color": INK})],
        [("undo 때 같은 이름 파일이 있으면 ", {}), ("덮어쓰지 않음", {"bold": True, "color": INK})],
        ".git, .venv, 휴지통, 심볼릭 링크는 스캔 제외",
    ], size=14, color=BODY, after=10, bullet=("✓", MINT, 26))
    pno(s, 5)
    slides.append(s)

    # 6. LLM 활용 ---------------------------------------------------------
    s = Slide(notes="Claude Code에 입력한 핵심 프롬프트 세 개입니다. 프로젝트 분석, 기록하면서 개발, 수정 가능한 발표 자료 작성 순서였습니다. "
              "사람은 방향과 공개 범위를 정하고, LLM은 코드와 테스트, 문서, 오류 분석을 맡았습니다.")
    header(s, "05 LLM 활용 과정", "핵심 프롬프트 3개로 분석부터 발표 자료까지")
    prompts = [
        ("‘PC 정리 백업’에 대한 프로젝트 분석",
         ["과제 요건을 체크리스트로 정리", "위험(실수 삭제)과 대책: dry-run, 격리, 샘플 생성기", "LLM은 개발 도구, 결과물은 순수 Python"]),
        ("개발하면서 prompt_log.md로 저장하면서 개발 진행",
         ["설계와 모듈 7개 구현, 문제 9건 해결", "테스트 13개, GitHub Actions CI, README", "과정을 prompt_log.md에 계속 기록"]),
        ("발표자료 수정 가능하게 ppt로 작성",
         ["편집 가능한 9장 PPTX와 발표자 노트", "미리보기 렌더러로 글자 넘침 점검", "PPTX 파일 구조 검증 통과"]),
    ]
    s.line([(86, 198), (1130, 198)], color=LINE, width=2, arrow=False)
    for i, (q, results) in enumerate(prompts):
        x = 64 + i * 392
        s.oval(x, 176, 44, TEAL)
        s.text(x, 176, 44, 44, str(i + 1), size=16, bold=True, color=WHITE, align="c", valign="m")
        s.rect(x, 238, 368, 254, fill=MIST, radius=14)
        s.text(x + 24, 258, 320, 64, f"“{q}”", size=17, bold=True, color=INK, ls=1.05)
        s.line([(x + 24, 334), (x + 344, 334)], color=LINE, width=1, arrow=False)
        s.text(x + 24, 346, 320, 142, results, size=13, color=BODY, ls=1.1, after=8, bullet=("•", TEAL, 18))
    s.rect(64, 516, 560, 84, fill=WHITE, radius=12, line=LINE)
    s.text(88, 516, 512, 84, [[("사람  ", {"bold": True, "color": TEAL_D}),
                               ("주제 선택, 요구사항 결정, 결과 검토, 공개 범위 판단(경로 속 사용자명 가리기)", {})]],
           size=14, color=BODY, valign="m", ls=1.1)
    s.rect(656, 516, 560, 84, fill=WHITE, radius=12, line=LINE)
    s.text(680, 516, 512, 84, [[("LLM  ", {"bold": True, "color": TEAL_D}),
                                ("코드·테스트·문서 작성, 오류 원인 분석, 스스로 코드 검토", {})]],
           size=14, color=BODY, valign="m", ls=1.1)
    s.text(64, 618, 900, 22, "전체 대화와 판단 근거는 저장소의 docs/prompt_log.md에 기록", size=12, color=MUTED)
    pno(s, 6)
    slides.append(s)

    # 7. 디버깅 -----------------------------------------------------------
    s = Slide(notes="개발 중 만난 문제 네 가지입니다. 두 가지는 LLM이 실행 전에 스스로 코드를 검토하다 찾았고, "
              "나머지는 재현성과 설계 문제였습니다. 버그마다 테스트를 추가했습니다.")
    header(s, "06 디버깅 과정", "실제로 만난 문제 4가지와 해결")
    bugs = [
        ("버그 · 코드 검토에서 발견", WARN_BG, WARN, "모든 파일이 \u201cTemp 폴더 안\u201d으로 판정",
         [("폴더 이름을 절대 경로 전체에서 찾아서 AppData\\Local\\Temp 아래를 스캔하면 일반 문서까지 임시 파일로 잡힘 → ", {}),
          ("스캔 루트 기준 상대 경로", {"bold": True, "color": TEAL_D}), ("로 검사, 회귀 테스트 추가", {})]),
        ("위험 · 코드 검토에서 발견", WARN_BG, WARN, "샘플 생성기가 폴더를 통째로 삭제",
         [("--out에 문서 폴더를 잘못 넣으면 rmtree로 삭제됨 → 스크립트가 만든 ", {}),
          ("표식 파일이 있는 폴더만", {"bold": True, "color": TEAL_D}), (" 다시 생성", {})]),
        ("재현성", MINT_L, TEAL_D, "Git은 파일 수정 날짜를 보존하지 않음",
         [("\u201c60일 지난 .tmp\u201d를 커밋해도 clone하면 새 파일이 되어 탐지 0개 → 샘플은 ", {}),
          ("생성 스크립트로 날짜 지정", {"bold": True, "color": TEAL_D}), ("(seed 고정)", {})]),
        ("설계", MINT_L, TEAL_D, "격리 이동만으로는 용량이 늘지 않음",
         [("같은 드라이브 안의 이동은 이름 변경일 뿐 → 확인 후 비우는 ", {}),
          ("purge 명령 추가", {"bold": True, "color": TEAL_D}), (". 백업 폴더가 원본 안에 있을 때 자기 자신을 압축하는 문제도 해결", {})]),
    ]
    for i, (tag, tbg, tcol, h, body) in enumerate(bugs):
        x, y = 64 + (i % 2) * 592, 172 + (i // 2) * 240
        s.rect(x, y, 560, 220, fill=MIST, radius=14)
        tw = _measure(tag, "kr", True, 11 * 4 / 3) + 28
        s.rect(x + 24, y + 22, tw, 26, fill=tbg, radius=13)
        s.text(x + 24, y + 22, tw, 26, tag, size=11, bold=True, color=tcol, align="c", valign="m")
        s.text(x + 24, y + 62, 512, 28, h, size=17, bold=True, color=INK)
        s.text(x + 24, y + 102, 512, 104, [body], size=14, color=BODY, ls=1.15)
    pno(s, 7)
    slides.append(s)

    # 8. 실행 결과 --------------------------------------------------------
    s = Slide(notes="샘플 데이터 26개 파일에서 중복 4그룹과 오래된 임시 파일 7개를 찾아 10.9MB를 확보할 수 있었습니다. "
              "정리 후 undo로 12개 파일을 모두 되돌렸고, 백업은 보관 개수만큼만 남았습니다. 오른쪽은 HTML 리포트입니다.")
    header(s, "07 실행 결과", "샘플 데이터 26개 파일에서 10.9 MB 확보")
    s.rect(64, 164, 548, 462, fill="0F1E24", radius=12, name="Terminal")
    G, Y, Cc, W_ = {"color": "8FA8AE"}, {"color": "F6C453"}, {"color": MINT}, {}
    term = [
        [("> python main.py scan sample_data", Cc)],
        [("[스캔] jaeuk/sample_data  파일 26개, 20.7 MB", W_)],
        [("[중복 파일] ", Y), ("4개 그룹, 낭비 9.1 MB", W_)],
        [("  남김  videos\\demo_recording.mp4", W_)],
        [("   └ 정리 desktop\\backup_copy\\demo_recording.mp4", W_)],
        [("  남김  photos\\2026_trip\\IMG_0001.jpg", W_)],
        [("   └ 정리 downloads\\IMG_0001.jpg", W_)],
        [("   └ 정리 downloads\\IMG_0001 (1).jpg", W_)],
        [("[오래된 임시 파일] ", Y), ("7개, 1.7 MB", W_)],
        [("  downloads\\chrome_download.tmp  (200일)", W_)],
        [("[확보 가능 용량] ", Y), ("10.9 MB (파일 12개)", W_)],
        [("> python main.py clean sample_data --apply", Cc)],
        [("[완료] 12개 파일(10.9 MB)을 격리 폴더로 이동", W_)],
        [("> python main.py undo", Cc)],
        [("[복구] 12개 파일을 원래 위치로 되돌렸습니다", W_)],
        [("> python main.py backup sample_data/work --keep 2", Cc)],
        [("  1.1 MB → 872.4 KB ", W_), ("(3회 실행 → 최신 2개만 보관)", G)],
    ]
    s.text(88, 186, 508, 424, term, size=11, color="D7E7EA", font="mono", ls=1.12, after=3)
    iw = 576
    ih = round(iw * 880 / 1100)
    s.rect(640 - 1, 164 - 1, iw + 2, ih + 2, fill=LINE, radius=4)
    s.image(640, 164, iw, ih, report_img, name="HTML 리포트 화면: 요약 카드와 확장자별, 폴더별 용량 그래프")
    s.text(640, 164 + ih + 12, iw, 22, "scan --report로 생성한 HTML 리포트 (요약 카드, 용량 그래프, 중복 목록)", size=11, color=MUTED)
    pno(s, 8)
    slides.append(s)

    # 9. 검증과 결론 ------------------------------------------------------
    s = Slide(bg=INK, notes="테스트 13개가 모두 통과했고, 정리한 12개 파일을 모두 복구했습니다. GitHub Actions CI도 4개 환경에서 모두 통과했습니다. "
              "LLM은 빠르게 코드를 만들지만 위험한 동작의 기준은 사람이 정해야 한다는 점을 배웠습니다.")
    header(s, "08 검증과 결론", "테스트로 안전성을 확인하고, 누구나 바로 실행할 수 있게", dark=True)
    kpis = [("13 / 13", "pytest 테스트 통과"), ("12 → 12", "정리 후 undo로 복구한 파일"),
            ("4 / 4", "CI 통과 (OS 2종 × Python 2버전)"), ("0개", "핵심 기능의 외부 라이브러리")]
    for i, (v, l) in enumerate(kpis):
        x = 64 + i * 292
        s.rect(x, 172, 274, 160, fill=DARK2, radius=14)
        s.text(x + 24, 192, 230, 60, v, size=34, bold=True, color=MINT)
        s.text(x + 24, 256, 230, 72, l, size=13, color=ON_DARK, ls=1.1)
    s.text(64, 368, 540, 36, "배운 점", size=20, bold=True, color=WHITE)
    s.text(64, 416, 540, 220, [
        [("LLM은 빠르게 코드를 만들지만 ", {}), ("위험한 동작의 기준은 사람이 정해야", {"bold": True, "color": WHITE}), (" 함", {})],
        "LLM의 자체 코드 검토 단계에서 버그·위험 2건을 실행 전에 잡음",
        "버그마다 테스트를 추가해 같은 실수를 막음",
    ], size=15, color=ON_DARK, after=12, ls=1.1, bullet=("•", MINT, 22))
    s.text(676, 368, 540, 36, "향후 개선", size=20, bold=True, color=WHITE)
    s.text(676, 416, 540, 220, [
        "Windows 휴지통 연동, 확장자별 정리 규칙 설정 파일",
        "증분 백업(변경된 파일만)과 백업 복원 명령",
        "간단한 GUI 또는 트레이 아이콘",
    ], size=15, color=ON_DARK, after=12, ls=1.1, bullet=("•", MINT, 22))
    pno(s, 9, dark=True)
    slides.append(s)
    return slides


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "presentation.pptx"))
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--preview-dir", default=str(HERE / "preview"))
    args = ap.parse_args()
    assets = HERE / "assets"
    assets.mkdir(exist_ok=True)
    slides = build_deck(assets)
    write_pptx(copy.deepcopy(slides), args.out)
    print(f"PPTX 저장: {args.out}")
    if args.preview:
        problems = render_preview(slides, Path(args.preview_dir))
        print(f"미리보기 저장: {args.preview_dir}")
        for p in problems:
            print("  [경고]", p)
        if problems:
            sys.exit(1)


if __name__ == "__main__":
    os.chdir(ROOT)
    main()

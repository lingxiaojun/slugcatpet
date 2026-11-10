"""珍珠：普通的灰珍珠可以右键写上文字、选颜色；写过的珍珠变色，悬停显示文字，静止时头上浮一个符文。"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QRadialGradient,
    QTextLayout,
    QTextOption,
)

from ...core.units import lerp
from ...rendering.primitives import blit
from ..base import CARRYABLE, TOY, ItemKind, dump_loose
from ..state import ItemState
from .model import GLYPH_COUNT, PLAIN_RGB, TEXT_MAX, Pearl

PALETTE = (
    (230, 242, 51),
    (230, 153, 26),
    (255, 26, 26),
    (255, 0, 140),
    (102, 26, 230),
    (2, 51, 255),
    (0, 179, 128),
    (0, 179, 26),
)

DRAW_SCALE = 1.0
GLOW_RADIUS = 10.0
GLOW_ALPHA = 0.5
GLYPH_SCALE = 1.5
GLYPH_LIFT = 12.5  # 符文中心到珍珠中心的距离
GLYPH_BOB = 1.2
GLYPH_HALF = 5.5  # 符文画出来的半高

HOVER_PAD = 8.0
HOVER_DELAY = 14  # 光标停在珍珠上多少 tick 后显示文字
HOVER_FADE = 6
NOTE_MIN_PX = 12.0  # 文字字号下限；平时跟系统界面字号
NOTE_SHORT = 12  # 不超过这么多字、一行放得下的放大显示
NOTE_SHORT_SCALE = 1.25
NOTE_LINE_EM = 20  # 每行最多约这么多个汉字宽
NOTE_MAX_LINES = 10
NOTE_PAD = 7.0
NOTE_GAP = 8.0  # 文字框和珍珠（有符文时是符文）之间的空隙
NOTE_EDGE = 6.0
NOTE_RADIUS = 6.0
NOTE_BG = (0, 0, 0, 175)
NOTE_TEXT = (255, 255, 255, 235)
_WHITE = (255, 255, 255)
_NOTE_ALIGN = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop


def _mix(c, d, k):
    return tuple(int(lerp(a, b, k)) for a, b in zip(c, d))


def _clean_color(v):
    try:
        r, g, b = (max(0, min(255, int(c))) for c in v)
    except (TypeError, ValueError, OverflowError):
        return None
    return (r, g, b)


class PearlKind(ItemKind):
    key = "pearl"
    store = "pearls"
    affords = frozenset({CARRYABLE, TOY})
    varies = True
    secondary = True
    collides = True
    grab_pad = 7.0
    grab_rank = 1
    dead_state = ItemState.GONE

    def make(self, win, lx, ly, seed):
        return Pearl(lx, ly, seed=seed)

    def dump(self, o):
        return dump_loose(o, text=o.text, color=list(o.color) if o.color else None, glyph=o.glyph)

    def load(self, win, d, fit):
        x, y = float(d["x"]), float(d["y"])
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        o = Pearl(*fit.inside(x, y), seed=int(d.get("seed", 0)))
        color = _clean_color(d.get("color"))
        if color is not None:
            o.color = color
            o.text = str(d.get("text") or "")[:TEXT_MAX]
            g = d.get("glyph")
            o.glyph = g if type(g) is int and 0 <= g < GLYPH_COUNT else None
        return o

    def affords_now(self, o, use):
        return not o.editing

    def grabbable(self, o):
        return not o.editing and super().grabbable(o)

    def hit(self, o, cx, cy):
        return None if o.editing else super().hit(o, cx, cy)

    def keeps(self, o):
        return o.written or o.editing

    def removal_note(self, o):
        return o.text if o.written else None

    def drop(self, win, o):
        dlg = win._item_dialog
        if dlg is not None and getattr(dlg, "pearl", None) is o:
            dlg.reject()
        for pet in win.pets:
            pet.body.release(o)
        o.state = ItemState.GONE

    def on_secondary(self, win, o, global_pos):
        if win._item_dialog is not None:
            return
        from ...ui.panels.pearl_editor import PearlEditor

        for pet in win.pets:
            pet.body.release(o, to_free=True)
        o.editing = True
        o.hover_ticks = 0
        dlg = PearlEditor(
            o.text, o.color or PALETTE[0], o.glyph is not None or not o.written, PALETTE, TEXT_MAX, parent=win
        )
        dlg.pearl = o
        dlg.finished.connect(lambda result, o=o, dlg=dlg: self._edited(win, o, dlg, result))
        win._item_dialog = dlg
        dlg.place_center()
        dlg.show()
        dlg.activateWindow()
        dlg.focus_text()

    def _edited(self, win, o, dlg, result):
        from PySide6.QtWidgets import QDialog

        win._item_dialog = None
        o.editing = False
        if result == QDialog.DialogCode.Accepted and o.state != ItemState.GONE:
            o.text = dlg.text()
            o.color = dlg.color()
            if not dlg.glyph_on():
                o.glyph = None
            elif o.glyph is None:
                o.glyph = random.randrange(GLYPH_COUNT)
        dlg.deleteLater()
        win.update()

    def after_step(self, win):
        """光标停在写了字的珍珠上计时，用来显示文字。"""
        cur = None
        if win._drag is None and any(o.text for o in win.pearls):
            cur = win.cursor_logical()
        for o in win.pearls:
            near = (
                cur is not None
                and o.text
                and o.state == ItemState.FREE
                and not o.editing
                and (cur[0] - o.x) ** 2 + (cur[1] - o.y) ** 2 <= (o.rad + HOVER_PAD) ** 2
            )
            o.hover_ticks = o.hover_ticks + 1 if near else 0

    def draw(self, win, p):
        self._draw(win, p, False)

    def draw_behind(self, win, p):
        self._draw(win, p, True)

    def _draw(self, win, p, behind):
        ts = win._ts
        for o in win.pearls:
            if (o.behind and o.state == ItemState.CARRIED) != behind:
                continue
            x = o.last_x + (o.x - o.last_x) * ts
            y = o.last_y + (o.y - o.last_y) * ts
            g = o.last_glimmer + (o.glimmer - o.last_glimmer) * ts
            _draw_pearl(p, win.atlas, x, y, o.rgb(), g)
            if o.glyph is not None and o.glyph_fade > 0.0:
                _draw_glyph(p, win.atlas, x, y, o, win.world_tick + ts)

    def draw_overlay(self, win, p):
        for o in win.pearls:
            a = (o.hover_ticks - HOVER_DELAY) / HOVER_FADE
            if a > 0.0:
                _draw_note(win, p, o, min(1.0, a))

    def draw_hint(self, win, p, cx, cy):
        p.save()
        p.setOpacity(0.5)
        _draw_pearl(p, win.atlas, cx, cy, PLAIN_RGB, 0.0)
        p.restore()

    def paint_icon(self, p, r, atlas):
        from ...ui.widgets.iconkit import ICON_GREY

        d = min(r.width(), r.height()) * 0.62
        cx, cy = r.center().x(), r.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ICON_GREY)
        p.drawEllipse(QPointF(cx, cy), d * 0.5, d * 0.5)
        p.setBrush(QColor(255, 255, 255, 230))
        p.drawEllipse(QPointF(cx - d * 0.14, cy - d * 0.16), d * 0.11, d * 0.11)


def _draw_pearl(p, atlas, x, y, rgb, glimmer):
    s = DRAW_SCALE
    if glimmer > 0.02:
        r = GLOW_RADIUS * glimmer
        grad = QRadialGradient(QPointF(x, y), r)
        grad.setColorAt(0.0, QColor(*_mix(rgb, _WHITE, 0.5), int(255 * GLOW_ALPHA * glimmer)))
        grad.setColorAt(1.0, QColor(*rgb, 0))
        p.save()
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(x, y), r, r)
        p.restore()
    blit(p, atlas, "JetFishEyeA", x, y, 0.0, s, s, _mix(rgb, _WHITE, glimmer))
    star = _mix(_mix(rgb, _WHITE, 0.3), _WHITE, 0.4 + 0.6 * glimmer)
    blit(p, atlas, "tinyStar", x - 0.5 * s, y - 1.5 * s, 0.0, s, s, star)


def _draw_glyph(p, atlas, x, y, o, phase):
    bob = math.sin(phase * 0.08 + o._id) * GLYPH_BOB
    p.save()
    p.setOpacity(o.glyph_fade)
    blit(
        p,
        atlas,
        f"TinyGlyph{o.glyph}",
        x,
        y - GLYPH_LIFT + bob,
        0.0,
        GLYPH_SCALE,
        GLYPH_SCALE,
        _mix(o.rgb(), _WHITE, 0.25),
    )
    p.restore()


def _compact(text):
    """悬停显示用：连续的空行并成一个换行，去掉首尾空白。"""
    return "\n".join(ln for ln in text.replace("\r", "").split("\n") if ln.strip()).strip()


def _note_font(text):
    """跟系统界面字体走；短句放大。"""
    f = QFont(QGuiApplication.font())
    px = max(NOTE_MIN_PX, f.pointSizeF() * 96.0 / 72.0) if f.pointSizeF() > 0 else NOTE_MIN_PX
    if len(text) <= NOTE_SHORT and "\n" not in text:
        px *= NOTE_SHORT_SCALE
    f.setPixelSize(round(px))
    return f


def _layout_note(text, font, HL):
    fm = QFontMetricsF(font)
    ls = fm.lineSpacing()
    wrap = fm.horizontalAdvance("中") * NOTE_LINE_EM
    room = min(HL - NOTE_GAP - NOTE_PAD * 2.0 - 8.0, ls * NOTE_MAX_LINES)
    cap = max(1, int(room // ls))
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    out = []
    paras = text.split("\n")
    for pi, para in enumerate(paras):
        lay = QTextLayout(para, font)
        lay.setTextOption(opt)
        lay.beginLayout()
        while len(out) < cap:
            ln = lay.createLine()
            if not ln.isValid():
                break
            ln.setLineWidth(wrap)
            a, n = ln.textStart(), ln.textLength()
            if len(out) == cap - 1:
                if a + n < len(para):
                    out.append(fm.elidedText(para[a:], Qt.TextElideMode.ElideRight, wrap))
                    break
                if pi < len(paras) - 1:
                    out.append(fm.elidedText(para[a:].rstrip() + "…", Qt.TextElideMode.ElideRight, wrap))
                    break
            out.append(para[a : a + n].rstrip())
        lay.endLayout()
        if len(out) >= cap:
            break
    w = max((fm.horizontalAdvance(s) for s in out), default=0.0)
    return out, w, ls


def _draw_note(win, p, o, alpha):
    p.save()
    k = win._scale or 1
    p.scale(1.0 / k, 1.0 / k)
    key = (o.text, win._HL, k, QGuiApplication.font().key())
    if o.note_cache is None or o.note_cache[0] != key:
        text = _compact(o.text)
        font = _note_font(text)
        o.note_cache = (key, font, *_layout_note(text, font, win._HL * k))
    _, font, lines, bw, ls = o.note_cache
    p.setFont(font)
    w, h = bw + NOTE_PAD * 2.0, ls * len(lines) + NOTE_PAD * 2.0
    x = min(max(o.x * k - w * 0.5, NOTE_EDGE), win._WL * k - w - NOTE_EDGE)
    above = GLYPH_LIFT + GLYPH_HALF + GLYPH_BOB if o.glyph is not None and o.glyph_fade > 0.0 else o.rad
    y = (o.y - above) * k - NOTE_GAP - h
    if y < NOTE_EDGE:
        y = (o.y + o.rad) * k + NOTE_GAP
    p.setOpacity(alpha)
    p.setBrush(QColor(*NOTE_BG))
    p.setPen(QColor(*o.rgb(), 200))
    p.drawRoundedRect(QRectF(x, y, w, h), NOTE_RADIUS, NOTE_RADIUS)
    p.setPen(QColor(*NOTE_TEXT))
    for i, s in enumerate(lines):
        p.drawText(QRectF(x + NOTE_PAD, y + NOTE_PAD + i * ls, bw + 1.0, ls), int(_NOTE_ALIGN), s)
    p.restore()


KIND = PearlKind()

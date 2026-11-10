"""雨循环表盘：业力符号外一圈雨 pip 的小浮窗；空闲时只有暗着的符号，滚轮调时长时预览，单击开始，拖动移位，右键开卡片，晴天滴答脉动，雨停时播业力梯。"""

from __future__ import annotations
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QLabel, QWidget

from ...platform.topmost import raise_topmost
from ...world.raintimer import DRY, DRY_MAX, IDLE, KARMA_TOP, RAIN
from ..theme import qss
from .ladder import KarmaLadder, inv_lerp, lerp, lerp_and_tick
from .card import RainCard
from .text import status_line
from .pips import DRAIN, HOLLOW, PipRing
from .sound import KarmaSounds

SIZE = 200  # 窗口边长（逻辑 px）
TICK_MS = 25  # 动画 40 tick/s，与原版一致
POLL_MS = 1000
POP_MS = 4000  # 事件时弹出多久
START_POP_MS = 2500
ENV_POPS = ("rain", "clear", "key")  # 别的程序全屏时只为这几件事弹出
HOVER_HOLD_MS = 1200  # 鼠标离开后再留多久
PREVIEW_MS = 1500  # 空闲时滚轮调完，空心 pip 预览再留多久
IDLE_LIT = 0.4  # 空闲时业力符号的亮度
LIT_EASE = 0.15
TIP_DELAY_MS = 350
VIS_IN, VIS_OUT = 1.0 / 10.0, 1.0 / 12.0
BACKDROP_R = 64.0
EDGE_IN, EDGE_OUT = 78.0, 100.0  # 外缘渐隐，光晕和波纹不被窗口边切断
SYMBOL_HALF = 31.5
HUD_RADIUS = 22.5  # 原版 HUD 业力圈半径；pip 的尺寸按它和大符号的比例放大
RAD_START = 17.0  # 业力符号出现时从这个半径长到 HUD_RADIUS
TICK_TOCK = 240  # 滴答周期，半周期一响一脉动
PIP_SCALE = SYMBOL_HALF / HUD_RADIUS
WHEEL_STEP = 60
WHEEL_MAX = 120 * 60
DRAG_THRESH = 3
LADDER_FADE = 1.0 / 12.0
LADDER_LEAD = 12  # 业力梯淡入后停几 tick 再开始滚
LADDER_HOLD = 50  # 落定后停几 tick 再收起
LADDER_WINDOW = 50.0  # 业力梯透过这么大的圆窗看
FADE_CIRCLE_GAIN = 3.0  # 原版波纹圈靠着色器画得更实，这里加亮

WHITE = QColor.fromHslF(0.0, 0.0, 1.0)
MEDIUM_GREY = QColor.fromHslF(0.73055553, 0.08, 0.67)
DARK_GREY = QColor.fromHslF(0.73055553, 0.08, 0.3)
TINT_STEPS = 16


def mix(a: QColor, b: QColor, f) -> QColor:
    f = max(0.0, min(1.0, f))
    return QColor.fromRgbF(
        lerp(a.redF(), b.redF(), f), lerp(a.greenF(), b.greenF(), f), lerp(a.blueF(), b.blueF(), f)
    )


def karma_frame(k) -> str:
    return f"karma{k}" if k < 5 else f"karma{k}-{KARMA_TOP}"


class RainDial(QWidget):
    def __init__(self, timer, atlas, cfg, muted=lambda: False, context=lambda: None):
        """context() 返回 None、"env"（别的程序全屏）或 "frozen"（定格并收起界面）。"""
        super().__init__()
        self.timer = timer
        self._atlas = atlas
        self._cfg = cfg
        self._context = context
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(SIZE, SIZE)
        self._place()

        self.vis = 0.0
        self.ring = PipRing()
        self.ladder: KarmaLadder | None = None
        self.ladder_alpha = 0.0
        self._ladder_ticks = 0
        self._celebrate = False
        self._shown_karma = timer.karma
        self._pop_until = 0.0
        self._hover = False
        self._hover_until = 0.0
        self._menu_open = False
        self._press = None
        self._dragging = False
        self._wheel_acc = 0
        self._preview_until = 0.0
        self._lit = 1.0 if timer.phase != IDLE else IDLE_LIT
        self._rad = RAD_START
        self._drawn_look = None
        self._ticks = 0
        self._tip_until = 0.0
        self.on_poll = []  # 每秒回调，托盘据此刷新
        self.on_wanted = []  # 常驻开关变了时回调，设置页据此同步

        self._sounds = KarmaSounds(muted, self)
        self._tip = QLabel("", None)
        self._tip.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.ToolTip
        )
        self._tip.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._tip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._tip.setStyleSheet(qss.TOAST)
        self._tip.hide()
        self._tip_at = None

        self._anim = QTimer(self)
        self._anim.setInterval(TICK_MS)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._step)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        self._poll.start()
        timer.listeners.append(self._on_event)
        if self.wanted:
            self._wake()

    # 显隐

    @property
    def wanted(self) -> bool:
        """用户要它一直显示。"""
        return bool(self._cfg.get("dial_visible", True))

    def set_wanted(self, on):
        on = bool(on)
        if on == self.wanted:
            return
        self._cfg["dial_visible"] = on
        self._wake()
        for fn in list(self.on_wanted):
            fn(on)

    def pop(self, ms=POP_MS, event=None):
        """弹出一会；全屏时也盖在上面，定格时不弹。"""
        ctx = self._context()
        if ctx == "frozen" or (ctx == "env" and event not in ENV_POPS):
            return
        self._pop_until = max(self._pop_until, self._now_ms() + ms)
        self._wake()
        raise_topmost(self)

    def peek(self):
        """快捷键：弹出一会并直接显示读数。"""
        self.pop(event="key")
        self._tip_until = self._now_ms() + POP_MS

    def open_card(self):
        """在表盘旁边打开雨循环卡片，开着时表盘不收起。"""
        c = self.frameGeometry().center()
        r = int(BACKDROP_R)
        anchor = QRect(c - QPoint(r, r), QSize(2 * r, 2 * r))
        self._menu_open = True
        self._tip.hide()
        RainCard.open_for(self.timer, self, anchor, ("right", "left", "above", "below"), self._card_closed)

    def _card_closed(self):
        self._menu_open = False
        self._hover_until = self._now_ms() + HOVER_HOLD_MS
        self._wake()

    def _should_show(self) -> bool:
        ctx = self._context()
        if ctx == "frozen":
            return False
        now = self._now_ms()
        return (
            (self.wanted and ctx is None)
            or now < self._pop_until
            or self._hover
            or now < self._hover_until
            or self._menu_open
            or self._celebrate
        )

    def _wake(self):
        if not self._anim.isActive():
            self._anim.start()
        if not self.isVisible() and self._should_show():
            self.show()

    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000.0

    def _place(self):
        pos = self._cfg.get("dial_pos")
        scr = QGuiApplication.primaryScreen()
        area = scr.availableGeometry() if scr is not None else None
        if isinstance(pos, list) and len(pos) == 2:
            self.move(int(pos[0]), int(pos[1]))
        elif area is not None:
            self.move(area.right() - SIZE - 40, area.bottom() - SIZE - 40)

    # 时钟

    def _on_poll(self):
        self.timer.poll()
        if not self._anim.isActive() and self._should_show():
            self._wake()
        for fn in list(self.on_poll):
            fn()

    def _on_event(self, ev):
        if ev == "half":
            self.ring.start_blink()
            self.pop(event=ev)
        elif ev in ("last", "rain"):
            self.pop(event=ev)
        elif ev == "start":
            self.ring.fade = 0.0  # 实心 pip 重新沿环展开
            if not self.isVisible():
                self.pop(START_POP_MS, event=ev)
        elif ev == "clear":
            self._celebrate = True
            self.pop(event=ev)
        self._wake()
        self.update()
        for fn in list(self.on_poll):
            fn()

    def _step(self):
        self.timer.poll()
        show = self._should_show()
        if show and not self.isVisible():
            self.show()
        self.vis = min(1.0, self.vis + VIS_IN) if show else max(0.0, self.vis - VIS_OUT)
        self._step_ladder()
        idle = self.timer.phase == IDLE
        preview = self._now_ms() < self._preview_until
        raining = self.timer.phase == RAIN
        self.ring.update(
            show and self.ladder is None and not self._celebrate and not raining and (not idle or preview)
        )
        self._rad = lerp_and_tick(
            self._rad, lerp(RAD_START, HUD_RADIUS, inv_lerp(0.0, 0.15, self.vis) ** 1.3), 0.12, 0.1
        )
        self._step_tick()
        lit = IDLE_LIT if idle and not self._celebrate else 1.0
        self._lit += (lit - self._lit) * LIT_EASE
        self._update_tip()
        look = self._look()
        if look is None or look != self._drawn_look:
            self._drawn_look = look
            self.update()
        if self.vis <= 0.0 and not show and self.ladder is None:
            self.hide()
            self._tip.hide()
            self._anim.stop()

    def _look(self):
        """决定画面的量，没变就不重画；业力梯在场时返回 None，表示每帧都画。"""
        if self.ladder is not None:
            return None
        pips = tuple(round(v, 2) for circle in self._pip_circles() for v in circle)
        return (
            self.devicePixelRatioF(),
            self.vis,
            round(self._rad, 2),
            round(self._lit, 3),
            self._shown_karma,
            pips,
        )

    def _step_tick(self):
        """晴天 pip 显示时每半个周期脉动一下；弹出或有人在看（悬停、快捷键读数、卡片）时还响一声。"""
        if self.timer.phase != DRY or self.ring.fade <= 0.0:
            return
        self._ticks += 1
        if self._ticks % (TICK_TOCK // 2):
            return
        self.ring.tick_pulse = 1.0
        now = self._now_ms()
        if now < self._pop_until or now < self._tip_until or self._hover or self._menu_open:
            self._sounds.play("tick" if self._ticks % TICK_TOCK == 0 else "tock")

    def _step_ladder(self):
        if not self._celebrate or self.vis <= 0.0:
            return
        if self.ladder is None:
            if self.ring.fade > 0.0:
                return  # 等 pip 收完
            self.ladder = KarmaLadder(self._shown_karma, KARMA_TOP, sound=self._sounds.play)
            self.ladder_alpha = 0.0
            self._ladder_ticks = 0
        lad = self.ladder
        lad.update()
        self._sounds.set_motion(lad.moving)
        self._ladder_ticks += 1
        if not lad.started:
            self.ladder_alpha = min(1.0, self.ladder_alpha + LADDER_FADE)
            if self.ladder_alpha >= 1.0 and self._ladder_ticks > LADDER_LEAD:
                lad.go_to(self._shown_karma + 1)
                self._ladder_ticks = 0
            return
        if lad.done and self._ladder_ticks > LADDER_HOLD:
            self.ladder_alpha = max(0.0, self.ladder_alpha - LADDER_FADE)
            if self.ladder_alpha <= 0.0:
                self._sounds.set_motion(0.0)
                self._shown_karma = min(self._shown_karma + 1, KARMA_TOP)
                self.ladder = None
                self._celebrate = self._shown_karma < self.timer.karma
        elif not lad.done:
            self._ladder_ticks = 0

    # 悬停读数

    def _update_tip(self):
        want = (
            (self._hover and self._tip_at is not None and self._now_ms() >= self._tip_at)
            or (self._tip.isVisible() and self._hover)
            or self._now_ms() < self._tip_until
        )
        if not want or self._menu_open or self.vis < 0.5:
            if self._tip.isVisible():
                self._tip.hide()
            return
        text = status_line(self.timer)
        if text != self._tip.text():
            self._tip.setText(text)
            self._tip.adjustSize()
        g = self.frameGeometry()
        self._tip.move(g.center().x() - self._tip.width() // 2, int(g.center().y() + BACKDROP_R + 4))
        if not self._tip.isVisible():
            self._tip.show()

    def enterEvent(self, ev):
        self._hover = True
        self._tip_at = self._now_ms() + TIP_DELAY_MS
        self._wake()
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        self._hover = False
        self._hover_until = self._now_ms() + HOVER_HOLD_MS
        self._tip_at = None
        super().leaveEvent(ev)

    # 鼠标

    def wheelEvent(self, ev):
        if self.timer.phase != IDLE:
            return
        self._wheel_acc += ev.angleDelta().y()
        step = int(self._wheel_acc / 120)
        if not step:
            return
        self._wheel_acc -= step * 120
        self.timer.dry_s = max(WHEEL_STEP, min(min(WHEEL_MAX, DRY_MAX), self.timer.dry_s + step * WHEEL_STEP))
        self._tip_at = self._now_ms()  # 调的时候立刻显示读数
        self._preview_until = self._now_ms() + PREVIEW_MS
        self._wake()
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press = (ev.globalPosition().toPoint(), self.pos())
            self._dragging = False
        elif ev.button() == Qt.MouseButton.RightButton:
            self.open_card()

    def mouseMoveEvent(self, ev):
        if self._press is None:
            return
        start, origin = self._press
        delta = ev.globalPosition().toPoint() - start
        if not self._dragging and delta.manhattanLength() > DRAG_THRESH:
            self._dragging = True
            self._tip.hide()
        if self._dragging:
            self.move(origin + delta)

    def mouseReleaseEvent(self, ev):
        if ev.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        if self._dragging:
            self._cfg["dial_pos"] = [self.x(), self.y()]
        elif self.timer.phase == IDLE and not self._celebrate:
            self.timer.start()
        self._press = None
        self._dragging = False

    # 绘制

    def paintEvent(self, _ev):
        if self.vis <= 0.0:
            return
        dpr = self.devicePixelRatioF()
        img = QImage(int(SIZE * dpr), int(SIZE * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        c = QPointF(SIZE / 2.0, SIZE / 2.0)
        self._paint_backdrop(p, c)
        la = self.ladder_alpha if self.ladder is not None else 0.0
        if la < 1.0:
            p.setOpacity((1.0 - la) * self._lit)
            self._paint_symbol(p, c, self._shown_karma, 1.0, 0.0, WHITE, self._rad / HUD_RADIUS)
            p.setOpacity(1.0)
            self._paint_pips(p, c)
        if la > 0.0:
            p.setOpacity(la)
            self._paint_ladder(p, c, self.ladder)
            p.setOpacity(1.0)
        self._mask_edge(p, c)
        p.end()
        w = QPainter(self)
        w.setOpacity(self.vis)
        w.drawImage(0, 0, img)
        w.end()

    def _paint_backdrop(self, p, c):
        g = QRadialGradient(c, BACKDROP_R)
        g.setColorAt(0.0, QColor(0, 0, 0, 165))
        g.setColorAt(0.65, QColor(0, 0, 0, 120))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, BACKDROP_R, BACKDROP_R)

    def _mask_edge(self, p, c):
        g = QRadialGradient(c, EDGE_OUT)
        g.setColorAt(0.0, QColor(0, 0, 0, 255))
        g.setColorAt(EDGE_IN / EDGE_OUT, QColor(0, 0, 0, 255))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        p.fillRect(QRectF(0, 0, SIZE, SIZE), g)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

    def _sprite(self, frame, color: QColor):
        if self._atlas is None:
            return None
        key = "ui" if self._atlas.get("ui").has(frame) else "uimsc"
        if not self._atlas.get(key).has(frame):
            return None
        q = QColor(color)
        q.setRgb(*(round(v * TINT_STEPS / 255) * 255 // TINT_STEPS for v in (q.red(), q.green(), q.blue())))
        return self._atlas.sprite(key, frame, None if q == WHITE else q)

    def _paint_symbol(self, p, c, k, sy, dy, color, scale=1.0):
        for frame in (karma_frame(k), "karmaRing"):
            pm = self._sprite(frame, color)
            if pm is None or pm.isNull():
                continue
            w, h = pm.width() / pm.devicePixelRatio(), pm.height() / pm.devicePixelRatio()
            p.save()
            p.translate(c.x(), c.y() - dy)
            p.scale(scale, sy * scale)
            p.drawPixmap(QRectF(-w / 2.0, -h / 2.0, w, h), pm, QRectF(pm.rect()))
            p.restore()

    def _pip_circles(self):
        t = self.timer
        if t.phase == RAIN:
            return []
        if t.phase == DRY:
            return self.ring.circles(t.pips(), 1.0 - t.progress(), DRAIN, self._rad)
        return self.ring.circles(t.pips(), 1.0, HOLLOW, self._rad)

    def _paint_pips(self, p, c):
        for x, y, rad, thick in self._pip_circles():
            pos = QPointF(c.x() + x * PIP_SCALE, c.y() - y * PIP_SCALE)
            r = rad * PIP_SCALE
            if thick < 0.0 or thick * PIP_SCALE >= r:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(WHITE)
                p.drawEllipse(pos, r, r)
            else:
                w = thick * PIP_SCALE
                p.setPen(QPen(WHITE, w))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(pos, r - w / 2.0, r - w / 2.0)

    def _paint_ladder(self, p, c, lad):
        """符号和分隔线只在圈里看得见；光晕和波纹是升档时的闪光，不裁。"""
        for s in lad.symbols:
            g = lad.glow(s.k)
            lay = lad.layout(s.k)
            if g is None or lay is None:
                continue
            y = lay[0]
            for rad, alpha in g:
                grad = QRadialGradient(QPointF(c.x(), c.y() - y), rad)
                grad.setColorAt(0.0, QColor(255, 255, 255, int(255 * min(1.0, alpha) * 0.5)))
                grad.setColorAt(0.4, QColor(255, 255, 255, int(255 * min(1.0, alpha) * 0.18)))
                grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(grad)
                p.drawEllipse(QPointF(c.x(), c.y() - y), rad, rad)
        p.save()
        clip = QPainterPath()
        clip.addEllipse(c, LADDER_WINDOW, LADDER_WINDOW)
        p.setClipPath(clip)
        for s in lad.symbols:
            ly = lad.line_y(s.k)
            if ly is None or s.line_width <= 0.0:
                continue
            half = 40.0 * max(0.0, s.line_width) ** 0.6
            lrp = max(0.0, min(1.0, abs(s.k + 0.5 - lad.scroll) / 0.75))
            col = mix(MEDIUM_GREY, DARK_GREY, lrp)
            col.setAlphaF(max(0.0, min(1.0, s.line_width**2)))
            p.setPen(QPen(col, 1.0))
            p.drawLine(QPointF(c.x() - half, c.y() - ly), QPointF(c.x() + half, c.y() - ly))
        for s in lad.symbols:
            lay = lad.layout(s.k)
            if lay is None:
                continue
            y, sy = lay
            p.setOpacity(p.opacity() * max(0.0, min(1.0, s.energy)))
            self._paint_symbol(p, c, s.k, sy, y, mix(WHITE, DARK_GREY, lad.selected(s.k)))
            p.setOpacity(self.ladder_alpha)
        p.restore()
        rad = lad.circle_rad
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(mix(MEDIUM_GREY, WHITE, lad.selector_white), lad.circle_thick))
        p.drawEllipse(c, rad, rad)
        for fc in lad.fade_circles:
            if fc.thickness <= 0.0:
                continue
            col = QColor(WHITE)
            col.setAlphaF(min(1.0, fc.alpha * FADE_CIRCLE_GAIN))
            p.setPen(QPen(col, fc.thickness))
            p.drawEllipse(c, fc.rad, fc.rad)

    def showEvent(self, ev):
        self._wake()
        super().showEvent(ev)

    def hideEvent(self, ev):
        self._tip.hide()
        super().hideEvent(ev)

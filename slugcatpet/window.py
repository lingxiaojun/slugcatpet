"""全屏透明置顶桌宠窗，固定步长物理+插值渲染。"""

from __future__ import annotations
import math
import os
import random
from PySide6.QtWidgets import QWidget
from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QCoreApplication, QEvent, QRect, QPoint, QPointF
from PySide6.QtGui import QPainter, QColor, QGuiApplication, QCursor, QTransform

from . import tuning
from .appearance import Look
from .characters import FALLBACK_VARIANT, REGISTRY
from .rendering.atlas import AtlasSet
from .rendering.display_list import FrameRecorder, replay
from .rendering.layout import Layout
from .rendering.pacer import FramePacer
from .platform import vsync
from .rendering.primitives import blit
from .petunit import PetUnit
from .brain.social import Social
from .core import chunkphys
from .core.units import TICK_RATE, clampf, lerp
from .core.water import WaterSurface
from .world.effects import EffectsMixin
from .world.rain import Rain, I_MAX as RAIN_I_MAX, push_force
from .items.base import alive_creature
from .items.host import ItemHost

MAX_PETS = 20
DEFAULT_MAX_PETS = 6


GRAV_EASE = 0.08

GROUND_INSET = 16.0

SHAKE_DECAY = 0.8
SHAKE_MAX = 6.0
SHAKE_EPS = 0.05


def _clamp_pets(n) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_PETS
    return max(1, min(n, MAX_PETS))


def _below_taskbar(area: QRect, geo: QRect) -> int:
    return max(0, (geo.y() + geo.height()) - (area.y() + area.height()))


def compute_geometry(area: QRect, geo: QRect, canvas_scale: int) -> dict:
    s = canvas_scale or 1
    inset_dev = _below_taskbar(area, geo) or int(round(GROUND_INSET * s))
    return {
        "WL": area.width() / s,
        "HL": area.height() / s,
        "ground_inset": inset_dev,
        "win_w": area.width(),
        "win_h": area.height() + inset_dev,
    }


FLOOR_EASE = 4.0  # 逻辑 px/tick


def _clamp_to_bounds(o, WL, HL, min_rad=0.0):
    r = max(getattr(o, "rad", 0.0), min_rad)
    if o.x < r:
        o.x = r
    elif o.x > WL - r:
        o.x = WL - r
    if o.y + r > HL:
        o.y = HL - r
        if o.vy > 0:
            o.vy = 0.0


class PetWindow(EffectsMixin, ItemHost, QWidget):
    def __init__(
        self, layout: Layout | None = None, debug: bool | None = None, params=None, catless: bool = False
    ):
        """catless：不放猫，也不带天气（雪、雨、水、失重）；给没有猫的 app 用。"""
        super().__init__()
        self._catless = catless
        self._params = params or {}
        self._seed = self._params.get("seed")
        self.atlas = AtlasSet()
        self.layout_data = layout or Layout.load()
        if debug is None:
            debug = os.environ.get("SLUGCATPET_DEBUG") not in (None, "", "0")
        self.debug = debug

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not self.debug)
        if self.debug:
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        s = self.layout_data.canvas_scale
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        geo = screen.geometry()
        geom = compute_geometry(area, geo, s)
        self._area = area  # 不含下延带
        self._geo = geo
        self._scale = s
        self._floor_sink = False
        self._floor_target = geom["HL"]
        self._WL = geom["WL"]
        self._HL = geom["HL"]  # 地板线=工作区底边，下延后不变
        self._ground_inset = geom["ground_inset"] / s if s else 0.0
        self.resize(geom["win_w"], geom["win_h"])
        self.move(area.x(), area.y())

        self.world_tick = 0  # 物理 tick 计数
        self._static_last = (None, QRect())  # 上一帧静态内容的 (签名, 范围)
        from .planning.budget import PlanBudget

        self._plan_budget = PlanBudget()  # 杆网的后台预计算
        self.sparks = []  # [x,y,vx,vy,life,maxlife,white]
        self.shockwaves = []  # [x,y,r,maxr,life,maxlife,flash]
        self.fx = []
        self.bubbles = []
        self._bubble_rng = random.Random(0xB0BB1E)
        self.cursor_hijack = None
        self._hud = None
        self._pets_changed_cb = None
        self._open_settings_cb = None
        self._refresh_top_cb = None
        self._set_lang_cb = None
        self._open_music_cb = None
        self._open_rain_cb = None
        self.env_listeners = []  # fn(kind, on)：kind 为 "blizzard" / "zerog" / "water" / "rain"
        self._hotkey_filter = None
        self._hotkeys_cb = None  # 全局热键改键后由 main 重挂
        self._control_hud = None  # 非 None 即受控中
        self._freeze = set()  # 冻结原因："env" 全屏收起、"user" 定格
        self._freeze_cb = None  # fn(on)：定格开关后由 main 处理热键、托盘、界面

        self.world_version = 0  # 放/清道具、环境变化时 +1
        self.geometry_version = 0  # 路径前提变更（杆/灯/工作区）时 +1

        self._init_items()

        self.blizzard_on = not tuning.COLD_BLIZZARD_DEFAULT_OFF
        self.blizzard_timer = 0
        self.cold_cycle_prog = 0.0
        from .world.snow import Snowfall

        self.snow_on = tuning.SNOW_ENABLED and not catless
        self._snow = Snowfall(tuning.SNOW_MAX_FLAKES, tuning.SNOW_VIGNETTE_MAX, seed=0x5)

        self.room_gravity = 1.0
        self.gravity_target = 1.0
        self.zerog_on = False

        self.water_on = False
        self.water_y = None  # 越小越高；None=无水
        self.water_target = None
        self.water_surface = None

        self.rain_on = False
        self.cycle_rain = None  # 雨循环驱动的雨：None / "drizzle" / "storm"
        self._cycle_rain_stopped_cb = None  # 手动关掉雨循环的大雨时回调
        self.cfg_rain_amount = clampf(float(self._params.get("rain_amount", tuning.RAIN_AMOUNT)), 0.05, 1.0)
        self.rain = Rain(seed=self._seed)
        self.cfg_rain_window_block = self.rain.window_block = bool(
            self._params.get("rain_window_block", False)
        )
        self._shelter_timer = QTimer(self)
        self._shelter_timer.setInterval(tuning.RAIN_SHELTER_POLL_MS)
        self._shelter_timer.timeout.connect(self._poll_shelters)
        self._env_rain = QTimer(self)  # 别的程序全屏、世界冻结时，雨强度仍按时间推进
        self._env_rain.setInterval(1000)
        self._env_rain.timeout.connect(self._advance_env_rain)
        self._ambient_shown = QRect()  # 屏上现有雪、雨的范围
        self._ambient_ns = 0
        self._ambient_tick = -1

        self._shake = [0.0, 0.0]

        self._shutting_down = False
        self._painted = None  # 最近一次记录的帧范围（逻辑坐标）
        self._scene_painted = None  # 其中雪、雨以外的部分
        self._shown = None  # 屏上现有内容的范围；None=未知，整窗刷
        self._ops = []
        self._ops_split = 0
        self._ops_key = None
        self._frame_no = 0
        self._fx_active = False
        self.follow_cursor = True
        self.cfg_cursor_hijack = bool(self._params.get("cursor_hijack", True))
        self.cfg_cursor_shape = bool(self._params.get("cursor_shape", True))
        self.cfg_freeze_hide_ui = bool(self._params.get("freeze_hide_ui", True))
        self.cfg_friendly_fire = bool(self._params.get("friendly_fire", False))
        self.cfg_keep_items = bool(self._params.get("keep_items", False))
        self.cfg_fruit_regrow = bool(self._params.get("fruit_regrow", False))
        self.cfg_fps = self._params.get("fps", "auto")  # "auto"=跟显示器刷新率
        self.cfg_power_save = bool(self._params.get("power_save", True))
        self.cfg_water_depth = clampf(
            float(self._params.get("water_depth", tuning.WATER_FULL_DEPTH)), 0.05, 0.95
        )  # 占窗口高的比例
        self.cfg_max_pets = _clamp_pets(self._params.get("max_pets", DEFAULT_MAX_PETS))
        mode = self._params.get("fullscreen_mode", "hide")
        self.cfg_fullscreen_mode = mode if mode in ("hide", "sink") else "hide"
        self._pacer = FramePacer(self.display_hz())
        self._paint_ns = 0  # 上次 paintEvent 耗时，并入下一帧统计
        self._calc_intervals()
        self.pets = []
        self.social = Social(self)
        self._hidden_pets = []
        self._build_pets()

        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_ns = self._clock.nsecsElapsed()
        self._last_vsync = False  # 上一拍来自 vblank
        self._active_until = 0
        self._t = 0.0
        self._phys_acc = 0.0
        self._ts = 1.0  # 0..1 插值因子
        self._hwnd = 0
        self._passthrough = None  # None 强制首次同步

        self._mode = None  # "vsync" / "fast" / "slow"
        self._vsync = None
        self._screen = None
        self.anim = QTimer(self)
        self.anim.setTimerType(Qt.TimerType.PreciseTimer)
        self.anim.setInterval(self._iv_fast)
        self.anim.timeout.connect(self._on_timer)
        self.anim.start()
        QGuiApplication.instance().primaryScreenChanged.connect(self._watch_screen)
        self._watch_screen()
        if self.cfg_keep_items:
            from .items import scene

            scene.restore(self, self._params.get("scene_last"))

    def _build_pets(self):
        if self._catless:
            return
        saved = self._params.get("pets")
        saved = list(saved) if isinstance(saved, list) and saved else None
        n = len(saved) if saved else 1
        env = os.environ.get("SLUGCATPET_PETS")
        if env:
            try:
                n = int(env)
            except ValueError:
                pass
        n = max(1, min(n, MAX_PETS))
        for i in range(n):
            state = saved[i] if saved and i < len(saved) else {}
            init_state = {
                "energy": state.get("energy", 1.0),
                "temper": state.get("temper", 0.0),
                "food": state.get("food", tuning.FOOD_INIT),
                "food_q": state.get("food_q", 0),
                "karma": state.get("karma", tuning.KARMA_INIT),
                "cold": state.get("cold", 0.0),
                "sleepiness": state.get("sleepiness", tuning.SLEEPY_INIT),
            }
            pet_id = state.get("id") or f"pet-{i}"
            variant = state.get("variant", "saint")
            if variant not in REGISTRY:
                variant = FALLBACK_VARIANT
            spawn_x = self._WL * (i + 1) / (n + 1)
            pet = PetUnit(
                self,
                i,
                pet_id,
                variant,
                init_state,
                spawn_x=spawn_x,
                look=Look.from_state(state),
                name=state.get("name") or "",
                personality=state.get("personality"),
            )
            if state.get("dead") and pet.behavior is not None:
                pet.behavior.enter_dead()
            pet.hidden = bool(state.get("hidden"))
            (self._hidden_pets if pet.hidden else self.pets).append(pet)
        if not self.pets and self._hidden_pets:
            pet = self._hidden_pets.pop(0)
            pet.hidden = False
            self.pets.append(pet)
        self.social.bonds.load(self._params.get("bonds"), self.all_pets())

    @property
    def body(self):
        return self.pets[0].body if self.pets else None

    @property
    def gfx(self):
        return self.pets[0].gfx if self.pets else None

    @property
    def tail(self):
        return self.pets[0].tail if self.pets else None

    @property
    def behavior(self):
        return self.pets[0].behavior if self.pets else None

    def to_logical(self, dx, dy):
        return dx / self._scale, dy / self._scale

    def cursor_logical(self):
        g = self.mapFromGlobal(QCursor.pos())
        return self.to_logical(g.x(), g.y())

    def _update_passthrough(self):
        if self.debug:
            return
        if self._place_mode:
            return  # 放置模式接管点击，勿翻转
        from .control.mouse import is_over

        cur = self.cursor_logical()
        over_body = any(
            ((pet.behavior is None) or not pet.behavior.blocks_interaction())
            and is_over(pet.body, pet.gfx, cur, pad=6.0)
            for pet in self.pets
        )
        if self._freeze:
            want = not over_body  # 定格时只留右键菜单
        else:
            active = any(pet.behavior is not None and pet.behavior.grab.active for pet in self.pets)
            want = not (active or over_body or self._drag is not None or self._grab_target(cur) is not None)
        if want != self._passthrough:
            self._passthrough = want
            if not self._hwnd:
                self._hwnd = int(self.winId())
            from .control.mouse import set_passthrough

            set_passthrough(self._hwnd, want)

    _PHYS_DT = 1.0 / 40.0
    _MAX_DT = 0.1  # 一帧最多补 4 个 tick，防时间螺旋
    _INT_SLOW = 66  # ms，省电时没动静的帧间隔
    _INT_WATCHDOG = 250  # ms，vblank 停了（熄屏、驱动重置）时的兜底间隔
    _FPS_FALLBACK = 60.0
    _ACTIVE_HOLD_MS = 300  # 防快慢档来回切
    _MOTION_STILL = 1.2

    def _scene_moving(self):
        if self.room_gravity < 1.0:
            return True
        if self.water_surface is not None:
            if self.water_y != self.water_target or self.water_surface.energy() > tuning.WATER_STILL_EPS:
                return True
        for pet in self.pets:
            b = pet.body
            if (
                abs(b.chunk0.vx) + abs(b.chunk0.vy) + abs(b.chunk1.vx) + abs(b.chunk1.vy)
            ) > self._MOTION_STILL:
                return True
            # 啃食、换手等只动手的动作也要保持高帧率
            if any(abs(h.vx) + abs(h.vy) > self._MOTION_STILL for h in pet.gfx.hands):
                return True
        return any(
            abs(o.vx) + abs(o.vy) > self._MOTION_STILL or getattr(o, "animating", False)
            for o in self._items()
        )

    def _on_timer(self):
        if self._mode == "vsync":
            if self._clock.nsecsElapsed() - self._last_ns < self._INT_WATCHDOG * 500_000:
                return
        self._tick()

    def _on_vsync(self, vs_dt):
        self._tick(vs_dt)

    def _tick(self, vs_dt=None):
        """推进一帧；vs_dt 是 vblank 节拍给的间隔，连续两拍都来自 vblank 时用它，否则用实测时长。"""
        if self._shutting_down:
            return
        now_ns = self._clock.nsecsElapsed()
        dt = (now_ns - self._last_ns) / 1e9
        self._last_ns = now_ns
        if vs_dt and self._last_vsync:
            dt = vs_dt
        self._last_vsync = vs_dt is not None
        if dt <= 0.0:
            return
        if not self.isVisible():
            return
        if self._freeze:
            self._update_passthrough()
            return
        t0 = self._clock.nsecsElapsed()
        self._update_passthrough()
        self._advance(min(dt, self._MAX_DT))
        region = self._update_region()
        self._pace(dt, t0)
        dragging = self._drag is not None
        grabbing = any(pet.behavior is not None and pet.behavior.grab.active for pet in self.pets)
        active = self._fx_active or dragging or grabbing or self.isActiveWindow() or self._scene_moving()
        now_ms = now_ns // 1_000_000
        if active:
            self._active_until = now_ms + self._ACTIVE_HOLD_MS
        elif now_ms < self._active_until:
            active = True
        self._set_mode(active or not self.cfg_power_save, self._ambient_kind())
        if region is None:
            self.update()
        else:
            self.update(region)

    def _set_mode(self, fast, ambient=None):
        """快档优先跟 vblank，不可用时用精确定时器；慢档用粗定时器，只有雪或雨在动时按它的帧率。

        全屏收起时停帧；定格时只留粗定时器更新点击穿透。"""
        if "env" in self._freeze:
            self.anim.stop()
            if self._vsync is not None:
                self._vsync.pause()
            self._mode = None
            return
        if self._freeze:
            mode = "frozen"
        elif fast and self._ensure_vsync():
            mode = "vsync"
        elif fast:
            mode = "fast"
        else:
            mode = ambient or "slow"
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "vsync":
            self._vsync.resume()
        elif self._vsync is not None:
            self._vsync.pause()
        iv, kind = {
            "vsync": (self._INT_WATCHDOG, Qt.TimerType.CoarseTimer),
            "fast": (self._iv_fast, Qt.TimerType.PreciseTimer),  # Precise 保平滑
            "slow": (self._iv_slow, Qt.TimerType.CoarseTimer),  # Coarse 省功耗
            "frozen": (self._INT_SLOW, Qt.TimerType.CoarseTimer),
            "snow": (max(self._iv_fast, round(1000 * self._PHYS_DT)), Qt.TimerType.PreciseTimer),
            "rain": (max(self._iv_fast, round(1000 / tuning.RAIN_FPS)), Qt.TimerType.PreciseTimer),
        }[mode]
        self.anim.stop()
        self.anim.setTimerType(kind)
        self.anim.setInterval(iv)
        self.anim.start()

    def _ensure_vsync(self):
        if self._vsync is None:
            if not self._vsync_wanted():
                return False
            self._vsync = vsync.VsyncDriver(self, self._screen_name(), self.display_hz())
            self._vsync.set_divider(self._divider)
            self._vsync.tick.connect(self._on_vsync)
            self._vsync.failed.connect(self._on_vsync_failed)
        return self._vsync.ok

    def _vsync_wanted(self):
        if not vsync.supported() or os.environ.get("SLUGCATPET_VSYNC", "1") == "0":
            return False
        return QGuiApplication.platformName() == "windows"

    def _on_vsync_failed(self):
        if self._mode == "vsync":
            self._mode = None
            self._set_mode(True)

    @staticmethod
    def _screen_name():
        scr = QGuiApplication.primaryScreen()
        return scr.name() if scr is not None else ""

    def _pace(self, dt, t0):
        """自动帧率下把快档帧的实测数据喂给 pacer，换档时重算间隔。"""
        busy = (self._clock.nsecsElapsed() - t0 + self._paint_ns) / 1e9
        self._paint_ns = 0
        if self.cfg_fps != "auto" or dt > self._MAX_DT or self._mode not in ("vsync", "fast"):
            return
        if self._pacer.frame(
            dt, busy, self._divider / self._pacer.hz if self._mode == "vsync" else self._iv_fast / 1000.0
        ):
            self._restart_timer()
            sp = getattr(self, "_settings_panel", None)
            if sp is not None and hasattr(sp, "refresh_fps"):
                sp.refresh_fps()

    def _update_region(self):
        """本帧范围 ∪ 屏上现有范围（None=整窗）。"""
        self._fx_active = self._fx_now()
        self._record()
        if self._shown is None:
            return None
        return self._painted.united(self._shown)

    def _fx_now(self):
        """只用于定时器保持高帧率，不影响重绘范围。"""
        return bool(
            any(pet.behavior is not None and pet.behavior.exclusive_fx() is not None for pet in self.pets)
            or self.sparks
            or self.shockwaves
            or self.fx
            or self.bubbles
            or self.cursor_hijack is not None
            or self._place_mode
            or self._any_kill_dialog()
            or self._shake[0] != 0.0
            or self._shake[1] != 0.0
        )

    def _advance(self, dt):
        self._t += dt
        self._phys_acc += dt
        phys_dt = self._PHYS_DT
        while self._phys_acc >= phys_dt:
            self._phys_acc -= phys_dt
            self._do_tick()
        self._ts = min(self._phys_acc / phys_dt, 1.0)

    def freeze(self, reason):
        self._freeze.add(reason)
        self._set_mode(False)
        if reason == "env" and not self._catless:
            self._env_rain.start()

    def _advance_env_rain(self):
        if "user" not in self._freeze:
            self.rain.advance(TICK_RATE)

    def unfreeze(self, reason):
        self._freeze.discard(reason)
        if reason == "env":
            self._env_rain.stop()
        if self._shutting_down:
            return
        if reason == "env":
            self._pacer.reset(self.display_hz())  # 全屏程序退了，它造成的降档不算数
            self._calc_intervals()
            if self._shelter_timer.isActive():  # 先取好遮挡再出画面
                self._poll_shelters()
        self._last_ns = self._clock.nsecsElapsed()
        self._last_vsync = False
        self._mode = None
        self._set_mode(True)

    @property
    def frozen(self):
        """用户定格中。"""
        return "user" in self._freeze

    @property
    def env_hidden(self):
        """别的程序全屏，桌宠收起中。"""
        return "env" in self._freeze

    def set_frozen(self, on):
        """定格：世界停在这一帧；进入前解除光标劫持、退出放置模式，免得光标被锁住或点击被拦。"""
        on = bool(on)
        if on == self.frozen:
            return
        if on:
            self._exit_place_mode()
            self.stop_cursor_hijack()
            from .platform import cursorfx

            cursorfx.abort_all()
            self.freeze("user")
        else:
            self.unfreeze("user")
        if self._freeze_cb is not None:
            self._freeze_cb(on)

    def step_frozen(self, n):
        """定格时前进 n 个物理 tick，画出这一 tick 的位置。"""
        if self._freeze != {"user"}:
            return
        for _ in range(n):
            self._do_tick()
        self._phys_acc = 0.0
        self._ts = 1.0
        self._ambient_ns = 0  # 雨也跟着换一帧
        self._update_region()
        self._update_passthrough()
        self.update()

    def set_freeze_hide_ui(self, on):
        self.cfg_freeze_hide_ui = self._params["freeze_hide_ui"] = bool(on)

    def shutdown(self):
        """退出前停帧并丢弃排队重绘，免得句柄销毁后还刷新。"""
        self._shutting_down = True
        self.anim.stop()
        if self._vsync is not None:
            self._vsync.stop()
        QCoreApplication.removePostedEvents(self, QEvent.Type.Paint)
        self.setUpdatesEnabled(False)
        self.hide()

    @classmethod
    def display_hz(cls):
        scr = QGuiApplication.primaryScreen()
        hz = scr.refreshRate() if scr is not None else 0.0
        return hz if hz > 1.0 else cls._FPS_FALLBACK

    def auto_fps(self):
        """自动帧率当前档：刷新率或其整数分频。"""
        return self._pacer.rate

    def _calc_intervals(self):
        """活跃 / 发呆两档帧间隔（ms）与 vblank 分频；固定帧率取最接近的整数分频。"""
        hz = self.display_hz()
        if hz != self._pacer.hz:
            self._pacer.reset(hz)
        rate = self.cfg_fps
        if rate == "auto":
            rate = self._pacer.rate
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 1.0:
            rate = self._FPS_FALLBACK
        self._divider = max(1, round(hz / rate))
        self._iv_fast = max(1, round(1000.0 / min(rate, 1000.0)))
        self._iv_slow = max(self._INT_SLOW, self._iv_fast) if self.cfg_power_save else self._iv_fast
        vs = getattr(self, "_vsync", None)
        if vs is not None:
            vs.set_divider(self._divider)
            vs.retarget(self._screen_name(), hz)

    def _restart_timer(self):
        self._calc_intervals()
        if self._mode is not None:
            fast = self._mode in ("vsync", "fast")
            self._mode = None
            self._set_mode(fast, self._ambient_kind())

    def _watch_screen(self, *_):
        """主屏或其刷新率变了就重算帧间隔、换 vblank 来源。"""
        scr = QGuiApplication.primaryScreen()
        if scr is not self._screen:
            if self._screen is not None:
                try:
                    self._screen.refreshRateChanged.disconnect(self._watch_screen)
                except (RuntimeError, TypeError):
                    pass
            self._screen = scr
            if scr is not None:
                scr.refreshRateChanged.connect(self._watch_screen)
        self._restart_timer()

    def set_water_depth(self, depth):
        """涨水高度（0~1）"""
        self.cfg_water_depth = self._params["water_depth"] = clampf(float(depth), 0.05, 0.95)
        if self.water_on:
            self.water_target = self._HL - self._HL * self.cfg_water_depth

    def set_fps(self, mode):
        self.cfg_fps = self._params["fps"] = mode
        if mode == "auto":
            self._pacer.reset(self.display_hz())
        self._restart_timer()

    def set_power_save(self, on):
        self.cfg_power_save = self._params["power_save"] = bool(on)
        self._restart_timer()

    def set_cursor_hijack(self, on):
        self.cfg_cursor_hijack = self._params["cursor_hijack"] = bool(on)
        if not on:
            self.stop_cursor_hijack()

    def set_friendly_fire(self, on):
        self.cfg_friendly_fire = self._params["friendly_fire"] = bool(on)

    def set_keep_items(self, on):
        self.cfg_keep_items = self._params["keep_items"] = bool(on)

    def set_fruit_regrow(self, on):
        self.cfg_fruit_regrow = self._params["fruit_regrow"] = bool(on)

    def blueprints(self):
        """存着的布局蓝图 [{name, size, items}]，就地增删。"""
        v = self._params.get("blueprints")
        if not isinstance(v, list):
            v = self._params["blueprints"] = []
        return v

    def set_cursor_shape(self, on):
        self.cfg_cursor_shape = self._params["cursor_shape"] = bool(on)
        if not on:
            if self.cursor_hijack is not None:
                self.cursor_hijack.replace_cursor = False
            from .platform.cursorfx import restore_cursors

            restore_cursors()

    def apply_workspace(self, area, geo):
        if self._shutting_down:
            return
        geom = compute_geometry(area, geo, self._scale)
        self._calc_intervals()
        self.world_version += 1
        self.geometry_version += 1
        self._area = area
        self._geo = geo
        old_hl = self._HL
        self._WL = geom["WL"]
        self._HL = self._floor_target = self._calc_floor()
        self._ground_inset = geom["ground_inset"] / self._scale if self._scale else 0.0
        self.resize(geom["win_w"], geom["win_h"])
        self.move(area.x(), area.y())
        self._items_floor_shift(old_hl, self._HL)
        self._reground(geom["WL"], self._HL)
        self._retarget_water()
        self._shown = None
        self.update()

    def set_fullscreen_mode(self, mode):
        """别的程序全屏时：hide 收起，sink 地面沉到屏幕底。"""
        if mode in ("hide", "sink"):
            self.cfg_fullscreen_mode = self._params["fullscreen_mode"] = mode

    def set_floor_sink(self, on):
        self._floor_sink = bool(on)
        self._floor_target = self._calc_floor()

    def _calc_floor(self):
        below = _below_taskbar(self._area, self._geo) if self._floor_sink else 0
        return (self._area.height() + below) / self._scale

    def _ease_floor(self):
        d = self._floor_target - self._HL
        if d == 0.0:
            return
        old = self._HL
        self._HL = self._floor_target if abs(d) <= FLOOR_EASE else old + math.copysign(FLOOR_EASE, d)
        self._items_floor_shift(old, self._HL)
        self._reground(self._WL, self._HL)
        if self._HL == self._floor_target:
            self.geometry_version += 1
            self.world_version += 1
            self._retarget_water()

    def _retarget_water(self):
        if self.water_surface is None:
            return
        self.water_target = self._HL - self._HL * self.cfg_water_depth if self.water_on else self._HL

    def _reground(self, WL, HL):
        for pet in self.pets:
            b = pet.body
            dy = HL - b.H
            if dy and b.on_floor():  # 站在地上的跟着地面平移，不被拽下去
                for c in (b.chunk0, b.chunk1):
                    c.y += dy
                    c.last_y += dy
                    c.last_last_y += dy
            b.W = WL
            b.H = HL
            b._floor_h = HL
            b.set_walk_band(WL)
            b.visual_floor_y = HL
            _clamp_to_bounds(b.chunk0, WL, HL, 1.0)
            _clamp_to_bounds(b.chunk1, WL, HL, 1.0)
            pet.resize(HL)
        for obj in self._items():
            _clamp_to_bounds(obj, WL, HL)

    def _do_tick(self):
        self.world_tick += 1
        self._frame_no += 1
        # 抖动衰减，须在 impact 前
        self._shake[0] *= SHAKE_DECAY
        self._shake[1] *= SHAKE_DECAY
        if abs(self._shake[0]) + abs(self._shake[1]) < SHAKE_EPS:
            self._shake[0] = self._shake[1] = 0.0
        cur = self.cursor_logical()

        # 躲杀期间周期性抬窗到弹窗之上
        if self._any_kill_dialog():
            self._kill_raise_t = getattr(self, "_kill_raise_t", 0) + 1
            if self._kill_raise_t % 8 == 1:
                self.raise_()

        self._ease_floor()
        self._zerog_update()
        cycle_prog = self._cold_update_world()
        self._water_update()  # 须在 pet.step 前

        for pet in self.pets:
            pet.check_grip()
        self.social.tick()
        for pet in self.pets:
            pet.step(cur, cycle_prog)
        self.social.carry.sync(self.pets)
        self._plan_budget.run(self.pets)

        self._tick_items()
        self._water_splash_detect()  # 须在物体积分后

        self._collide_objects()

        self._tick_items(late=True)

        if self.snow_on:
            self._snow.step(self.cold_cycle_prog, self._WL, self._HL)

        self._rain_update()

        self._update_fx()

    def _env_changed(self, kind, on):
        for fn in self.env_listeners:
            fn(kind, on)

    def set_zerog(self, on):
        """开/关无重力；开时摘掉所有果柄。"""
        if bool(on) != self.zerog_on:
            self._env_changed("zerog", bool(on))
        self.zerog_on = bool(on)
        self.gravity_target = 0.0 if on else 1.0
        if self.zerog_on:
            self._items_zerog()

    def _zerog_update(self):
        rg = self.room_gravity + (self.gravity_target - self.room_gravity) * GRAV_EASE
        if abs(rg - self.gravity_target) < 1e-3:
            rg = self.gravity_target
        self.room_gravity = rg
        for o in self._items():
            o.room_gravity = rg

    def set_blizzard(self, on):
        if bool(on) != self.blizzard_on:
            self._env_changed("blizzard", bool(on))
        self.blizzard_on = bool(on)
        self.blizzard_timer = 0

    def set_water(self, on):
        if bool(on) != self.water_on:
            self._env_changed("water", bool(on))
        self.water_on = bool(on)
        self.water_target = (self._HL - self._HL * self.cfg_water_depth) if on else self._HL
        if on and self.water_surface is None:
            self.water_y = self._HL
            self.water_surface = WaterSurface(self._WL, self._HL, tuning.WATER_SPACING, seed=self._seed)

    def set_rain(self, on):
        """手动开关雨；关掉雨循环的大雨算提前停雨。"""
        storm = self.cycle_rain == "storm"
        self.cycle_rain = None
        self._switch_rain(on, self.cfg_rain_amount * RAIN_I_MAX)
        if not on and storm and self._cycle_rain_stopped_cb is not None:
            self._cycle_rain_stopped_cb()

    def set_cycle_rain(self, level):
        """雨循环驱动的雨：drizzle 毛毛雨，storm 下到最大、不看雨量设置，None 停；会关掉其它环境。"""
        if level == self.cycle_rain:
            return
        if level is None:
            self.cycle_rain = None
            if self.rain_on:
                self._switch_rain(False, 0.0)
        else:
            for kind, off in (
                ("blizzard", self.set_blizzard),
                ("zerog", self.set_zerog),
                ("water", self.set_water),
            ):
                if getattr(self, f"{kind}_on"):
                    off(False)
            self.cycle_rain = level
            self._switch_rain(True, tuning.RAIN_CYCLE_DRIZZLE if level == "drizzle" else RAIN_I_MAX)
        sp = getattr(self, "_settings_panel", None)
        if sp is not None and hasattr(sp, "refresh_env"):
            sp.refresh_env()

    def _switch_rain(self, on, target):
        if bool(on) != self.rain_on:
            self._env_changed("rain", bool(on))
        self.rain_on = bool(on)
        self.rain.set_target(target if on else 0.0)
        if on and not self._shelter_timer.isActive():
            self._poll_shelters()
            self._shelter_timer.start()

    def set_rain_amount(self, amount):
        """设雨量（0~1）；雨循环的雨不跟着变。"""
        self.cfg_rain_amount = self._params["rain_amount"] = clampf(float(amount), 0.05, 1.0)
        if self.rain_on and self.cycle_rain is None:
            self.rain.set_target(self.cfg_rain_amount * RAIN_I_MAX)

    def set_rain_window_block(self, on):
        self.cfg_rain_window_block = self.rain.window_block = self._params["rain_window_block"] = bool(on)
        self.update()

    def _poll_shelters(self):
        """取挡雨窗口的矩形，换算到逻辑坐标；全屏收起时不取，免得把全屏程序当成遮挡。"""
        if self.env_hidden:
            return
        from .platform import shelters

        dpr = self.devicePixelRatioF() or 1.0
        ox, oy, s = self.x(), self.y(), self._scale or 1
        hwnds = [self._hwnd] if self._hwnd else []
        self.rain.set_shelters(
            [
                ((x0 / dpr - ox) / s, (y0 / dpr - oy) / s, (x1 / dpr - ox) / s, (y1 / dpr - oy) / s)
                for x0, y0, x1, y1 in shelters.window_rects(hwnds)
            ]
        )

    def _rain_update(self):
        rain = self.rain
        if not self.rain_on and not rain.active:
            if self._shelter_timer.isActive():
                self._shelter_timer.stop()
                rain.release()
            return
        rain.resize(self._WL, self._HL)
        rain.step()
        force = push_force(rain.intensity)
        if force > 0.0:
            self._rain_push_items(force)

    def _rain_push_items(self, force):
        """露天散落的物品被雨推；活的小生物会被砸晕。"""
        rain = self.rain
        floor = self._HL - 1.0
        for o in self._items():
            if (
                o.state != "free"
                or getattr(o, "held_by_hand", None) is not None
                or not getattr(o, "mass", 0.0)
            ):
                continue
            alive = alive_creature(o)
            n = rain.kick(o, force, o.y + o.rad >= floor, 1 if alive else 0, tuning.RAIN_ITEM_KICK_MAX)
            if n:
                o.stun = max(o.stun, n)

    def spawn_bubble(self, x, y, vx, vy):
        if self.water_surface is None or len(self.bubbles) >= tuning.BUBBLE_MAX:
            return
        from .world.bubbles import Bubble

        self.bubbles.append(Bubble(x, y, vx, vy, self._bubble_rng))

    def _water_update(self):
        surf = self.water_surface
        if surf is None:
            return
        rate = self._HL * self.cfg_water_depth / tuning.WATER_EASE_TICKS
        target = self.water_target
        moving = abs(self.water_y - target) > 1e-6
        if self.water_y < target:
            self.water_y = min(self.water_y + rate, target)
        elif self.water_y > target:
            self.water_y = max(self.water_y - rate, target)
        if not self.water_on and self.water_y >= self._HL - 0.5:
            self.water_surface = None
            self.water_y = None
            self.bubbles = []
            for o in self._items():
                o.water_y = None
            for pet in self.pets:
                pet.body.water_surface = None
                pet.body.bubble_cb = None
            return
        surf.base_y = self.water_y
        if moving:
            if self.water_on:
                surf.waterfall_hit(0.0, self._WL, tuning.WATER_FLOW)
            else:
                surf.drain_affect(0.0, self._WL, tuning.WATER_FLOW)
        surf.step()
        for o in self._items():
            o.water_y = surf.level_at(o.x)
        for pet in self.pets:
            pet.body.water_surface = surf
            pet.body.bubble_cb = self.spawn_bubble

    def _water_splash_detect(self):
        surf = self.water_surface
        if surf is None or surf.splash_stop > 0:
            return
        objs = []
        for pet in self.pets:
            b = pet.body
            objs.append(b.chunk0)
            objs.append(b.chunk1)
        objs.extend(self._splash_items())
        for o in objs:
            lvl = surf.level_at(o.x)
            vy = o.vy
            if vy > 3.0 and o.last_y < lvl <= o.y:
                pass  # 向下穿越（y↓）
            elif vy < -3.0 and o.last_y > lvl >= o.y:
                pass  # 向上穿越
            else:
                continue
            impulse = lerp(vy * o.rad * lerp(o.mass, 1.0, 0.3) / 3.0, 10.0, 0.5)
            if abs(impulse) > abs(vy):
                impulse = vy
            surf.splash(o.x, impulse)
            if abs(impulse) > 5.0:
                surf.ripple_ring(o.x)
            surf.splash_stop = 10
            break

    def _shake_impact(self, chunk, direction, speed, strength, ix, iy):
        self._shake[0] = clampf(self._shake[0] + ix, -SHAKE_MAX, SHAKE_MAX)
        self._shake[1] = clampf(self._shake[1] + iy, -SHAKE_MAX, SHAKE_MAX)

    def _cold_update_world(self):
        cycle_prog = 0.0
        if self.blizzard_on:
            self.blizzard_timer += 1
            if self.blizzard_timer >= tuning.COLD_BLIZZARD_TOTAL:
                self.set_blizzard(False)
                sp = getattr(self, "_settings_panel", None)
                if sp is not None and hasattr(sp, "refresh_env"):
                    sp.refresh_env()
            else:
                ramp = tuning.COLD_BLIZZARD_RAMP
                hold_end = tuning.COLD_BLIZZARD_TOTAL - ramp
                t = self.blizzard_timer
                if t < ramp:
                    cycle_prog = t / ramp
                elif t < hold_end:
                    cycle_prog = 1.0
                else:
                    cycle_prog = 1.0 - (t - hold_end) / ramp
        self.cold_cycle_prog = cycle_prog
        return cycle_prog

    def _collide_objects(self):
        chunkphys.collide_objects([*(pet.body for pet in self.pets), *self._colliding_items()])

    def _any_kill_dialog(self):
        return any(getattr(p, "_kill_dialog", None) is not None for p in self.pets)

    def request_kill(self, pet):
        if pet.behavior is None or pet._kill_dialog is not None:
            return
        from .i18n import t
        from .ui.widgets.dialogs import ConfirmDialog
        from .ui.panels.catmenu import pet_label

        dlg = ConfirmDialog(
            t("dlg_confirm_title"),
            t("dlg_kill_text", name=pet_label(pet, list(self.pets))),
            t("dlg_kill_yes"),
            t("dlg_kill_no"),
            parent=self,
        )
        dlg.finished.connect(lambda result, p=pet: self._on_pet_kill_finished(p, result))
        pet._kill_dialog = dlg  # 供 FSM 舌点取消
        n = sum(1 for p in self.pets if p is not pet and p._kill_dialog is not None)
        dlg.place_center(offset=QPoint(48 * n, 48 * n))
        dlg.show()
        dlg.activateWindow()  # Esc 即刻可用
        self.raise_()  # 提到弹窗之上

    def _on_pet_kill_finished(self, pet, result):
        from PySide6.QtWidgets import QDialog

        box = pet._kill_dialog
        pet._kill_dialog = None
        silent = pet._kill_dismiss_silent
        pet._kill_dismiss_silent = False
        do_kill = result == QDialog.DialogCode.Accepted
        by_self = pet._kill_cancel_by_self
        pet._kill_cancel_by_self = False
        if box is not None:
            box.deleteLater()
        if pet.behavior is None:
            return
        if do_kill:
            pet.behavior.kill()
        elif not silent:  # 静默消解不扣好感
            pet.behavior.kill_threat_canceled(by_self)

    def reload_skins(self, force=True) -> bool:
        """force=False 时文件没变就跳过；返回是否重扫。"""
        from . import skins

        if force:
            skins.reload()
        elif not skins.refresh_if_changed():
            return False
        for pet in self.pets:
            pet.refresh_skin()
        return True

    def all_pets(self):
        return [*self.pets, *self._hidden_pets]

    def set_max_pets(self, n):
        """同时在场的上限；已经超出的不会被移走。"""
        self.cfg_max_pets = self._params["max_pets"] = _clamp_pets(n)

    def add_pet(self, variant="saint"):
        """新增一只猫，满员返回 None。"""
        if len(self.pets) >= self.cfg_max_pets:
            return None
        used_idx = {p.index for p in self.all_pets()}
        used_id = {p.id for p in self.all_pets()}
        k = 0
        while k in used_idx or f"pet-{k}" in used_id:
            k += 1
        init_state = {
            "energy": 1.0,
            "temper": 0.0,
            "food": tuning.FOOD_INIT,
            "karma": tuning.KARMA_INIT,
            "cold": 0.0,
        }
        margin = self.layout_data.canvas_w / 2.0
        lo, hi = margin, max(margin + 1.0, self._WL - margin)
        spawn_x = clampf(random.uniform(lo, hi), 0.0, self._WL)
        pet = PetUnit(self, k, f"pet-{k}", variant, init_state, spawn_x=spawn_x)
        self.pets.append(pet)
        self._shown = None
        self._after_pets_changed()
        return pet

    def remove_pet(self, pet):
        """成功返回 True。"""
        if pet in self._hidden_pets:
            self._hidden_pets.remove(pet)
        elif len(self.pets) <= 1 or pet not in self.pets:
            return False
        else:
            self._take_off_stage(pet)
            self.pets.remove(pet)
        self.social.bonds.drop(pet)
        self._shown = None
        self._after_pets_changed()
        return True

    def _take_off_stage(self, pet):
        if getattr(pet, "controlled", False):
            self.stop_control()
        self.social.carry.release_all(pet)
        pet.body.drop_carried()
        pet.dismiss_kill_dialog()
        if pet.behavior is not None:
            try:
                pet.behavior._break_active_controllers()
                pet.behavior.grab.force_release()
            except Exception:
                pass

    def hide_pet(self, pet):
        if pet not in self.pets or len(self.pets) <= 1:
            return False
        self._take_off_stage(pet)
        self.pets.remove(pet)
        pet.set_held(False)
        pet.hidden = True
        self._hidden_pets.append(pet)
        self._shown = None
        self._after_pets_changed()
        return True

    def show_pet(self, pet):
        """把收起的猫放回来，满员返回 False。"""
        if pet not in self._hidden_pets or len(self.pets) >= self.cfg_max_pets:
            return False
        self._hidden_pets.remove(pet)
        pet.hidden = False
        self.pets.append(pet)
        self._shown = None
        self._after_pets_changed()
        return True

    def show_all_pets(self):
        changed = False
        for pet in list(self._hidden_pets):
            if len(self.pets) >= self.cfg_max_pets:
                break
            self._hidden_pets.remove(pet)
            pet.hidden = False
            self.pets.append(pet)
            changed = True
        if changed:
            self._shown = None
            self._after_pets_changed()
        return changed

    def rename_pet(self, pet, name):
        pet.name = (name or "").strip()
        self._after_pets_changed()

    def set_pet_pup(self, pet, on):
        pet.set_pup(on)
        self._after_pets_changed()

    def _after_pets_changed(self):
        hud = self._hud
        if hud is not None and hasattr(hud, "rebuild_rows"):
            hud.rebuild_rows()
        if self._pets_changed_cb is not None:
            try:
                self._pets_changed_cb()
            except Exception:
                pass
        sp = getattr(self, "_settings_panel", None)
        if sp is not None:
            sp.pets_changed()
        self.update()

    def open_cat_menu(self, pet, global_pos):
        from .ui.panels.catmenu import build_cat_menu

        menu = build_cat_menu(pet, self.all_pets(), parent=self)
        menu.exec(global_pos)

    def open_settings(self):
        if self._open_settings_cb is not None:
            self._open_settings_cb()

    def open_music(self):
        if self._open_music_cb is not None:
            self._open_music_cb()

    def open_rain(self, anchor=None):
        """打开雨循环卡片，anchor 为它旁边的按钮。"""
        if self._open_rain_cb is not None:
            self._open_rain_cb(anchor)

    def set_lang(self, lang):
        if self._set_lang_cb is not None:
            self._set_lang_cb(lang)

    def refresh_top(self):
        if self._refresh_top_cb is not None:
            self._refresh_top_cb()

    def controlled_pet(self):
        for pet in self.pets:
            if getattr(pet, "controlled", False):
                return pet
        return None

    def start_control(self, pet):
        if pet is None or pet not in self.pets or getattr(pet, "controlled", False):
            return
        if pet._kill_dialog is not None:
            return
        beh = pet.behavior
        if beh is None or beh.is_truly_dead() or beh.is_reincarnating() or beh.blocks_interaction():
            return
        self.stop_control()
        pet.set_held(False)
        from .control.session import enter_control
        from .platform.topmost import own_by
        from .ui.panels.controlhud import ControlHud

        hud = ControlHud(self, pet)
        own_by(self, hud)
        enter_control(pet, hud.current_input)
        self._control_hud = hud
        hud.show()  # 须同步执行，勿 QTimer 推迟

    def hold_controlled(self):
        """退出手动控制，让这只猫按当前姿势定身。"""
        pet = self.controlled_pet()
        self.stop_control()
        if pet is not None:
            pet.set_held(True)

    def held_pets(self):
        return [p for p in self.pets if p.held is not None]

    def release_all_held(self):
        for p in self.pets:
            p.set_held(False)

    def stop_control(self):
        hud = self._control_hud
        self._control_hud = None
        pet = self.controlled_pet()
        if pet is not None:
            from .control.session import exit_control

            exit_control(pet)
        if hud is not None:
            hud.close()
            hud.deleteLater()

    def _draw_water(self, p):
        from PySide6.QtGui import QPolygonF, QPen

        surf = self.water_surface
        n = surf.n
        WL = self._WL
        bottom = self._HL + self._ground_inset
        base = surf.base_y
        h = surf.height
        pts = []
        for i in range(n):
            x = surf.point_x(i)
            if x > WL:
                x = WL
            pts.append((x, base + h[i]))
        poly = QPolygonF()
        for x, y in pts:
            poly.append(QPointF(x, y))
        poly.append(QPointF(WL, bottom))
        poly.append(QPointF(0.0, bottom))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(*tuning.WATER_BODY_RGBA))
        p.drawPolygon(poly)
        if self.bubbles:
            self._draw_bubbles(p)
        # 一次性画折线，防重叠叠 alpha
        pen = QPen(QColor(*tuning.WATER_SURFACE_RGBA))
        pen.setWidthF(2.0)
        p.setPen(pen)
        line = QPolygonF()
        for x, y in pts:
            line.append(QPointF(x, y))
        p.drawPolyline(line)

    def _draw_bubbles(self, p):
        ts = self._ts
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setOpacity(tuning.BUBBLE_OPACITY)
        for b in self.bubbles:
            x = b.last_x + (b.x - b.last_x) * ts
            y = b.last_y + (b.y - b.last_y) * ts
            sc = b.full_size * tuning.BUBBLE_DRAW_SCALE
            blit(p, self.atlas, "LizardBubble5", x, y, 0.0, sc, sc, tuning.BUBBLE_RGBA, ax=0.5, ay=0.5)
        p.restore()

    def _draw_scene(self, p):
        # 图层序：物品 under（果柄）/ 烟 / 身后的手持物 → 猫身 → 物品 mid（杆）/ 手 → 物品 front → 水 → 物品 top（灯）→ 特效 → 雪 / 雨
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.scale(self._scale, self._scale)
        if self._shake[0] or self._shake[1]:
            p.translate(self._shake[0], self._shake[1])

        self._draw_items_under(p)
        self._draw_fx_under(p)
        self._draw_items_behind(p)

        self._draw_click_pads(p)
        order = self.social.carry.draw_order(self.pets)
        for pet in order:
            fx = pet.behavior.exclusive_fx() if pet.behavior is not None else None
            if fx is not None:
                fx.draw_under(p, self._ts)
            pet.gfx.draw_sprites(p, pet.atlas, timeStacker=self._ts)

        self._draw_items(p, "mid")
        for pet in order:
            pet.gfx._draw_hand_grips(p, pet.atlas, self._ts)

        self._draw_items(p, "front")

        if self.water_surface is not None:
            self._draw_water(p)

        self._draw_items(p, "top")

        self._draw_fx(p)

    def _static_sig(self):
        from .items import kinds

        return (*(k.static_sig(self) for k in kinds()), self._ghosts is not None, *self._shake)

    def _draw_click_pads(self, p):
        """猫的抓点下铺一层近乎透明的底，接住点击。"""
        from .control.mouse import grab_circles

        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 1))
        for pet in self.pets:
            if pet.behavior is not None and pet.behavior.blocks_interaction():
                continue
            for _, _, x, y, r in grab_circles(pet.body, pet.gfx):
                p.drawEllipse(QPointF(x, y), r, r)
        p.restore()

    def _draw_overlay(self, p):
        self._draw_ghosts(p)
        self._draw_item_overlays(p)
        if self._place_mode:
            self._draw_place_hint(p)

    def _snow_visible(self):
        return self.snow_on and self._snow.active

    def _ambient_kind(self):
        if self._snow_visible():
            return "snow"
        return "rain" if self.rain.shows else None

    def _ambient_frame(self):
        """雪按物理 tick、雨按 RAIN_FPS 定格，返回要刷新的范围（含旧范围）；不到时刻返回空。"""
        kind = self._ambient_kind()
        if kind is None and self._ambient_shown.isEmpty():
            return QRect()
        now = self._clock.nsecsElapsed()
        if kind == "snow" and self.world_tick == self._ambient_tick:
            return QRect()
        if kind == "rain" and now - self._ambient_ns < 0.9e9 / tuning.RAIN_FPS:
            return QRect()
        self._ambient_ns, self._ambient_tick = now, self.world_tick
        self._snow.snapshot()
        self.rain.snapshot(self._ts)
        if kind == "snow":
            rect = self.rect()
        elif kind == "rain":
            rect = self.rain.paint_rect(self._scale)
        else:
            rect = QRect()
        out = rect.united(self._ambient_shown)
        self._ambient_shown = rect
        return out

    def _record(self):
        """记下调用列表与精确范围（逻辑坐标），不光栅化；雪、雨不入列表。"""
        r = FrameRecorder(self.font())
        try:
            self._draw_scene(r)
            self._ops_split = len(r.ops)
            self._draw_overlay(r)
        finally:
            r.end()
        self._ops = r.ops
        scene = r.device_rect()
        static, sig = r.static_rect(), self._static_sig()
        if sig != self._static_last[0]:
            scene = scene.united(static).united(self._static_last[1])  # 静态内容变了，新旧位置都刷
        self._static_last = (sig, static)
        self._scene_painted = scene
        self._painted = scene.united(self._ambient_frame())
        self._ops_key = (self._frame_no, self._ts)

    def paintEvent(self, _):
        if self._shutting_down:
            return
        t0 = self._clock.nsecsElapsed()
        if self._ops_key != (self._frame_no, self._ts):
            self._record()  # 非 tick 触发（render()/expose/状态直改）
        p = QPainter(self)
        try:
            ops, k = self._ops, self._ops_split
            replay(p, ops[:k])
            self._snow.draw(p, self._WL, self._HL, self._scale)
            p.save()
            p.setTransform(QTransform.fromScale(self._scale, self._scale))  # 雨定格不动，不跟着震
            self.rain.draw(p, self._scale, self._scene_painted)
            p.restore()
            replay(p, ops[k:])
        finally:
            p.end()
        self._shown = self._scene_painted  # 雪、雨的旧范围由 _ambient_frame 管
        self._paint_ns += self._clock.nsecsElapsed() - t0

    def mousePressEvent(self, e):
        if self._place_mode:
            if e.button() == Qt.MouseButton.LeftButton:
                self._place_click(*self.to_logical(e.position().x(), e.position().y()))
            elif e.button() == Qt.MouseButton.RightButton:
                self._exit_place_mode()
            return
        if self._freeze and e.button() != Qt.MouseButton.RightButton:
            return
        if e.button() == Qt.MouseButton.RightButton:
            pos = self.to_logical(e.position().x(), e.position().y())
            hit = self._secondary_target(pos)
            if hit is not None:
                hit[0].on_secondary(self, hit[1], e.globalPosition().toPoint())
                return
            from .control.mouse import hit_test, GRAB_PAD

            for pet in self.pets:
                if pet.behavior is not None and pet.behavior.blocks_interaction():
                    continue
                name, _ = hit_test(pet.body, pet.gfx, pos, pad=GRAB_PAD)
                if name is not None:
                    self.open_cat_menu(pet, e.globalPosition().toPoint())
                    return
            return
        if e.button() == Qt.MouseButton.LeftButton:
            pos = self.to_logical(e.position().x(), e.position().y())
            if self.held_item_at(pos):
                self._begin_item_drag(pos)  # 手里的东西挨着猫身，先于抓猫
                return
            grabbed = False
            for pet in self.pets:
                if getattr(pet, "controlled", False):
                    continue
                if pet.behavior is not None and pet.behavior.on_press(pos):
                    grabbed = True
                    break
            if not grabbed:
                self._begin_item_drag(pos)

    def keyPressEvent(self, e):
        if self._place_mode and e.key() == Qt.Key.Key_Escape:
            self._exit_place_mode()
            return
        super().keyPressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            for pet in self.pets:
                if pet.behavior is not None:
                    pet.behavior.on_release()
            self._end_item_drag()

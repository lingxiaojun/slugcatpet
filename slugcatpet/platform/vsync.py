"""垂直同步驱动：后台线程等显示器 vblank，按分频把帧节拍投递到主线程。"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot

_IS_WIN = sys.platform == "win32"

SCALE_S = 0.25  # 每隔这么久按真实时长校正一次 dt；标称周期在游戏切刷新率、可变刷新率时不准
MAX_ERRORS = 3  # 连续等待失败这么多次就重开适配器
RETRY_S = 0.005

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _OpenAdapter(ctypes.Structure):
        _fields_ = [
            ("hDc", wintypes.HDC),
            ("hAdapter", ctypes.c_uint32),
            ("luidLow", ctypes.c_uint32),
            ("luidHigh", ctypes.c_int32),
            ("VidPnSourceId", ctypes.c_uint32),
        ]

    class _WaitVBlank(ctypes.Structure):
        _fields_ = [
            ("hAdapter", ctypes.c_uint32),
            ("hDevice", ctypes.c_uint32),
            ("VidPnSourceId", ctypes.c_uint32),
        ]

    class _CloseAdapter(ctypes.Structure):
        _fields_ = [("hAdapter", ctypes.c_uint32)]

    _gdi = ctypes.WinDLL("gdi32")
    _gdi.CreateDCW.restype = wintypes.HDC
    _gdi.CreateDCW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p)
    _gdi.DeleteDC.argtypes = (wintypes.HDC,)
    for _fn in ("D3DKMTOpenAdapterFromHdc", "D3DKMTWaitForVerticalBlankEvent", "D3DKMTCloseAdapter"):
        getattr(_gdi, _fn).restype = ctypes.c_long
        getattr(_gdi, _fn).argtypes = (ctypes.c_void_p,)


def supported() -> bool:
    return _IS_WIN


class _Adapter:
    """一块屏的显示源句柄，只在驱动线程里用。"""

    def __init__(self, screen_name: str):
        self._wait = None
        hdc = _gdi.CreateDCW(screen_name or "DISPLAY", None, None, None)
        if not hdc:
            return
        try:
            op = _OpenAdapter()
            op.hDc = hdc
            if _gdi.D3DKMTOpenAdapterFromHdc(ctypes.byref(op)) == 0:
                self._wait = _WaitVBlank(op.hAdapter, 0, op.VidPnSourceId)
        finally:
            _gdi.DeleteDC(hdc)

    @property
    def ok(self) -> bool:
        return self._wait is not None

    def wait(self) -> bool:
        return _gdi.D3DKMTWaitForVerticalBlankEvent(ctypes.byref(self._wait)) == 0

    def close(self):
        if self._wait is not None:
            _gdi.D3DKMTCloseAdapter(ctypes.byref(_CloseAdapter(self._wait.hAdapter)))
            self._wait = None


class VsyncDriver(QObject):
    """每 divider 个标称周期发一次 tick(dt)，dt 为校正到真实时长的间隔（秒），0 表示间隔未知。

    主线程没处理完上一拍时不再投递，漏掉的 vblank 计进下一拍的 dt。
    打不开适配器或反复等待失败时发 failed，直到 retarget 换屏或换刷新率再试。
    """

    tick = Signal(float)
    failed = Signal()
    _beat = Signal(float)

    def __init__(self, parent, screen_name: str, hz: float):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._screen = screen_name
        self._period = 1.0 / hz
        self._reopen = True
        self._running = False
        self._stop = False
        self._pending = False
        self._divider = 1
        self._ok = True
        self._thread = None
        self._beat.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @property
    def ok(self) -> bool:
        return self._ok

    def retarget(self, screen_name: str, hz: float):
        """换屏或刷新率变了：重开适配器、按新刷新率重估周期。"""
        with self._lock:
            if screen_name == self._screen and abs(1.0 / hz - self._period) < 1e-6 and self._ok:
                return
            self._screen = screen_name
            self._period = 1.0 / hz
            self._reopen = True
            self._ok = True

    def set_divider(self, n: int):
        self._divider = max(1, int(n))

    def resume(self):
        if self._stop or not self._ok:
            return
        self._running = True
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="vsync", daemon=True)
            self._thread.start()
        self._wake.set()

    def pause(self):
        self._running = False

    def stop(self, timeout=0.1):
        self._stop = True
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @Slot(float)
    def _deliver(self, dt):
        self._pending = False
        if self._running:
            self.tick.emit(dt)

    def _fail(self):
        self._ok = False
        self._running = False
        try:
            self.failed.emit()
        except RuntimeError:
            self._stop = True

    def _run(self):
        adapter = None
        prev = None  # 上次醒来的时刻
        acc = 0  # 上次投递以来经过的 vblank 数
        lost = True  # 这段间隔里有数不清的 vblank
        beat_t = 0.0  # 上次投递的时刻
        scale = 1.0  # 真实时长 ÷ 标称计时
        span_real, span_nominal = 0.0, 0.0
        errors = 0
        try:
            while not self._stop:
                if not self._running:
                    self._wake.clear()
                    if not self._running and not self._stop:
                        self._wake.wait()
                    prev, lost = None, True
                    continue
                with self._lock:
                    reopen, name = self._reopen, self._screen
                    self._reopen = False
                if reopen or adapter is None:
                    if adapter is not None:
                        adapter.close()
                    adapter = _Adapter(name)
                    prev, lost = None, True
                    scale, span_real, span_nominal = 1.0, 0.0, 0.0
                    if not adapter.ok:
                        adapter = None
                        self._fail()
                        continue
                if not adapter.wait():
                    errors += 1
                    if errors % MAX_ERRORS == 0:
                        adapter.close()
                        adapter = None
                        if errors >= 2 * MAX_ERRORS:
                            errors = 0
                            self._fail()
                    time.sleep(RETRY_S)
                    continue
                errors = 0
                t = time.perf_counter()
                if prev is None:
                    prev = t
                    acc += 1
                    continue
                with self._lock:
                    period = self._period
                acc += max(1, round((t - prev) / period))
                prev = t
                if acc >= self._divider and not self._pending and self._running:
                    self._pending = True
                    self._beat.emit(0.0 if lost else acc * period * scale)
                    if not lost:
                        span_real += t - beat_t
                        span_nominal += acc * period
                        if span_real >= SCALE_S:
                            scale = span_real / span_nominal
                            span_real, span_nominal = 0.0, 0.0
                    beat_t = t
                    acc, lost = 0, False
        except RuntimeError:
            pass  # 窗口已销毁
        finally:
            if adapter is not None:
                adapter.close()

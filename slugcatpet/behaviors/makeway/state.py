"""让路状态：让别的猫过去。"""

from __future__ import annotations

from ...brain.state import LIGHT, T_ACTIVITY, State
from ...planning.climbmap import locate, map_for
from ...planning.climbmoves import PullUpMotion, SwingUnderMotion, climb_tick, release, tip_up_tick

LIE_MIN = 36  # 趴下至少趴这么久
LIE_HASTE = 2.0  # 让路时趴下比平时快这么多倍
TIMEOUT = 300  # 兜底：让这么久还没完，说明对方的过路没有收尾（设计上不该发生）
STEP_OFF_H = 34.0  # 竖杆上臀离地这么近就下到地上
CALM = 15  # 竖杆上对方这么久不再逼近才算让完


class MakeWay(State):
    name = "MakeWay"
    tier = T_ACTIVITY
    energy = LIGHT

    def __init__(self, fsm):
        super().__init__(fsm)
        self.kind = None
        self.by = None  # 要过去的那只
        self.dir = 0  # 它在通道上的去向
        self.bar = None
        self.motion = None
        self.calm = 0
        self.resume = None  # 让之前在待机里要走去的地方 (x, tol)

    def enter(self):
        fsm = self.fsm
        b = fsm.body
        o = fsm.social.order(fsm.win)
        self.kind, self.by, self.dir = ("stop", None, 0) if o is None else (o.kind, o.by, o.dir)
        self.bar = self.motion = None
        self.calm = 0
        idle = fsm.prev_state == "IdleStand" and b.walk_target_x is not None
        self.resume = (b.walk_target_x, b.walk_tol) if idle else None  # 让完接着走，走不到了待机自己再挑
        if self.kind == "lie":
            b.stop_walk()
            b.facing = self.dir
            b.set_posture(False)
            b.lie_haste = LIE_HASTE
            return
        if self.kind == "stop":
            b.stop_walk()
            self._done()
            return
        if self.kind == "off":
            if not b.walk_to(b.chunk1.x, None):
                self.kind = None
            return
        cm = map_for(fsm.win)
        here = locate(cm, fsm.win)
        if here is None or here[0] not in ("H", "S", "V") or (self.kind == "clear") != (here[0] == "V"):
            self.kind = None  # 已经不在下指令时的那条通道上
            return
        self.bar = cm.bars[here[1]]
        if self.kind == "swap":
            if here[0] == "H":
                self.motion = PullUpMotion(b, self.bar, b.facing or 1)
            else:
                self.motion = SwingUnderMotion(b, self.bar)

    def tick(self, cursor, disturbed):
        fsm = self.fsm
        o = fsm.social.order(fsm.win)
        if o is not None:
            self.by, self.dir = o.by, o.dir
        if self.by is not None:
            ob = self.by.body
            fsm.gfx.look_at = (ob.chunk0.x, ob.chunk0.y)
        if fsm.timer > TIMEOUT:
            fsm.social.traffic.fallback(fsm.win, "让路超时")
        if self.kind is None or fsm.timer > TIMEOUT:
            self._done()
        elif self.kind == "off":
            if fsm.body.walk_target_x is None:
                self._done()
        elif self.kind == "lie":
            self._lie(o)
        elif self.kind == "swap":
            if self.motion.step() is not False:
                self._done()
        else:
            self._clear(o)

    def _lie(self, o):
        """趴着等对方过完这一排：没有指令了、也趴够了才起来。"""
        b = self.fsm.body
        b.move_dir = 0
        if o is not None and o.dir:
            b.facing = o.dir
        if o is None and self.fsm.timer >= LIE_MIN:
            self._done()

    def _clear(self, o):
        """顺着竖杆往对方去的方向挪：往上到顶杆尖空着就站上去，往下到底就下到地上。"""
        fsm = self.fsm
        b = fsm.body
        if b.animation == "GetUpToBeamTip" or (
            self.dir < 0 and b.chunk0.y <= self.bar.lo + 1.0 and fsm.social.traffic.tip_free(fsm.win)
        ):
            if tip_up_tick(b, self.bar) is not False:
                self._done()
            return
        if o is None:
            climb_tick(b, self.bar, 0)
            self.calm += 1
            if self.calm > CALM:
                self._done()
            return
        self.calm = 0
        if self.dir > 0 and fsm.HL - b.chunk1.y < STEP_OFF_H:
            release(b)
            fsm.settle()
            return
        climb_tick(b, self.bar, -self.dir)

    def _done(self):
        self.fsm.settle()
        if self.resume is not None and self.fsm.state == "IdleStand":
            self.fsm.body.walk_to(*self.resume)

    def brk(self):
        b = self.fsm.body
        if b.on_pole:
            release(b)

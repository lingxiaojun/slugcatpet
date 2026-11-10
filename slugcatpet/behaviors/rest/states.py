"""趴下、睡觉、起身的状态。"""

from __future__ import annotations

from ... import tuning
from ...brain.skills import motor
from ...brain.social.bonds import LYING
from ...brain.social.space import LieMark
from ...brain.state import T_REST, T_WARM, State, LIGHT, REST
from ...core.creature import BLOCKED


def too_cold_to_sleep(fsm):
    return fsm.body.cold >= tuning.COLD_NOSLEEP


T_POINT_WAKE = 160  # 起身后指光标最长时长（tick）
WAKE_STABILIZE_TICKS = 30  # 落地稳定多久才算醒透

# 趴下的原因
EXHAUST, REST_A_BIT, NAP, HIBERNATE, LOUNGE = "exhaust", "rest", "nap", "hibernate", "lounge"
SLEEPS = (NAP, HIBERNATE)


def lie_down(fsm, why):
    """趴下；已经趴着就只换原因。"""
    st = fsm._states["LieDown"]
    if fsm.state == "LieDown":
        st.why = why
    else:
        st.next_why = why
        fsm._transition("LieDown")
    return True


def lying_for(fsm):
    """趴着或睡着的原因；没趴着 None。"""
    if fsm.state in LYING:
        return fsm._states[fsm.state].why
    return None


class LieDown(State):
    name = "LieDown"
    tier = T_REST
    energy = REST
    why = EXHAUST
    next_why = None
    lounge_ticks = 0  # 趴着发呆多久，进入前由待机设

    def enter(self):
        fsm = self.fsm
        b = fsm.body
        self.why, self.next_why = self.next_why or EXHAUST, None
        b.stop_walk()
        self.face = None
        # 这里趴下会压到别的猫就先挪到走得到的空地
        spot = fsm.social.space.lie_spot(fsm.win) if b.standing else None
        if spot is None:
            b.set_posture(False)
            return
        x, self.face = spot
        fsm.social.claim(fsm.win, "lie", LieMark(x, b.chunk1.y, self.face), state=self.name)
        if abs(x - b.chunk1.x) <= 1.0 or not b.walk_to(x):  # 就在脚下或走不过去：原地趴下
            b.facing = self.face
            b.set_posture(False)

    def tick(self, cursor, disturbed):
        fsm = self.fsm
        b = fsm.body
        if b.standing:
            if b.is_moving():
                return
            if b.walk_result == BLOCKED:  # 走到一半路断了：就地找个趴得下的朝向
                spot = fsm.social.space.lie_spot(fsm.win, here=True)
                self.face = spot[1] if spot is not None else self.face
            b.stop_walk()
            if self.face is not None:
                b.facing = self.face
            b.set_posture(False)
            fsm.timer = 0
        if self.why in SLEEPS:
            if too_cold_to_sleep(fsm):
                fsm._transition("WakeSequence")
            elif fsm.timer >= tuning.LIE_SETTLE_TICKS:
                fsm._states["Sleep"].why = self.why
                fsm._transition("Sleep")
            return
        if self.why == REST_A_BIT:
            done = fsm.timer >= tuning.REST_TICKS or fsm.body.energy >= tuning.REST_DONE_ENERGY
        elif self.why == LOUNGE:
            done = fsm.timer >= self.lounge_ticks
        else:
            done = fsm.body.energy >= tuning.EXHAUST_EXIT_ENERGY
        if done:
            fsm._transition("WakeSequence")


class Sleep(State):
    name = "Sleep"
    tier = T_REST
    energy = REST
    why = NAP

    def enter(self):
        fsm = self.fsm
        b = fsm.body
        self.nap_until = max(tuning.NAP_FLOOR, b.sleepiness - tuning.NAP_DROP)
        b.set_posture(False)
        b.stop_walk()

    def tick(self, cursor, disturbed):
        fsm = self.fsm
        b = fsm.body
        fsm.gfx.sleeping = True
        b.sleeping = True
        if too_cold_to_sleep(fsm):
            fsm._transition("WakeSequence")
            return
        b.sleepiness = max(0.0, b.sleepiness - tuning.SLEEP_RELIEF_RATE)
        if self.why == NAP:
            if b.sleepiness <= self.nap_until:
                fsm._transition("WakeSequence")
            return
        if fsm.timer >= tuning.HIBERNATE_TICKS:
            b.karma_gain()
            b.food_eat(-b.food_hibernate)
            b.energy = 1.0
            b.sleepiness = 0.0
            fsm._transition("WakeSequence")


class WakeSequence(State):
    name = "WakeSequence"
    tier = T_WARM
    energy = LIGHT

    def __init__(self, fsm):
        super().__init__(fsm)
        self._wake_stable = 0
        self.protest_left = 0

    def enter(self):
        fsm = self.fsm
        fsm.gfx.sleeping = False
        fsm.body.sleeping = False
        fsm.phase = 0
        self._wake_stable = 0
        fsm._wake_then = None

    def tick(self, cursor, disturbed):
        fsm = self.fsm
        b = fsm.body
        if fsm.phase == 0:
            fsm.gfx.sleeping = False
            b.sleeping = False
            b.set_posture(False)
            if fsm.gfx.sleep_curl <= 0.05:
                fsm.phase = 1
                b.request_jump("wake")
                b.set_posture(True)
        elif fsm.phase == 1:
            self._wake_stable = self._wake_stable + 1 if b.on_floor() else 0
            if fsm._wake_then is not None:
                if self._wake_stable > WAKE_STABILIZE_TICKS:
                    target = fsm._wake_then
                    fsm._wake_then = None
                    fsm._clear_hands()
                    fsm._transition(target)
                return
            if b.on_floor() and fsm.timer > 5:
                if fsm.rng.random() < 0.5:
                    fsm.phase = 2
                    fsm.timer = 0
                    fsm.point_side = 0
                else:
                    fsm.phase = 3
                    self.protest_left = fsm.rng.randint(1, 3)
                    fsm.timer = 0
        elif fsm.phase == 2:
            fsm.gfx.look_at = cursor
            motor.point_at_cursor(fsm, cursor, enforce_side=True)
            if fsm.timer >= T_POINT_WAKE or fsm._point_stopped:
                fsm._clear_hands()
                fsm._transition("IdleStand")
        elif fsm.phase == 3:
            if b.on_floor():
                if self.protest_left > 0 and fsm.timer > 16:
                    b.request_jump("protest")
                    self.protest_left -= 1
                    fsm.timer = 0
                elif self.protest_left <= 0 and fsm.timer > 16:
                    fsm._transition("IdleStand")

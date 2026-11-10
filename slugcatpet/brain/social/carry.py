"""背猫：一只猫用手拉着或背着另一只；被带的一方每 tick 贴到带的一方身上，待够了自己下来。"""

from __future__ import annotations

import math
import random

from ...core.chunkphys import GRAVITY
from ...core.units import TICK_RATE, inv_lerp, lerp
from .space import sign

HAND, BACK = "hand", "back"
HAND_DIST = 15.0  # 拉着时上身和被拉那一节的最远距离
BACK_LIFT = 14.0  # 背成年猫时它沿背者脊柱往上错开
BACK_EASE = 0.75
BACK_UP = 5.0  # 背上的猫上身被往上托的速度
PUP_HEAD_K, ADULT_HEAD_K = 0.3, 0.9  # 背上的猫上身速度每 tick 向托起速度靠拢的比例
DROP_LIFT = 10.0  # 从背上放下时臀先往上挪这么多
DROP_KICK = 3.0  # 放下时上身随机多出的速度上限
PATIENCE_S = (2.0, 20.0)  # 意愿最低到最高时待多少秒
PATIENCE_JITTER = 0.25
HAND_PATIENCE = 0.5  # 拉在手里时只待这么多倍
WIGGLE_TICKS = (100, 300)  # 拉在手里待够后，背者站着不动时扭这么久再挣脱
WIGGLE_JUMP = 0.95  # 扭的时候每 tick 按着跳键的概率
THROW_CAT_FAC = 0.35  # 扔猫比扔物件弱这么多
THROW_POW = (4.0, 14.0)  # 每一节加的出手速度的上下限
THROW_AHEAD = 8.0  # 出手时被扔的猫至少在背者身前这么远
THROW_BACK_UP = 20.0  # 从背上扔时先举到上身上方这么高
THROW_SAFE = 80  # 被扔出后这么多 tick 内摔落不晕不死
DROP_GHOST = 10  # 下来后这么多 tick 内不和带着它的猫相撞
CARRIER_STUN_DROP = (40, 80)  # 背者晕得比其间随机一个数久就松开
DROP_SPEED = 25.0  # 背者被甩得比这快就松开
POLE_LIFT = 0.75  # 背者爬杆时拉着的那只的重力抵掉这么多
LIGHT_FRAC = 0.75  # 轻于背者这么多倍的猫崽扔出时和上身齐平
CARRIER_STATES_DROP = (
    "LieDown",
    "Sleep",
    "Stunned",
    "Dead",
    "Swimming",
    "Ascension",
)  # 背者进入这些状态就松开
NO_TAKE_STATES = ("Stunned", "Dead", "Swimming", "Ascension", "DodgeKill")
HANG_ANIMS = ("HangFromBeam", "GetUpOnBeam")


def is_pup(body):
    return body.stats.hands < 2


class Link:
    """一对背猫关系：mode 为 hand / back，chunk 是拉着的那一节，patience 是还肯待的 tick 数，wiggle 是还要扭多久。"""

    __slots__ = ("carrier", "rider", "mode", "chunk", "patience", "wiggle", "stun_drop")

    def __init__(self, carrier, rider, mode, chunk, patience, stun_drop):
        self.carrier = carrier
        self.rider = rider
        self.mode = mode
        self.chunk = chunk
        self.patience = patience
        self.wiggle = 0
        self.stun_drop = stun_drop


class Carry:
    def __init__(self, social):
        self.social = social
        self.links = {}  # 被带的猫 → Link
        self._rng = random.Random(0)

    # 查询

    def link_of(self, rider):
        return self.links.get(rider)

    def carrying(self, carrier, mode=None):
        """carrier 用 mode 带着的那一对，没有为 None。"""
        for lk in self.links.values():
            if lk.carrier is carrier and (mode is None or lk.mode == mode):
                return lk
        return None

    def pick_mode(self, carrier):
        """拾取到一只猫时放在哪：手里已拉着猫、没空手、趴着时直接背到背上，否则拉在手里。"""
        cb = carrier.body
        if self.carrying(carrier, HAND) is not None or cb.free_hand() is None or not cb.standing:
            return BACK
        return HAND

    def can_take(self, carrier, rider, mode):
        """carrier 能不能用 mode 带上 rider：只叠一层，双方都清醒、不在水里和失重里，背者不是猫崽。"""
        if carrier is rider or rider in self.links or carrier in self.links:
            return False
        if self.carrying(rider) is not None or self.carrying(carrier, mode) is not None:
            return False
        cb, rb = carrier.body, rider.body
        if is_pup(cb):
            return False
        for p in (carrier, rider):
            b = p.body
            if b.dead or b.stun > 0 or b.swimming or b.zerog or b.suspended:
                return False
            beh = p.behavior
            if beh is not None and not p.controlled and (beh.grab.active or beh.state in NO_TAKE_STATES):
                return False
        if cb.on_pole and cb.animation in HANG_ANIMS:
            return False
        if mode == HAND and cb.free_hand() is None:
            return False
        return rb.ride is None

    # 上下

    def take(self, carrier, rider, mode):
        if not self.can_take(carrier, rider, mode):
            return False
        cb, rb = carrier.body, rider.body
        rider.release_organs()
        rb.let_go_pole()
        rb.stop_walk()
        rb.feet_stuck = rb.crawl_anchor = None
        near = min((rb.chunk0, rb.chunk1), key=lambda c: math.hypot(c.x - cb.chunk0.x, c.y - cb.chunk0.y))
        me, other = (carrier, rider) if rider.controlled else (rider, carrier)
        stun_drop = self._rng_of(carrier).randint(*CARRIER_STUN_DROP)
        lk = Link(carrier, rider, mode, near, self._patience(me, other, mode), stun_drop)
        self.links[rider] = lk
        self._set_mode(lk, mode)
        return True

    def move(self, lk, mode):
        """手里和背上之间换位置；目标位置占着或没空手返回 False。"""
        if lk.mode == mode or self.carrying(lk.carrier, mode) is not None:
            return False
        cb = lk.carrier.body
        if mode == HAND and cb.free_hand() is None:
            return False
        if mode == BACK:  # 甩到背上时上身往外让一下
            c0, rc = cb.chunk0, lk.chunk
            d = math.hypot(c0.x - rc.x, c0.y - rc.y)
            if d > 1e-6:
                c0.x += (c0.x - rc.x) / d * 2.0
                c0.y += (c0.y - rc.y) / d * 2.0
        else:
            lk.chunk = lk.rider.body.chunk0
        self._set_mode(lk, mode)
        return True

    def drop(self, rider):
        """放下 rider：手里的直接松手；背上的臀先往上挪一点，上身带着背者的速度随机晃开。"""
        lk = self.links.pop(rider, None)
        if lk is None:
            return
        cb, rb = lk.carrier.body, rider.body
        if lk.mode == HAND:
            _free_hand(cb)
        else:
            rng = self._rng_of(rider)
            a = rng.uniform(0.0, 2.0 * math.pi)
            k = DROP_KICK * rng.random()
            rb.chunk0.vx = cb.chunk0.vx + math.cos(a) * k
            rb.chunk0.vy = cb.chunk0.vy + math.sin(a) * k
            rb.chunk1.y -= DROP_LIFT
        rb.ride = rb.ride_with = None
        rb.pass_through((cb,), DROP_GHOST)

    def throw(self, rider, d, ix, iy):
        """往 d 那侧扔出去：角度和力度按按键与被扔的猫的质量，跟原版扔东西一样算；从背上扔时举过上身。"""
        lk = self.links.get(rider)
        if lk is None:
            return
        cb, rb = lk.carrier.body, rider.body
        from_back = lk.mode == BACK
        m = rb.total_mass
        ang, power = 45.0, 4.0
        if ix != 0 and iy == 0:
            ang = lerp(60.0, 50.0, inv_lerp(0.2, 0.3, m))
            power = lerp(12.5, 8.0, inv_lerp(0.2, 0.3, m) ** 2)
        elif ix != 0 and iy > 0:
            ang, power = 25.0, 9.0
        elif ix == 0 and iy > 0:
            ang, power = 5.0, 8.0
        if is_pup(rb):  # 猫崽单手拿得动
            power *= 2.0
            if ix != 0 and iy == 0:
                ang = 70.0
        power *= THROW_CAT_FAC
        del self.links[rider]
        if not from_back:
            _free_hand(cb)
        rb.ride = rb.ride_with = None
        rb.pass_through((cb,), DROP_GHOST)
        rb.feet_stuck = rb.crawl_anchor = None
        rb.stop_walk()
        rb.standing = True
        k = lerp(0.6, 0.3, inv_lerp(0.2, 0.5, m))
        sp = min(max(power / (lerp(m, 0.4, 0.2) * 2.0), THROW_POW[0]), THROW_POW[1])
        a = math.radians(ang * d)
        edge = max(cb.chunk0.x, cb.chunk1.x) if d > 0 else min(cb.chunk0.x, cb.chunk1.x)
        light = is_pup(rb) and m < cb.total_mass * LIGHT_FRAC
        for c in (rb.chunk0, rb.chunk1):
            if (c.x - edge) * d < THROW_AHEAD:
                c.x = edge + d * THROW_AHEAD
            if c.vx * d < 0:
                c.vx = 0.0
            if light:
                c.y = cb.chunk0.y
            c.vy = min(c.vy, 0.0)
            c.vx = lerp(c.vx * 0.35, cb.chunk0.vx, k) + math.sin(a) * sp
            c.vy = lerp(c.vy * 0.35, cb.chunk0.vy, k) - math.cos(a) * sp
            if from_back:
                c.y = cb.chunk0.y - THROW_BACK_UP
        rider.buffeted_until = rider.window.world_tick + THROW_SAFE

    def release_all(self, pet):
        """pet 离场、重置、交出操控时：它带着的和带着它的都放下。"""
        for rider, lk in list(self.links.items()):
            if rider is pet or lk.carrier is pet:
                self.drop(rider)

    # 被带着的猫

    def steer(self, rider):
        """拉在手里的 AI 猫照着背者按的键动：同向走、跟着趴下站起、跟着跳；扭着要下来时往外蹬、乱跳。"""
        rb = rider.body
        rb.stop_walk()
        lk = self.links.get(rider)
        if lk is None or lk.mode != HAND:
            return
        cb = lk.carrier.body
        inp = cb._ctrl_input
        x, jmp = (inp[0].x, _jump_pressed(cb)) if inp is not None else (0, False)
        if cb.bodyMode == "Crawl" and rb.standing:
            rb.set_posture(False)
        elif cb.bodyMode == "Stand" and not rb.standing:
            rb.set_posture(True)
        if lk.wiggle > 0:
            x = sign(rb.chunk0.x - cb.chunk0.x) or -(cb.facing or 1)
            jmp = self._rng_of(rider).random() < WIGGLE_JUMP * (1.0 - WIGGLE_JUMP)  # 松开又按下才起跳
        rb.move_dir = x
        if jmp and rb.on_floor():
            rb.request_jump()

    # 每 tick

    def sync(self, pets):
        """所有猫推进完之后调：解掉不成立的，贴位置，数待了多久。"""
        for rider, lk in list(self.links.items()):
            if self._broken(lk, pets):
                self.drop(rider)
                continue
            if lk.mode == BACK:
                _place_back(lk)
            else:
                _pull_hand(lk)
            if rider.controlled and _jump_pressed(rider.body):
                self.drop(rider)
                continue
            self._tick_patience(lk)

    def draw_order(self, pets):
        """绘制顺序：背上的猫画在背者前一个，显得在它身后。"""
        backs = {lk.carrier: lk.rider for lk in self.links.values() if lk.mode == BACK}
        if not backs:
            return pets
        riders = set(backs.values())
        out = []
        for p in pets:
            if p in riders:
                continue
            r = backs.get(p)
            if r is not None and r in pets:
                out.append(r)
            out.append(p)
        return out

    # 内部

    def _rng_of(self, pet):
        return pet.behavior.rng if pet.behavior is not None else self._rng

    def _set_mode(self, lk, mode):
        cb, rb = lk.carrier.body, lk.rider.body
        if lk.mode == HAND and mode != HAND:
            _free_hand(cb)
        if mode == HAND and cb.hand_cat is None:
            cb.hand_cat = cb.free_hand()
            cb.hand_load = 0.0 if is_pup(rb) else rb.total_mass  # 成年猫占满双手、拖慢背者
        lk.mode = mode
        lk.wiggle = 0
        rb.ride = mode
        rb.ride_with = cb

    def _patience(self, me, other, mode):
        """待多少 tick：me 对 other 越想凑近越久，拉在手里减半。"""
        w = (self.social.pull(me, other) + 1.0) * 0.5
        jitter = self._rng_of(me).uniform(1.0 - PATIENCE_JITTER, 1.0 + PATIENCE_JITTER)
        s = lerp(PATIENCE_S[0], PATIENCE_S[1], w) * jitter
        if mode == HAND:
            s *= HAND_PATIENCE
        return int(s * TICK_RATE)

    def _broken(self, lk, pets):
        carrier, rider = lk.carrier, lk.rider
        if carrier not in pets or rider not in pets:
            return True
        cb, rb = carrier.body, rider.body
        if cb.dead or rb.dead or rb.stun > 0 or cb.stun > lk.stun_drop:
            return True
        if cb.swimming or cb.zerog or rb.swimming:
            return True
        if cb.on_pole and cb.animation in HANG_ANIMS:
            return True
        if not rider.controlled and rider.behavior is not None and rider.behavior.grab.active:
            return True
        cbeh = carrier.behavior
        if not carrier.controlled and cbeh is not None and cbeh.state in CARRIER_STATES_DROP:
            return True
        return math.hypot(cb.chunk0.vx, cb.chunk0.vy) > DROP_SPEED

    def _tick_patience(self, lk):
        if lk.patience > 0:
            lk.patience -= 1
            return
        rider, carrier = lk.rider, lk.carrier
        if rider.controlled:
            cb = carrier.body
            if cb.on_floor():
                cb.request_jump("protest")
            self.drop(rider)
            return
        if lk.mode == BACK:
            self.drop(rider)
            return
        inp = carrier.body._ctrl_input
        if inp is not None and inp[0].x != 0:  # 背者走着时不扭，停下再扭
            lk.wiggle = 0
            return
        if lk.wiggle == 0:
            lk.wiggle = self._rng_of(rider).randint(*WIGGLE_TICKS)
            return
        lk.wiggle -= 1
        if lk.wiggle == 0:
            self.drop(rider)


def _free_hand(cb):
    cb.hand_cat = None
    cb.hand_load = 0.0
    cb.arm_aim["l"] = cb.arm_aim["r"] = None


def _jump_pressed(b):
    inp = b._ctrl_input
    return inp is not None and inp[0].jmp and not inp[1].jmp


def _head_pos(pet):
    """背者画出来的头的位置，没有画面时用上身。"""
    g = getattr(pet, "gfx", None)
    if g is not None:
        return g.head.x, g.head.y
    c0 = pet.body.chunk0
    return c0.x, c0.y


def _place_back(lk):
    """背着：猫崽的臀压在背者头上、上身被往上托着自己晃；成年猫沿背者脊柱往上错开叠着。"""
    cb, rb = lk.carrier.body, lk.rider.body
    c0, c1 = cb.chunk0, cb.chunk1
    r0, r1 = rb.chunk0, rb.chunk1
    if is_pup(rb):
        r1.x, r1.y = _head_pos(lk.carrier)
        k = PUP_HEAD_K
    else:
        dx, dy = c0.x - c1.x, c0.y - c1.y
        n = math.hypot(dx, dy)
        ux, uy = (dx / n, dy / n) if n > 1e-6 else (0.0, -1.0)
        for c, src in ((r0, c0), (r1, c1)):
            c.x += (src.x + ux * BACK_LIFT - c.x) * BACK_EASE
            c.y += (src.y + uy * BACK_LIFT - c.y) * BACK_EASE
        k = ADULT_HEAD_K
    r1.vx, r1.vy = c0.vx, c0.vy
    r0.vx += (c0.vx * 0.5 - r0.vx) * k
    r0.vy += ((c0.vy - BACK_UP) * 0.5 - r0.vy) * k
    rb.facing = cb.facing


def _pull_hand(lk):
    """拉着：上身和被拉那一节超过 HAND_DIST 时按质量互相拉近；背者爬杆时托住被拉的猫。"""
    cb, rb = lk.carrier.body, lk.rider.body
    cc, rc = cb.chunk0, lk.chunk
    dx, dy = rc.x - cc.x, rc.y - cc.y
    d = math.hypot(dx, dy)
    if d > HAND_DIST:
        ux, uy = dx / d, dy / d
        m = 0.0 if cc.pinned else rc.mass / (cc.mass + rc.mass)
        k = (d - HAND_DIST) * m * 0.5
        cc.x += ux * k
        cc.y += uy * k
        cc.vx += ux * k
        cc.vy += uy * k
        k = (d - HAND_DIST) * (1.0 - m)
        rc.x -= ux * k
        rc.y -= uy * k
        rc.vx -= ux * k
        rc.vy -= uy * k
    if cb.on_pole and cb.animation not in ("BeamTip", "StandOnBeam") and not rb.on_pole:
        rc.vy -= GRAVITY * POLE_LIFT * (1.0 - rb.submersion)
    if cb.hand_cat is not None:
        cb.arm_aim[cb.hand_cat] = (rc.x, rc.y)

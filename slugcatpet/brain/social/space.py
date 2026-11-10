"""占位：每 tick 把每只猫占的地方、姿势、去向整理成一份；能站哪、能不能到都只按这一份算。
停住只看猫自己（没有要去的地方、趴着、叫不动、原地等迎面的猫先过）；在走的猫按落脚处预约一段，预约两两不相交。"""

from __future__ import annotations

import math

from ... import tuning
from ...core.creature import WALK_STOP_EPS
from ..state import CROWD
from .bonds import LYING

FLOOR, AIR, ON_CAT, POLE, CARRIED, SWIM, ZEROG = "floor", "air", "on_cat", "pole", "carried", "swim", "zerog"
WALK_CLEAR = 34.0  # 杆上的猫身子低于地面以上这么高时也挡着地上走的猫
STATIC_STEP = 8.0  # 寻路绕开的猫按这么粗记版本，挪一点不算变
SLIDE_V = 3.0  # 身子横着滑得比这快（被撞飞）不算停住；被挤着慢慢挪的还算
SPOT_PAD = 10.0  # 寻路时绕开的猫身子外扩这么多以内的位置不走
SWIM_TURN = 0.35  # 想游方向的横向分量超过此值才算往一边游
TOUCH_TOL = 1.0  # 两只猫的身子重叠这么多以内算挨着，不算压到
LAND_MARGIN = 4.0  # 落脚时离两边的猫至少留这么远
LAND_WIDTH = 16.0  # 两排猫中间留给臀落脚的地方比这窄就落不稳（起跳点前后差几像素，落点也跟着差），算同一排
STEP = 8.0  # 找能趴下的位置时一格格试的间隔
LIE_REACH = 160.0  # 找能趴下的位置最远找这么远


class LieMark:
    """预约的趴下位置：臀在 x，朝 d 趴。"""

    __slots__ = ("x", "y", "d")

    def __init__(self, x, y, d):
        self.x, self.y, self.d = x, y, d


class Occ:
    """一只猫这一 tick 的占位。pose：floor / air / on_cat 踩在别的猫身上 / pole / carried 被带着 / swim / zerog；
    lane、s、d、v：杆、水、失重时所在的通道、通道上的位置、去向、速度；lo、hi、top、bottom：身体占的范围（正在趴下
    或起身的按变完的样子）；lying 趴着或正在趴下；parked 停住；blocks 挡着地上走的猫；can 能叫它让，fixed 一定不能；
    key 是迎面时谁过的先后；under 是踩着的猫；target 是预约的落脚处。"""

    __slots__ = tuple(
        "pet pose lane s y d v can fixed key giving lo hi top bottom lying parked blocks state under target".split()
    )

    @property
    def x(self):
        return self.pet.body.chunk1.x


def sign(v):
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _layer(me, o):
    return abs(o.pet.body.chunk1.y - me.body.chunk1.y) <= tuning.FLOOR_SAME_DY


def stuck(pet):
    """怎么都叫不动：操控中、定身、被带着、晕、死、被光标拎着。"""
    b, fsm = pet.body, pet.behavior
    if getattr(pet, "controlled", False) or pet.held is not None or b.ride is not None:
        return True
    return b.dead or b.stun > 0 or fsm.grab.active


def _busy(fsm):
    """在忙让路以外更要紧的事（睡觉、吃、躲雨……），或者不会让路。"""
    if fsm.state == "Sleep" or "MakeWay" not in fsm._states:
        return True
    return fsm.state != "MakeWay" and fsm.blocked(CROWD)


def stand_half(body):
    """站着时臀两侧身子占的宽度。"""
    return max(body.chunk0.rad, body.chunk1.rad)


def lie_extent(body, x, d):
    """臀在 x、朝 d 趴下后身子占的 (左, 右)。"""
    reach = x + d * (body.conn_rest + body.chunk0.rad)
    r = body.chunk1.rad
    return min(x - r, reach), max(x + r, reach)


def extent(o, d):
    """停住的猫占的 (左, 右)；地上站着、叫得动的连它朝 d 趴下的样子也算上，叫不叫它让，过它的猫看到的都一样。"""
    if o.can and o.pose == FLOOR and not o.lying:
        a, c = lie_extent(o.pet.body, o.x, d)
        return min(o.lo, a), max(o.hi, c)
    return o.lo, o.hi


def build(pet, waiting, last):
    """一只猫这一 tick 的占位；waiting 是原地等迎面的猫先过的，last 是它上一 tick 的占位；不在场上（没身体）返回 None。"""
    b = getattr(pet, "body", None)
    fsm = getattr(pet, "behavior", None)
    if b is None or fsm is None:
        return None
    o = Occ()
    o.pet = pet
    c0, c1 = b.chunk0, b.chunk1
    o.lo = min(c0.x - c0.rad, c1.x - c1.rad)
    o.hi = max(c0.x + c0.rad, c1.x + c1.rad)
    o.top = min(c0.y - c0.rad, c1.y - c1.rad)
    o.bottom = max(c0.y + c0.rad, c1.y + c1.rad)
    fixed = stuck(pet)
    o.fixed = fixed or _busy(fsm)
    o.can = not o.fixed
    o.key = (fsm._cur().tier, pet.index)
    o.state = fsm.state
    o.giving = o.state == "MakeWay"
    o.lane, o.s, o.y, o.d = None, c1.x, c1.y, 0
    o.v = abs(c1.x - last.s) if last is not None and last.lane is None else abs(c1.vx)
    o.lying = o.parked = o.blocks = False
    o.under = o.target = None
    if fsm.grab.active:
        o.pose = AIR
    elif b.ride is not None:
        o.pose = CARRIED
    elif b.zerog:
        o.pose = ZEROG
        o.can = False
        pole = b.zerog_pole
        if pole is not None:
            ax, ay = pole.bx - pole.ax, pole.by - pole.ay
            n = math.hypot(ax, ay) or 1.0
            ax, ay = ax / n, ay / n
            fx, fy = b.zerog_pole_intent
            o.lane = ("Z", id(pole))
            o.s = c0.x * ax + c0.y * ay
            o.d = sign(round(fx * ax + fy * ay))
            o.v = abs(c0.vx * ax + c0.vy * ay)
    elif b.swimming:
        o.pose = SWIM
        sx, _ = b._swim_dir()
        o.lane = ("W",)
        o.s, o.y = c0.x, c0.y
        o.d = sign(sx) if abs(sx) > SWIM_TURN else 0
        o.v = abs(c0.vx)
        o.can = not fixed and not (b.no_dive or b.swim_resting)
    elif b.on_pole:
        o.pose = POLE
        _pole_lane(o, b)
        o.blocks = o.bottom > pet._HL - WALK_CLEAR
        o.parked = o.blocks and o.v < SLIDE_V
    elif b.on_floor() or b.on_support():
        o.pose = FLOOR if b.on_floor() else ON_CAT
        o.blocks = o.pose == FLOOR
        turning = b.standing == (b.lying_span() is not None)  # 正在趴下或正在起身
        o.lying = (not b.standing) or fsm.state in LYING
        if o.pose == FLOOR and not b.standing:
            lo, hi = lie_extent(b, c1.x, b.facing or 1)  # 按趴好后的长度占地
            o.lo, o.hi = min(o.lo, lo), max(o.hi, hi)
        elif o.pose == FLOOR and turning:
            o.lo, o.hi = c1.x - stand_half(b), c1.x + stand_half(b)  # 正在起身：按站起来的样子
        o.target = b.target_x if b.walk_target_x is not None else None
        o.d = sign(o.target - c1.x) if o.target is not None else sign(b.move_dir)
        still = o.target is None or o.lying or o.fixed or pet in waiting
        o.parked = o.pose == FLOOR and still and not b.move_dir and o.v < SLIDE_V
        o.can = o.can and o.pose == FLOOR and (o.giving or b.standing)
    else:
        o.pose = AIR
    return o


def _pole_lane(o, b):
    anim = b.animation
    ix, iy = b.pole_input
    c0, c1 = b.chunk0, b.chunk1
    if anim in ("ClimbOnBeam", "GetUpToBeamTip", "BeamTip"):
        tip = anim != "ClimbOnBeam"
        o.lane = ("V", round(b.pole_x))
        o.s, o.d, o.v = c0.y, (0 if anim == "BeamTip" else -iy), abs(c0.vy)
        o.can = o.can and not tip
    elif anim == "HangFromBeam":
        o.lane = ("H", round(b.pole_y))
        o.s, o.d, o.v = c0.x, ix, abs(c0.vx)
    elif anim == "StandOnBeam":
        o.lane = ("S", round(b.pole_y))
        o.s, o.d, o.v = c1.x, ix, abs(c1.vx)


class Space:
    def __init__(self, social):
        self.social = social
        self.occs = {}  # 猫 → Occ
        self._static_key = None
        self.static_version = 0  # 绕开的猫变了就加一，寻路缓存按它失效
        self._boxes = []  # 这一 tick 寻路要绕开的猫：[(猫, 框)]
        self._memo = {}

    def tick(self):
        occs = {}
        for p in self.social.win.pets:
            o = build(p, self.social.traffic.waiting, self.occs.get(p))
            if o is not None:
                occs[p] = o
        self.occs = occs
        for o in occs.values():
            if o.pose == ON_CAT:
                ch = o.pet.body.foot_chunk
                o.under = next((p for p in occs if ch is p.body.chunk0 or ch is p.body.chunk1), None)
        self._boxes = [
            (o.pet, (o.lo - SPOT_PAD, o.hi + SPOT_PAD, o.top - SPOT_PAD, o.bottom + SPOT_PAD))
            for o in occs.values()
            if o.parked and (o.lying or o.fixed)
        ]
        self._memo = {}
        key = tuple(
            (p.index, round(box[0] / STATIC_STEP), round(box[2] / STATIC_STEP)) for p, box in self._boxes
        )
        if key != self._static_key:
            self._static_key = key
            self.static_version += 1

    # 查询

    def of(self, pet):
        return self.occs.get(pet)

    def memo(self, key, fn):
        """这一 tick 里同一个 key 只算一次 fn()。"""
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def others(self, me):
        return [o for p, o in self.occs.items() if p is not me]

    def lanes(self):
        """杆、水、失重通道上的猫：{通道: [Occ]}。"""
        out = {}
        for o in self.occs.values():
            if o.lane is not None:
                out.setdefault(o.lane, []).append(o)
        return out

    def floor(self, me):
        """和我同一层、挡着地上走的猫（含低处杆上的）。"""
        return self.memo(("floor", me), lambda: [o for o in self.others(me) if o.blocks and _layer(me, o)])

    def mates(self, me, states=None):
        """和我同一层、站或趴在地上的猫的占位；给了 states 时只要处在这些状态里的。"""
        return [o for o in self.floor(me) if o.pose == FLOOR and (states is None or o.state in states)]

    def parked(self, me):
        """和我同一层、停住的猫（含低处杆上的）。"""
        return [o for o in self.floor(me) if o.parked]

    def booked(self, me):
        """别的猫预约了的 [(左, 右)]：要去的落脚处、预约的趴下位置。"""
        out = []
        for o in (o for o in self.others(me) if _layer(me, o)):
            if o.target is not None:  # 预约跟着意图走，停下等着时也还占着
                h = stand_half(o.pet.body)
                out.append((o.target - h, o.target + h))
            m = self.social.held(o.pet, "lie")
            if m is not None:
                out.append(lie_extent(o.pet.body, m.x, m.d))
        return out

    def free(self, me, lo, hi, parked=False):
        """[lo, hi] 这段地上没有别的猫停着的身子或预约（挨着不算）；parked 为真时只看停住的猫。"""
        spans = [(o.lo, o.hi) for o in self.parked(me)] + ([] if parked else self.booked(me))
        return not any(a < hi - TOUCH_TOL and lo + TOUCH_TOL < c for a, c in spans)

    def band(self, me):
        """我的臀能到的地上范围。"""
        b = me.body
        h = stand_half(b)
        return (h if b.walk_min is None else b.walk_min), (me._WL - h if b.walk_max is None else b.walk_max)

    def blocks(self, me):
        """我站着时臀不能待的 [[左, 右]]：两边停住的猫按排（见 rows）整排算，加上别人的预约，按我的半宽外扩后合并。"""
        x = me.body.chunk1.x
        spans = [sorted((d * near, d * far)) for d in (-1, 1) for _, near, far in self._groups(me, x, d)]
        pad = stand_half(me.body) - TOUCH_TOL
        out = []
        for lo, hi in sorted((a - pad, c + pad) for a, c in spans + self.booked(me)):
            if out and lo <= out[-1][1]:
                out[-1][1] = max(out[-1][1], hi)
            else:
                out.append([lo, hi])
        return out

    def settle_x(self, me, x, tol=None, keep=None):
        """离 x 最近能站的臀位置：x 被占着时我这一侧的边在 tol（None 不限）以内就站这侧，不用越过别人；
        已经定下的落脚处 keep 还站得下就不换；站不下 None。"""
        lo, hi = self.band(me)
        blocks = self.blocks(me)
        if keep is not None and lo <= keep <= hi and not any(a < keep < c for a, c in blocks):
            return keep
        x = min(max(x, lo), hi)
        here = me.body.chunk1.x
        for a, c in blocks:
            if a < x < c:
                ref = x if a < here < c else here  # 我挤在这块里时两边都要穿过别人，才取离 x 近的
                cands = sorted((q for q in (a, c) if lo <= q <= hi), key=lambda q: abs(q - ref))
                return next((q for q in cands if tol is None or abs(q - x) <= tol - WALK_STOP_EPS), None)
        return x

    def reach(self, me, x, tol, keep=None):
        """走去 x 能落脚的臀位置（见 settle_x）：加上停步的误差离 x 不超过 tol，路上每一排都过得去；到不了 None。"""
        t = self.settle_x(me, x, tol, keep)
        if t is None or (tol is not None and abs(t - x) > tol - WALK_STOP_EPS):
            return None
        o = self.occs.get(me)
        rows = self.rows(o, t) if o is not None and o.pose in (FLOOR, ON_CAT) else []
        if all(self.social.traffic.verdict(o, *r)[1] for r in rows):
            return t
        return None if keep is None else self.reach(me, x, tol)  # 定下的落脚处过不去了：重挑

    def rows(self, a, t):
        """a 到 t 之间停住的猫分成的排：[(这一排, 过完后落脚段的左, 右)]，近的在前；落脚段到下一排（或走路范围的边）为止。"""
        d = sign(t - a.x)
        if not d:
            return []
        groups = self._groups(a.pet, a.x, d, a.under)
        h = stand_half(a.pet.body) + LAND_MARGIN
        lo, hi = self.band(a.pet)
        ends = [g[1] - h for g in groups[1:]] + [hi if d > 0 else -lo]
        segs = zip(groups, ends)
        return [(g, *sorted((d * (far + h), d * end))) for (g, near, far), end in segs if near < d * t]

    def _groups(self, me, x, d, under=None):
        """从 x 往 d 要过的停住的猫（踩着的没走过它那头也算），中间落不稳脚的算一排：[[这一排, 近边, 远边]]，坐标乘 d。"""
        ahead = [o for o in self.parked(me) if ((o.lo + o.hi) * 0.5 - x) * d > 0.0]
        u = self.occs.get(under)
        if u is not None and u not in ahead and ((u.hi if d > 0 else u.lo) - x) * d > 0.0:
            ahead.append(u)
        gap = 2.0 * (stand_half(me.body) + LAND_MARGIN) + LAND_WIDTH
        groups = []
        for o in sorted(ahead, key=lambda o: o.lo * d if d > 0 else -o.hi):
            lo, hi = extent(o, d)
            near, far = (lo, hi) if d > 0 else (-hi, -lo)
            if groups and near - groups[-1][2] < gap:
                groups[-1][0].append(o)
                groups[-1][2] = max(groups[-1][2], far)
            else:
                groups.append([[o], near, far])
        return groups

    def lie_spot(self, me, here=False):
        """我要趴下的位置 (x, 朝向)：原地先试现在的朝向再试反过来，不行再一格格往两边找走得到的；here 为真时只试原地；都不行 None。"""
        b = me.body
        if not (b.on_floor() or b.on_support()):
            return None
        x0 = b.chunk1.x
        lo, hi = self.band(me)
        d0 = b.facing or 1
        for k in range(1 if here else int(LIE_REACH / STEP) + 1):
            for q in (x0 + k * STEP, x0 - k * STEP) if k else (x0,):
                for d in (d0, -d0):
                    a, c = lie_extent(b, q, d)
                    if lo <= a + TOUCH_TOL and c - TOUCH_TOL <= hi and self.free(me, a, c):
                        if not k or self.reach(me, q, WALK_STOP_EPS) is not None:
                            return q, d
        return None

    def keepout(self, me, rain=None):
        """寻路要绕开的位置：大雨时露天的，和别的猫里停住又叫不动的、趴着的身边；都没有返回 None。"""

        def make():
            boxes = [box for p, box in self._boxes if p is not me]
            return None if not boxes and rain is None else _Keepout(rain, boxes, self.static_version)

        return self.memo(("ko", me, rain), make)


class _Keepout:
    """寻路用：keeps_out(x, y) 为真的位置不经过；version 变了缓存的路线作废。"""

    def __init__(self, rain, boxes, static_version):
        self.rain = rain
        self.boxes = boxes
        self.version = (rain.version if rain is not None else None, static_version)

    def keeps_out(self, x, y):
        if self.rain is not None and self.rain.keeps_out(x, y):
            return True
        return any(lo < x < hi and top < y < bottom for lo, hi, top, bottom in self.boxes)

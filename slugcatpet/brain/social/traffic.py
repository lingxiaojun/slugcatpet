"""通行：按占位找出谁挡谁的路，定下谁让、怎么过。地上在走的猫每 tick 按现在的样子重求落脚处（space.reach），
到不了当场交回行为；身前一排进了视线就照它的走法（ground.plan，按这排的样子缓存）走，落地后从新的位置接着来。
迎面的按一个先后定谁过，输的原地等（停住，过的一方把它算进那排）；同向在走的跟在后面。"""

from __future__ import annotations

import logging
import math
from collections import namedtuple

from ... import tuning
from ...core.stride import ARRIVE, TO, Stride
from .ground import BRAKE, WALK_V, Plan, gait, lead, near_edge, plan, snapshot
from .space import FLOOR, ON_CAT, POLE, TOUCH_TOL, sign, stand_half, stuck

log = logging.getLogger(__name__)

LEAD = 10  # 按对方速度提前这么多 tick 开始让
CONTACT = {"W": 26.0, "H": 22.0, "S": 22.0, "V": 40.0, "Z": 22.0}  # 通道上两只猫挨上的距离
WATER_BAND = 26.0  # 水里上下相差这么多以内才会挡路
HOP_DIST = 44.0  # 水里离挡路的猫这么近时从水面跳过去
SHOVE_TICKS = 20  # 顶着杆尖上的猫往上挤这么久就把它顶下去
SHOVE_FALL_VX = 3.5  # 被顶下去时的横向速度
SHOVE_GHOST = 45  # 被顶下去后穿过同一根杆上的猫的 tick，免得和它们撞翻
LOOK = 90.0  # 身前一排、在走的猫离这么近才管；照走法走从这和 ground.lead 里远的那个起
STOP_GAP = 8.0  # 跟在别的猫后面时隔这么远
OFF_WAIT = 8
OFF_GIVE_UP = 60
GET_OFF_GHOST = 20
FALLBACK_TICKS = 200  # 兜底：有要去的地方却这么久一点没靠近，记下现场并交回行为（设计上不该发生）
YIELD = ("lie", "swap", "clear", "off")  # 由让路状态执行的指令

# 让路指令：kind 见 YIELD（off 是从别的猫身上下来），by 是对方，dir 在地上是趴下的朝向、杆上是对方的去向
Order = namedtuple("Order", "kind by dir")


def _knock_off(pet, lane):
    """把站在杆尖上的猫顶下去，往它偏的那侧掉。"""
    b = pet.body
    if getattr(pet, "controlled", False) or pet.held is not None or not b.on_pole:
        return False
    side = sign(b.chunk0.x - b.pole_x) or (b.facing or 1)
    b.let_go_pole()
    b.chunk0.vx += side * SHOVE_FALL_VX
    b.chunk1.vx += side * SHOVE_FALL_VX * 0.4
    b.pass_through(lane, SHOVE_GHOST)
    if pet.behavior is not None:
        pet.behavior.pole_lost()
    return True


class Traffic:
    def __init__(self, social):
        self.social = social
        self.orders = {}  # 猫 → Order
        self.tips = set()  # 有猫站着（或正爬上去）的杆尖所在通道
        self.shove = {}  # 猫 → 顶着杆尖上的猫往上挤了几 tick
        self.called = {}  # 猫 → {叫过让路的猫: (指令, 方向)}
        self.waiting = set()  # 迎面输了原地等的猫，下一 tick 的占位里算停住
        self.plans = {}  # 一排的样子（ground.snapshot 的键）→ 走法或 None
        self.progress = {}  # 猫 → (意图, 离落脚处最近到过多近, 之后多少 tick 没更近)
        self.perched = {}
        self._shoving = {}

    def update(self):
        self.orders.clear()
        self.waiting.clear()
        self.tips.clear()
        space = self.social.space
        for p in self.social.win.pets:
            b = getattr(p, "body", None)
            if b is not None:
                b.lane_hold, b.lane_blocked = 0, False
        self._shoving = {}
        lanes = space.lanes()
        for lane, occs in lanes.items():
            if lane[0] == "V":
                self.tips.update(lane for o in occs if o.pet.body.animation != "ClimbOnBeam")
            if len(occs) < 2:
                continue
            for a in occs:
                if a.d:
                    self._scan(a, occs)
        self._ground(space)
        self.shove = {p: self.shove.get(p, 0) + 1 for p in self._shoving}
        for p, top in self._shoving.items():
            if self.shove[p] >= SHOVE_TICKS:
                self.shove.pop(p)
                lane = [q.pet.body for q in lanes.get(("V", round(top.body.pole_x)), ())]
                if _knock_off(top, lane):
                    self.social.bonds.nudge(p, top, tuning.BOND_SHOVE)

    def tip_free(self, pet):
        """我所在竖杆的杆尖空着。"""
        return ("V", round(pet.body.pole_x)) not in self.tips

    # 杆上、水里、失重

    def _scan(self, a, occs):
        kind = a.lane[0]
        c = CONTACT[kind]
        reach = c + a.v * LEAD
        for b in occs:
            if b is a or (b.d == a.d and not b.giving) or (kind == "W" and abs(b.y - a.y) > WATER_BAND):
                continue
            rel = (b.s - a.s) * a.d
            if 0.0 < rel <= reach:
                self._meet(kind, a, b, rel, c)

    def _meet(self, kind, a, b, rel, c):
        """a 往 b 走、b 挡着：定谁让、怎么过。"""
        if b.d != -a.d:
            y = b if b.can else None
        else:  # 迎面：正在让路的不再掉头让
            y = min((r for r in (a, b) if r.can and not r.giving), key=lambda r: r.key, default=None)
        by = b if y is a else a
        passes = not a.giving and y is not a and (y is not None or b.d != -a.d or a.key < b.key)
        controlled = getattr(a.pet, "controlled", False)
        if kind == "W":
            if a.pet.body.swim_pass.busy or b.pet.body.swim_pass.busy:
                return  # 已经有一只在错开
            if y is None and a.can:
                y = a
            if y is not None:
                y.pet.body.swim_pass.request(True, by.s, by.pet.body.chunk0.vx)
            elif passes and rel <= HOP_DIST:
                a.pet.body.swim_pass.request(False, b.s, b.pet.body.chunk0.vx)
        elif kind == "Z":
            if rel <= c and not (b.d == -a.d and a.key < b.key) and not controlled:
                a.pet.body.lane_hold = a.d  # 迎面时优先级低的接着走
        else:
            if y is not None:
                self._order(y.pet, "swap" if kind in ("H", "S") else "clear", by.pet, by.d)
            if rel <= c and not controlled:
                a.pet.body.lane_hold = -a.d if kind == "V" else a.d
                if kind == "V" and a.d < 0 and b.pet.body.animation == "BeamTip":
                    self._shoving[a.pet] = b.pet
                elif y is None:
                    a.pet.body.lane_blocked = True  # 谁都让不开：当场交回

    # 地上

    def _ground(self, space):
        perched, self.perched = self.perched, {}
        for o in space.occs.values():
            pet, b = o.pet, o.pet.body
            idle = b.walk_target_x is None
            if o.pose == ON_CAT and not stuck(pet):
                self._perch(space, o, idle, *perched.get(pet, (0, False)))
            if idle or getattr(pet, "controlled", False):
                self._end(pet)
                continue
            if o.pose in (FLOOR, ON_CAT):  # 空中、杆上：落地再说
                t = space.reach(pet, b.walk_target_x, b.walk_tol, keep=b.target_x)
                if t is None or self._fallback(o, t):
                    self._block(o)
                    continue
                b.target_x = o.target = t  # 后面的猫这一 tick 就避开它
                if o.pose == FLOOR:  # 在地上和停住的猫叠着：穿过它走开，免得拖着它走
                    b.pass_through([q.pet.body for q in space.parked(pet) if _overlap(o, q)], 2)
                d = sign(t - o.x)
                if not self._walkers(space, o, t, d):
                    self._pass(space, o, t, d)
            self._call(space, o)

    def _perch(self, space, o, idle, n, called):
        pet, b = o.pet, o.pet.body
        n += 1
        if not (idle or called):
            n, called = 0, False
        elif b.stride is not None:
            pass
        elif n >= OFF_GIVE_UP or (idle and n >= OFF_WAIT and space.reach(pet, o.x, None) is None):
            b.pass_through(_touching(space, pet), GET_OFF_GHOST)
            n, called = 0, False
        elif idle and n >= OFF_WAIT:
            self._order(pet, "off", None, 0)
            called = True
        self.perched[pet] = (n, called)

    def _pass(self, space, o, t, d):
        """身前一排进了视线就照它的走法走；走法变了（这排的样子变了）就换。从远处走过来、走着过不比站定费事的，走着过。"""
        b = o.pet.body
        rows = space.rows(o, t)
        if not rows or (o.under is None and (near_edge(rows[0][0], d) - o.x) * d > max(LOOK, lead(b, d))):
            self._drop_stride(b)
            return
        st = b.stride
        p = st and self.verdict(o, *rows[0], st.walking)[1]
        if st is not None and (p is None or p.steps != st.plan.steps):
            self._drop_stride(b)
        if b.stride is not None:
            return
        x0, p = self.verdict(o, *rows[0])  # reach 刚判过能过
        steps = p.steps if abs(x0 - o.x) <= ARRIVE or o.under is not None else [(TO, x0), *p.steps]
        st = Stride(steps, p, False)
        if o.under is None and not p.yields:
            xw, w = self.verdict(o, *rows[0], walking=True)
            ws = w and (w.steps if w.steps[0][0] == TO else [(TO, xw), *w.steps])
            room = ws and (ws[0][1] - o.x) * d >= gait(b.stats)[1]  # 走到起步处已是走路速度
            if room and w.cost <= p.cost + abs(x0 - xw) / WALK_V + BRAKE:
                st = Stride(ws, w, True)
        b.stride = st

    def _walkers(self, space, o, t, d):
        """身前在走的猫：同向的按身子、迎面的按它整段要走的路，近边在我的落脚处之前（和 rows 同一口径）才算。
        迎面的按先后定谁过，让的一方原地等；同向的跟在后面。要等或跟着时返回真。"""
        best = None
        for q in space.floor(o.pet) if d else ():
            if q.target is None or not q.d or q.parked or (q.d == -d and self._wins(o, q)):
                continue
            mid, gap = (q.lo + q.hi) * 0.5, (q.lo - o.hi) if d > 0 else (o.lo - q.hi)
            near = (q.lo if d > 0 else q.hi) * d
            if q.d == -d:
                near = min(near, q.target * d - stand_half(q.pet.body))  # 它退后助跑时也还挡着
            if (mid - o.x) * d > 0.0 and near < t * d and gap <= LOOK:
                best = (gap, q) if best is None or gap < best[0] else best
        if best is None:
            return False
        if best[1].d == -d:
            self.waiting.add(o.pet)
        if best[1].d == -d or best[0] <= STOP_GAP:
            o.pet.body.lane_hold = d  # 迎面输了原地等；同向的跟到它身后停下（还算在走）
        self._drop_stride(o.pet.body)
        return True

    @staticmethod
    def _wins(a, q):
        """迎面碰上时 a 先过：已经开始过的先过，叫不动的先过，再按优先级。"""
        st = [o.pet.body.stride for o in (a, q)]
        ka, kq = ((s is not None and (s.i > 0 or s.jumped), not o.can, o.key) for s, o in zip(st, (a, q)))
        return ka > kq

    def _call(self, space, o):
        """叫要让的猫让开：走到等着那一步时叫走法里要让的，叫过的一直叫到越过它；走法里要让的都让好了才放行。"""
        pet, st = o.pet, o.pet.body.stride
        called = self.called.setdefault(pet, {})
        if st is not None and st.waiting:
            called.update(st.plan.yields)
        d = sign(pet.body.target_x - o.x)
        for y, (kind, f) in list(called.items()):
            yo = space.of(y)
            if yo is None or ((yo.hi if d > 0 else yo.lo) - (o.lo if d > 0 else o.hi)) * d <= 0.0:
                del called[y]  # 越过它了
            else:
                self._order(y, kind, pet, f)
        if st is not None:
            st.go = all(self._cleared(space.of(y), k) for y, (k, _) in st.plan.yields.items())

    @staticmethod
    def _cleared(yo, kind):
        """让路的猫让好了：趴平了，或从低处杆上挪开了。"""
        flat = yo is not None and yo.pet.body.lying_flat()
        return yo is None or (flat if kind == "lie" else not (yo.pose == POLE and yo.blocks))

    @staticmethod
    def _drop_stride(b):
        if b.stride is not None and not b.stride.jumped:
            b.stride = None

    def _block(self, o):
        """路断了：当场交回行为（停在猫背上的下一 tick 就穿过它落到地上）。"""
        self._end(o.pet)
        o.pet.body.walk_blocked()

    def _end(self, pet):
        self.called.pop(pet, None)
        self.progress.pop(pet, None)
        self._drop_stride(pet.body)

    def _fallback(self, o, t):
        """兜底：有要去的地方却很久一点没靠近，说明设计有漏洞或出了物理意外；记下现场，交回行为。"""
        w, dist = o.pet.body.walk_target_x, abs(t - o.x)
        w0, best, n = self.progress.get(o.pet, (None, 0.0, 0))
        self.progress[o.pet] = (w, dist, 0) if w0 != w or dist < best - 1.0 else (w, best, n + 1)
        if n + 1 >= FALLBACK_TICKS:
            self.fallback(o.pet, "走不近")
            return True
        return False

    def fallback(self, pet, why):
        """兜底触发：记下现场（每只猫的编号、状态、姿势、位置、停没停、落脚处，和这只的过路动作）。"""
        occs = self.social.space.occs.values()
        scene = [(q.pet.index, q.state, q.pose, round(q.x), q.parked, q.target) for q in occs]
        st = pet.body.stride
        rec = (why, self.social.now, pet.index, pet.body.walk_target_x, st and st.steps, st and st.i, scene)
        log.warning("通行兜底触发：%s", rec)

    def verdict(self, a, row, glo, ghi, walking=False):
        """a 过这一排、落到 [glo, ghi] 的走法：(起步处, Plan 或 None)，按这排的样子（ground.snapshot）缓存。"""
        d = sign((glo + ghi) * 0.5 - a.x)
        key, x0, occs = snapshot(self.social.space, a.pet, d, row, glo, ghi, walking, a.under)
        if key not in self.plans:
            if len(self.plans) > 4096:
                self.plans.clear()
            self.plans[key] = plan(key, a.pet.body)
        p = self.plans[key]
        return x0, p and Plan(p.steps, {occs[i].pet: (k, f) for i, k, f in p.yields}, p.cost)

    def _order(self, pet, kind, by, d):
        if pet not in self.orders:
            self.orders[pet] = Order(kind, by, d)


def _overlap(a, q):
    return q.lo < a.hi - TOUCH_TOL and a.lo + TOUCH_TOL < q.hi


def _touching(space, pet):
    h = pet.body.chunk1
    return [
        q.pet.body
        for q in space.others(pet)
        if any(
            math.hypot(c.x - h.x, c.y - h.y) <= c.rad + h.rad + TOUCH_TOL
            for c in q.pet.body.collision_chunks()
        )
    ]

"""猫的身体：两节质点、连接、运动和程序化脚步。"""

from __future__ import annotations
import math
import random

from .. import tuning
from ..characters.stats import DEFAULT_STATS
from . import chunkphys as cp
from .chunkphys import BodyChunk, solve_conn
from .swim_pass import SwimPass
from .units import K_VEL, K_IMP, TICK_RATE, lerp, inv_lerp, clampf
from .vecmath import _ang_from_up, _rot

RUN_UPPER = 4.2 * K_VEL
RUN_LOWER = 4.0 * K_VEL
CRAWL_SPEED = 2.5 * K_VEL
CRAWL_SLOW = 1.0 * K_VEL
H_ACCEL = (2.4 * 0.5) * K_IMP
SKID_DAMP = 0.354  # 接地滑停回收率

STAND_HEAD = -(1.5 * K_IMP)
STAND_FEET = +(4.5 * K_IMP)
DEF_STAND_HEAD = -(4.0 * K_IMP)
DEF_STAND_FEET = +(4.0 * K_IMP)

FEET_EASE = 0.5

JUMP_STAND_HEAD = -(4.0 * K_IMP)
JUMP_STAND_FEET = -(3.0 * K_IMP)
PROTEST_HEAD = -(2.0 * K_IMP)
PROTEST_FEET = -(1.0 * K_IMP)
PROTEST_BOOST = 4
JUMPBOOST_DECAY = 1.5 * K_VEL
JUMPBOOST_GAIN = 0.3 * K_IMP

CONN_STAND = cp.DIST_STAND
CONN_CRAWL = cp.DIST_CRAWL
HIP_SINK_EASE = 0.06

WALK_STOP_EPS = 2.0
ARRIVED, BLOCKED = "arrived", "blocked"  # 走路的结果
FLAT_TOL = 4.0  # 头和臀的下沿高低差这么多以内算趴平
FOOT_TICKS = 5  # 碰到别的猫后这么多 tick 内还算踩着；踩着跳完也冷却这么久
WALK_SLOW_DIST = 24.0  # 离走路目标这么近时开始减速

ZEROG_KICK = 5.4 * K_VEL
ZEROG_HIP_KICK = 5.0 * K_VEL
ZEROG_HEAD_MAX = 5.4 * K_VEL
ZEROG_HIP_MAX = 5.0 * K_VEL
ZEROG_CANJUMP = 12
ZEROG_SWIM_PUSH = 0.15 * K_VEL
ZEROG_SWIM_WALL = 0.2 * K_VEL
ZEROG_SWIM_HIP = 0.1 * K_VEL
ZEROG_SWIM_MAX = 2.5 * K_VEL
ZEROG_KICK_LERP = 0.5
ZEROG_KICK_BODYAXIS = 0.25

ZEROG_GRAB_DIST = 10.0  # 头在杆那一格里才抓得住
ZEROG_POLE_PULL = 0.1
ZEROG_POLE_CLIMB = 1.05 * K_VEL
ZEROG_POLE_HIP = 0.7
ZEROG_POLE_KICK_HEAD = 5.4  # 从杆上蹬开时头的速度上限
ZEROG_POLE_KICK_HIP = 5.0
ZEROG_POLE_SIDE = 5.0  # 头和臀分在杆两侧的距离

CARRY_OFF_X = 20.0
CARRY_OFF_Y = 12.0
EAT_BITES_MIN = 1

HANDS = ("l", "r")  # 主手在前
SWITCH_HANDS_RATE = 1.0 / 12.0


def other_hand(side):
    return "r" if side == "l" else "l"


class Throw:
    """一次投掷：投掷者、方向（四向，y↓）、力度、瞄准方向（可无）、是否放过光标。"""

    __slots__ = ("by", "dir_x", "dir_y", "force", "aim", "spare_cursor")

    def __init__(self, by, dir_x, dir_y=0, force=1.0, aim=None, spare_cursor=False):
        self.by = by
        self.dir_x = int(dir_x)
        self.dir_y = int(dir_y)
        self.force = float(force)
        self.aim = aim
        self.spare_cursor = spare_cursor


WATER_FRICTION = 0.96


class SlugcatBody:
    collision_layer = 1  # 与果/黏菌同层互推

    def __init__(
        self,
        hip_xy,
        world_w: float,
        world_h: float,
        energy=1.0,
        temper=0.0,
        food=None,
        karma=None,
        stats=DEFAULT_STATS,
        food_q=0,
    ):
        self.stats = stats
        hx, hy = hip_xy
        self.W = float(world_w)
        self.H = float(world_h)
        chunk_mass = cp.MASS * stats.weight_fac
        self.chunk0 = BodyChunk(0, hx, hy - CONN_STAND, cp.RAD0, chunk_mass)
        self.chunk1 = BodyChunk(1, hx, hy, cp.RAD1, chunk_mass)
        self.conn_rest = float(CONN_STAND)
        self.conn_type = "Normal"

        self.standing = True
        self.move_dir = 0
        self.walk_target_x = None  # 行为要去的地方（意图）
        self.target_x = None  # 腿实际走向的落脚处：离意图最近、走得到的站位
        self.walk_tol = WALK_STOP_EPS  # 落脚处离意图多远以内算到；None 为不限
        self.walk_result = None  # 这次走路的结果：arrived 到了 / blocked 走到一半路断了
        self.holding = False  # 这一 tick 腿因为 lane_hold 停着
        self.reach = lambda x, tol: x  # 走去 x 能落脚的位置，到不了 None；没有别的猫时都走得到
        self.walk_speed_target = None
        self.dead = False
        self._jump_pending = None  # None/"stand"/"protest"
        self._jump_hold = None  # 下次跳持跳时长（None=到衰减完）
        self._jump_hold_left = None  # 本次腾空剩余持跳（None=不截断）
        self.jump_boost = 0.0

        self.feet_stuck = None  # 钉脚锚点 (x, y)，None 为不钉
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self.lie_haste = 1.0  # 趴下过程的速度倍率，站起复位
        self._end_x1 = None  # 上一 tick 结束时臀的 x，分出被外力推开的位移
        self._prev_end_x1 = None

        self.crawl_sink = 0.0
        self.hip_sink = 0.0
        self._floor_h = self.H

        self.bodyMode = "Stand"
        self.animation = None
        self._flip_spin = 0  # 自动翻跳的力矩方向
        self._flip_mult = 2.5  # 自动翻跳的力矩倍率
        self.on_pole = False
        self.pole_obj = None  # 抓着的杆物体
        self.pole_x = 0.0
        self.pole_y = 0.0
        self.pole_frame = 0  # 杆上动画帧
        self.pole_input = (0, 0)  # 杆上这一 tick 想按的方向
        self.lane_hold = 0  # 被前面的猫挡住、不许走的方向（杆上、地上），由通行写
        self.lane_blocked = False  # 这一 tick 杆上被挡住、谁都让不开
        self.ghost = 0  # 还要穿过所有猫多少 tick（翻上、翻下横杆时）
        self.ghost_of = {}
        self.foot_chunk = None  # 臀踩着的别的猫的那一节
        self.foot_mass = 0.0  # 踩着的那只猫的总质量
        self.foot_n = 0  # >0：还算踩着几 tick；<0：踩着跳完的冷却
        self.ride = None  # 被别的猫带着：hand 拉在手里 / back 背在背上
        self.ride_with = None  # 带着我的那只猫的身体
        self.hand_cat = None  # 拉着别的猫的那只手
        self.hand_load = 0.0  # 拉着的猫的质量，跑得慢一些
        self.stride = None  # 通行插进走路里的小动作，走路时自己执行
        self.beam_span = (-1e9, 1e9)  # 吊着的横杆两端

        self.facing = 1  # +1 右 / -1 左
        self.stance = 0.42 * CONN_STAND
        self.foot_lift = 4.0
        self.step_threshold = 0.5 * CONN_STAND
        self.step_speed = 0.18
        self.step_predict = 0.5
        self.lfoot = [hx - self.stance, self.H]
        self.rfoot = [hx + self.stance, self.H]
        self._lstep = {"on": False, "start": 0.0, "goal": 0.0, "lerp": 1.0}
        self._rstep = {"on": False, "start": 0.0, "goal": 0.0, "lerp": 1.0}
        self.stride_phase = 0.0
        self.stride_rate = 0.06
        self._stride_prev_x = None
        self.turn = 0.0  # 0=正面 .. 1=侧面
        self.turn_rate = 0.12
        self.walk_bob_y = 0.0
        self.walk_bob_amp = 0.0
        self.walk_bob_freq = 1.0

        self.breath = 0.0
        self.last_breath = 0.0
        self.sleeping = False  # 睡眠态：呼吸周期恒 80

        # 气喘 aerobic ∈[0,1]：跳、爬、扔累积，随时间回落；驱动呼吸
        self.aerobic = 0.0
        self.exhausted = False  # 累倒：由能力按 aerobic 判定，核心只结算后果
        self.slow_stun = 0  # 动作迟缓计数，>0 时跑、爬变慢
        self.endless_roll = False  # 未累倒时翻滚不自然停（重压能力写入）
        self.throw_hooks = []  # 扔出武器后调用 fn(body, obj, throw)，由能力登记
        self.pierce_hooks = []  # 扔出的武器扎中 / 穿过东西时调用 fn(body, 武器, 目标, 目标原本活着)，由能力登记
        self.wounds = 0.0  # 累计伤，到 1 即死，复活清零

        # energy ∈[0,1]，1=满 0=耗尽
        self.energy = float(energy)

        # temper ∈[-1,1]，负疏远正亲密
        self.temper = float(temper)
        self.temper_fac = (1.0, 1.0)  # 好感涨、跌的倍率，按性格设

        # food ∈[0,food_max]，上限/冬眠阈值按种族
        self.food_max = stats.max_food
        self.food_hibernate = stats.food_hibernate
        self.food = int(tuning.FOOD_INIT if food is None else food)
        self.food_q = int(food_q) % 4 if self.food < self.food_max else 0  # 攒着的 1/4 格

        # karma_max 随种族，老存档超值在此夹回
        self.karma_max = tuning.KARMA_MAX if stats.karma_cap is None else stats.karma_cap
        self.karma = max(0, min(self.karma_max, int(tuning.KARMA_INIT if karma is None else karma)))

        # cold ∈[0,2]，0.8起抽搐 ≥1可冻死
        self.cold = 0.0
        # sleepiness ∈[0,1]，醒着慢慢涨，睡觉降
        self.sleepiness = tuning.SLEEPY_INIT
        self.cold_gain = 0.0  # 上一 tick cold 增量

        self.walk_min = None
        self.walk_max = None

        self.suspended = False
        self.hover = False
        self.hover_airfric = 0.788

        # 零重力：window 每 tick 注入 zerog/room_gravity
        self.zerog = False
        self.room_gravity = 1.0
        self.canJump = 0
        self._zerog_kick = False
        self._zerog_wall = (0, 0)
        self._zerog_kick_dir = (0.0, 0.0)
        self.zerog_pole = None
        self.zerog_pole_intent = (0.0, 0.0)
        self.zerog_grab_dir = [1, 1]  # 失重抓杆时最后按的方向，y 向上为正

        # 水面波形（None=无水），_wy* 为本 tick 水面高缓存，x 变了现算
        self.water_surface = None
        self._wy0 = self._wy1 = None
        self._wyx0 = self._wyx1 = 0.0

        # 游泳/憋气/淹死；air_frac 是死亡下界统一口径
        self.air_death = 0.0
        self.no_dive = False  # 憋气极短，从不主动潜水（爆破能力写入）
        self.swim_resting = False  # 浮在水面歇着
        self.swim_pass = SwimPass(self)
        self.drown_burst_air = None  # 憋气降到此值即溺爆，None=不会（爆破能力写入）
        self.air_in_lungs = 1.0
        self.lungs_exhausted = False
        self.drown = 0.0
        self.submersion = 0.0
        self.head_sub = 0.0
        self.hip_sub = 0.0
        self.submerged = False
        self.swimming = False
        self.swim_target = None
        self.swim_mode = None  # "deep"/"surface"/None
        self.swim_cycle = 0.0
        self.swim_input_x = 0
        self.swim_force = 0.0
        self.water_jump_delay = 0
        self.swim_boost_dist = tuning.SWIM_BOOST_DIST
        self._swim_wf = WATER_FRICTION
        self.drown_burst = False  # 溺爆待触发，FSM 下一 tick 结算
        self.aquatic = False  # 水生体质（鳃能力写入）：潜泳冲刺/冲刺后低水阻/浮泳拖臀

        # 手动控制：总闸 / 每 tick 输入源 / 10 帧历史环
        self._ctrl_on = False
        self._input_provider = None
        self._ctrl_input = None

        self.impact_cb = None
        self.on_hurt = None  # 撞击伤害回调 (kind, ticks)：kind 为 "stun" / "die"
        self.drag_speed = 0.0  # 拖拽时光标的真实速度（未限速）
        self._edge_contact = (False, False, False)
        self._spin_prev = None
        self._spin_rate = 0.0
        self._spin_hold = 0
        self._spin_done = False
        self.bubble_cb = None
        self.grasps = {"l": None, "r": None}  # 手持物：l 为主手
        self.stun = 0
        self.arm_aim = {"l": None, "r": None}
        self.arm_full_reach = 24.0
        self.eat_raise = {"l": 0.0, "r": 0.0}
        self.switch_hands = 0.0  # 换手进度，0=未在换

    @property
    def total_mass(self):
        return self.chunk0.mass + self.chunk1.mass

    def collision_chunks(self):
        return (self.chunk0, self.chunk1)

    def collides_with(self, other):
        """穿行期间、背在背上时不和别的猫相撞；拉着的一对之间也不撞。"""
        if not isinstance(other, SlugcatBody):
            return True
        if self.ghost or other.ghost or other in self.ghost_of or self in other.ghost_of:
            return False
        if self.ride == "back" or other.ride == "back":
            return False
        return self.ride_with is not other and other.ride_with is not self

    def pass_through(self, bodies, ticks):
        for o in bodies:
            if o is not self:
                self.ghost_of[o] = max(self.ghost_of.get(o, 0), ticks)

    @property
    def steady(self):
        """睡着时只被别的猫挤动，物品推不动。"""
        return self.sleeping

    def clamp_cold(self):
        self.cold = 0.0 if self.cold < 0.0 else (1.0 if self.cold > 1.0 else self.cold)

    @property
    def hip(self):
        return self.chunk1

    @property
    def upper(self):
        return self.chunk0

    def on_floor(self):
        return self.chunk0.on_floor or self.chunk1.on_floor

    def note_foot(self, mine, other, ch):
        """空中臀碰到别的猫、且不低于它那一节中心偏下半个半径时，记下踩着的那一节。"""
        if not isinstance(other, SlugcatBody) or mine is not self.chunk1 or self.foot_n < 0:
            return
        if self.on_floor() or self.on_pole or self.swimming or self.zerog:
            return
        if mine.y < ch.y + ch.rad * 0.5:
            self.foot_n, self.foot_chunk, self.foot_mass = FOOT_TICKS, ch, other.total_mass

    def on_support(self):
        """悬空但踩着别的猫。"""
        return self.foot_n > 0 and not self.on_floor()

    def afoot(self):
        """在地上走着：着地，或正在走路里插的跨越中。走路阶段的判断用它，别把跨越当成离地。"""
        return self.on_floor() or self.stride is not None

    def jump_on_chunk(self):
        """踩着别的猫起跳：下面的猫越轻跳得越低，并把它往下压。"""
        c0, c1, ch = self.chunk0, self.chunk1, self.foot_chunk
        f = min(max(self.foot_mass / self.total_mass, 0.0), 1.0) ** 0.7
        for c in (c0, c1):
            c.vy *= 1.0 - f * 0.9
        c0.vy -= 4.0 * f * K_VEL
        c1.vy -= 3.0 * f * K_VEL
        self.jump_boost = 7.0
        ch.vy += 7.0 * f * self.total_mass / ch.mass * K_VEL
        self.foot_n, self.foot_chunk = -FOOT_TICKS, None
        self.feet_stuck = self.crawl_anchor = None

    def walking(self):
        """看起来在走：有走路目标或方向，且没晕、没停在等别的猫让路。"""
        st = self.stride
        if self.stun > 0 or self.holding or (st is not None and (st.waiting or st.braking)):
            return False
        return self.is_moving() or self.move_dir != 0

    def is_moving(self):
        return self.walk_target_x is not None and abs(self.target_x - self.chunk1.x) > WALK_STOP_EPS

    def set_walk_band(self, world_w):
        """横向能到的范围按身子定：两节中心离左右屏幕边至少较大的那个半径，正好碰到边。"""
        m = max(self.chunk0.rad, self.chunk1.rad)
        self.walk_min = m
        self.walk_max = world_w - m

    def walk_to(self, x, tol=WALK_STOP_EPS):
        """走去 x，落脚处离 x 不超过 tol（None 为不限）；到不了当场返回 False，不改现在的走路。"""
        x = float(x)
        if self.walk_min is not None:
            x = min(max(x, self.walk_min), self.walk_max)
        if self.walk_target_x is not None and abs(x - self.walk_target_x) < 0.5 and tol == self.walk_tol:
            return True
        t = self.reach(x, tol)
        if t is None:
            return False
        self.walk_target_x, self.target_x, self.walk_tol, self.walk_result = x, t, tol, None
        return True

    def walk_blocked(self):
        """走到一半路断了：停下，结果记为 blocked，交回行为。"""
        self.stop_walk()
        self.walk_result = BLOCKED

    def lying_span(self):
        """趴在地上时身体占的横向范围（含半径）；没趴着为 None。"""
        if self.bodyMode not in ("Crawl", "Dead") or not self.on_floor():
            return None
        c0, c1 = self.chunk0, self.chunk1
        return min(c0.x - c0.rad, c1.x - c1.rad), max(c0.x + c0.rad, c1.x + c1.rad)

    def lying_flat(self):
        """已经趴平（头和臀一样贴地），别的猫能踩上来、跳过去。"""
        if self.lying_span() is None:
            return False
        c0, c1 = self.chunk0, self.chunk1
        return abs((c0.y + c0.rad) - (c1.y + c1.rad)) < FLAT_TOL

    def stop_walk(self):
        self.walk_target_x = self.target_x = None
        self.walk_result = None
        self.move_dir = 0
        self.stride = None

    def set_posture(self, standing: bool):
        self.standing = bool(standing)
        if standing:
            self.lie_haste = 1.0

    def request_jump(self, kind="stand", hold_ticks=None):
        self._jump_pending = kind
        self._jump_hold = hold_ticks

    def tip_launch(self, hold_ticks=None, move_dir=0):
        c0, c1 = self.chunk0, self.chunk1
        c0.pinned = c1.pinned = False
        self.on_pole = False
        self.animation = None
        self.feet_stuck = None
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self.standing = True
        self._jump_pending = self._jump_hold = None
        self._jump_hold_left = hold_ticks
        c0.vy = self.stats.jump_head
        c1.vy = self.stats.jump_feet
        self.jump_boost = self.stats.jump_boost
        self.stop_walk()
        self.move_dir = int(move_dir)
        self.aerobic_increase(self.stats.jump_aerobic)

    def pole_jump(self, direction, move_dir=None):
        c0, c1 = self.chunk0, self.chunk1
        c0.pinned = c1.pinned = False
        self.on_pole = False
        self.animation = None
        self.feet_stuck = None
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self.standing = True
        self._jump_pending = self._jump_hold = self._jump_hold_left = None
        self.jump_boost = 0.0
        d = 1.0 if direction >= 0 else -1.0
        s = self.stats
        c0.vx = s.pole_jump_head_vx * d
        c0.vy = s.pole_jump_head_vy
        c1.vx = s.pole_jump_feet_vx * d
        c1.vy = s.pole_jump_feet_vy
        self.stop_walk()
        self.move_dir = int(d if move_dir is None else move_dir)
        self.aerobic_increase(s.jump_aerobic)

    def backflip_launch(self, direction, boosted=False):
        c0, c1 = self.chunk0, self.chunk1
        c0.pinned = c1.pinned = False
        self.on_pole = False
        self.feet_stuck = None
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self.standing = True
        self._jump_pending = self._jump_hold = self._jump_hold_left = None
        self.jump_boost = self.stats.flip_boost if boosted else 0.0
        d = 1 if int(direction) >= 0 else -1
        s = self.stats
        c0.y -= 6.0
        c1.y -= 6.0
        c0.cx = c0.cy = c1.cx = c1.cy = 0
        c0.vx = s.backflip_vx * d
        c0.vy = s.flip_head if boosted else s.backflip_c0_vy
        c1.vx = s.backflip_vx * d
        c1.vy = s.flip_feet if boosted else s.backflip_c1_vy
        self.animation = "Flip"
        self._flip_spin = -d  # 力矩方向=飞行反向
        self._flip_mult = 2.5  # 滑铲源翻跳
        self.stop_walk()
        self.move_dir = d
        self.facing = -d
        self.aerobic_increase(s.jump_aerobic)

    def pyro_boost(self, ix, iy, overheat=False):
        c0, c1 = self.chunk0, self.chunk1
        ix = int(ix)
        iy = int(iy)
        if ix != 0:
            c0.vy = max(c0.vy, 0.0) - 8.0
            c1.vy = max(c1.vy, 0.0) - 7.0
            self.jump_boost = 6.0
        if ix == 0 or iy == 1:
            if overheat:
                c0.vy, c1.vy, self.jump_boost = -16.0, -15.0, 10.0
            else:
                c0.vy, c1.vy, self.jump_boost = -11.0, -10.0, 8.0
        if iy == 1:
            c0.vx = 10.0 * ix
            c1.vx = 8.0 * ix
        else:
            c0.vx = 15.0 * ix
            c1.vx = 13.0 * ix
        self._jump_hold_left = None
        self.animation = "Flip"
        self._flip_spin = ix if ix != 0 else self.facing
        self._flip_mult = 1.0  # 非滑铲源
        self.bodyMode = "Default"

    def release_to_air(self, move_dir=0):
        c0, c1 = self.chunk0, self.chunk1
        c0.pinned = c1.pinned = False
        self.on_pole = False
        self.animation = None
        self.feet_stuck = None
        self.crawl_anchor = None
        self.standing = True
        self.stop_walk()
        self.move_dir = int(move_dir)

    def die(self):
        self.dead = True
        self.bodyMode = "Dead"
        self.standing = False
        self.stop_walk()
        self.feet_stuck = None
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self.suspended = False
        self.hover = False
        self._jump_pending = None
        self._jump_hold = None
        self.drop_carried()
        self.stun = 0
        self.zerog_pole = None
        self.let_go_pole()

    def drop_lost_pole(self, poles):
        """抓着的杆已不在 poles 里就松手；有重力时松了返回 True。"""
        if self.zerog_pole is not None and all(p is not self.zerog_pole for p in poles):
            self.zerog_pole = None
        if self.on_pole and self.pole_obj is not None and all(p is not self.pole_obj for p in poles):
            self.let_go_pole()
            return True
        return False

    def let_go_pole(self):
        """松开抓着的杆，回到普通物理。"""
        if self.on_pole:
            self.chunk0.pinned = self.chunk1.pinned = False
            self.on_pole = False
            self.animation = None

    def revive(self):
        self.dead = False
        self.wounds = 0.0
        self.air_in_lungs = 1.0  # 复活重置憋气，防残留标志立即再死
        self.drown = 0.0
        self.lungs_exhausted = False
        self.submerged = False
        self.drown_burst = False

    def set_control_input(self, pkg):
        if self._ctrl_input is None:
            from ..control.input import InputBuffer

            self._ctrl_input = InputBuffer()
        self._ctrl_input.push(pkg)
        self._ctrl_on = True

    def step(self):
        self._prev_end_x1, self._end_x1 = self._end_x1, None
        self.ghost = max(0, self.ghost - 1)
        if self.ghost_of:
            self.ghost_of = {o: n - 1 for o, n in self.ghost_of.items() if n > 1}
        if self.foot_n:
            self.foot_n += -1 if self.foot_n > 0 else 1
            if self.foot_n == 0:
                self.foot_chunk = None
        self._temper_update()
        self._drag_impacts()
        if self._input_provider is not None:
            # 晕、死时推空输入，免得醒来那一刻读到晕期间的按键边沿
            pkg = self._input_provider()
            if self.stun > 0 or self.dead:
                from ..control.input import InputPackage

                pkg = InputPackage()
            if pkg is not None:
                self.set_control_input(pkg)
        if self._ctrl_on:
            from ..control.moves_wall import tick_counters

            tick_counters(self)  # 杆上、水里、晕着时也要倒计时
        if self.dead:
            self._step_dead()
            return
        self._aerobic_update()
        self._switch_update()
        self._update_submersion()
        self._lung_update()
        if self.ride == "back":
            self._step_ride()
            return
        swimming = self._should_swim()
        self.swimming = swimming
        if not swimming:
            self.swim_pass.reset()
        if self.hover:
            self._step_hover()
            return
        if self.zerog and not swimming:
            self._step_zerog()
            return
        if swimming:
            self._step_swim()
            return
        if self.stun > 0:
            self.stun -= 1
            if self._ctrl_on:
                self.let_go_pole()  # 操控中晕了就松杆
            self._step_stunned()
            return
        if self.on_pole and self._ctrl_on:
            from ..control.moves_pole import ctrl_pole_update

            ctrl_pole_update(self)
        if self.on_pole:
            self._pole_update()
        elif not self.suspended or self._ctrl_on:  # 操控时吊在舌头上照常能动
            self._movement_update()
        else:
            self._suspended_update()
        ws = self.water_surface
        for c, wy, wx in ((self.chunk0, self._wy0, self._wyx0), (self.chunk1, self._wy1, self._wyx1)):
            if ws is not None and c.x != wx:
                wy = ws.level_at(c.x)
            c.update(
                self.W,
                self._floor_h,
                impact=self._impact,
                room_gravity=self.room_gravity,
                water_y=wy,
                buoyancy=self.stats.buoyancy,
                water_friction=WATER_FRICTION,
            )
        self.conn_rest = CONN_STAND if self.standing else CONN_CRAWL
        # 手动控制翻滚、滑铲时两节间距取 10
        rest = 10.0 if (self._ctrl_on and self._ctrl_roll_direction != 0) else self.conn_rest
        solve_conn(self.chunk0, self.chunk1, rest, ctype=self.conn_type)
        self.chunk0.clamp_inside(self.W, self._floor_h)
        self.chunk1.clamp_inside(self.W, self._floor_h)
        self._end_x1 = self.chunk1.x
        self._gait_update()
        self._breath_update()
        self._apply_carry()

    def _step_ride(self):
        """背在别的猫背上：站着、不走，照常受重力；背猫随后把它拉回背上。"""
        self.swimming = False
        self.bodyMode = "Default"
        self.animation = None
        self.standing = True
        self.feet_stuck = self.crawl_anchor = None
        self.chunk0.vy += DEF_STAND_HEAD
        self.chunk1.vy += DEF_STAND_FEET
        for c in (self.chunk0, self.chunk1):
            c.update(self.W, self._floor_h, room_gravity=self.room_gravity)
        self.conn_rest = CONN_STAND
        solve_conn(self.chunk0, self.chunk1, CONN_STAND, ctype=self.conn_type)
        self._breath_update()

    def _step_hover(self):
        ws = self.water_surface
        for c, wy, wx in ((self.chunk0, self._wy0, self._wyx0), (self.chunk1, self._wy1, self._wyx1)):
            if ws is not None and c.x != wx:
                wy = ws.level_at(c.x)
            c.update(
                self.W,
                self.H,
                gravity=0.0,
                air_friction=self.hover_airfric,
                impact=self._impact,
                room_gravity=self.room_gravity,
                water_y=wy,
                buoyancy=self.stats.buoyancy,
                water_friction=WATER_FRICTION,
            )
        solve_conn(self.chunk0, self.chunk1, self.conn_rest, ctype="Normal")
        self.chunk0.clamp_inside(self.W, self.H)
        self.chunk1.clamp_inside(self.W, self.H)
        self._breath_update()

    # 水浸没度/憋气/游泳（y↓）
    def _update_submersion(self):
        ws = self.water_surface
        if ws is None:
            self.head_sub = self.hip_sub = self.submersion = 0.0
            self._wy0 = self._wy1 = None
            return
        c0, c1 = self.chunk0, self.chunk1
        self._wy0 = wy0 = ws.level_at(c0.x)
        self._wy1 = wy1 = ws.level_at(c1.x)
        self._wyx0 = c0.x
        self._wyx1 = c1.x
        hs = clampf((c0.y + c0.rad - wy0) / (2.0 * c0.rad), 0.0, 1.0)
        ps = clampf((c1.y + c1.rad - wy1) / (2.0 * c1.rad), 0.0, 1.0)
        if self.swimming:  # 游泳时滤掉波面噪声
            k = tuning.SWIM_SUB_FILTER
            hs = self.head_sub + (hs - self.head_sub) * k
            ps = self.hip_sub + (ps - self.hip_sub) * k
        self.head_sub = hs
        self.hip_sub = ps
        self.submersion = self.head_sub if self.head_sub > self.hip_sub else self.hip_sub

    def _should_swim(self) -> bool:
        """进出游泳判据：站浅水不算，进出水位滞回。"""
        if self.water_surface is None or self.on_pole:
            return False
        if self.swimming:
            return self.submersion > tuning.SWIM_EXIT
        if self.submersion <= tuning.SWIM_ENTER:
            return False
        if self.on_floor() and self.head_sub < tuning.SWIM_ENTER:
            return False
        if self.bodyMode == "WallClimb" and self.head_sub < 1.0:  # 贴墙时头没全没进水不游
            return False
        return True

    def _swim_dir(self):
        t = self.swim_target
        if t is None or self.stun > 0:
            return 0.0, 0.0
        dx, dy = t[0] - self.chunk0.x, t[1] - self.chunk0.y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return 0.0, 0.0
        return dx / d, dy / d

    def _swim_x_arrived(self) -> bool:
        """目标与头部横向距离在死区内。"""
        t = self.swim_target
        return t is not None and abs(t[0] - self.chunk0.x) < tuning.SWIM_ARRIVE_X

    def _swim_up_still(self) -> bool:
        t = self.swim_target
        if t is None:
            return False
        dx, dy = t[0] - self.chunk0.x, t[1] - self.chunk0.y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return False
        return (dy / d) < -0.5 and abs(dx / d) < 0.3

    @property
    def air_frac(self) -> float:
        d = self.air_death
        if d <= 0.0:
            return self.air_in_lungs
        return max(0.0, (self.air_in_lungs - d) / (1.0 - d))

    def _lung_update(self):
        if self.hover:  # 超度悬浮不耗气
            return
        s = self.stats
        c0 = self.chunk0
        if self.head_sub > tuning.SWIM_LUNG_SUBMERGE:
            if not self.submerged:
                self.swim_force = inv_lerp(0.0, 8.0, abs(c0.vx))
                self.swim_cycle = 0.0
            saver = 1.5 if (self.air_frac < s.drown_threshold and self._swim_up_still()) else 1.0
            prev_frac = self.air_frac
            self.air_in_lungs -= (1.0 / (40.0 * (4.5 if self.lungs_exhausted else 9.0) * saver)) * s.lungs_fac
            self._drown_bubbles(prev_frac)
            if self.drown_burst_air is not None and self.air_in_lungs <= self.drown_burst_air:
                self.drown_burst = True
            # 淹死判定；Rivulet 非溺水免疫但耗气极慢，实际难触发
            if (
                self.air_in_lungs <= 0.0
                and self.head_sub >= 0.999
                and self.hip_sub > tuning.SWIM_DROWN_SUB_HIP
            ):
                self.air_in_lungs = 0.0
                self.stun = max(self.stun, 10)
                self.drown += tuning.SWIM_DROWN_STEP
                if self.drown >= 1.0:
                    self.die()
            self.submerged = True
        else:
            if self.submerged and self.air_frac < s.drown_threshold:
                self.lungs_exhausted = True
            if not self.lungs_exhausted and self.air_in_lungs > 0.9:
                self.air_in_lungs = 1.0
            if self.air_in_lungs < 0.0:
                self.air_in_lungs = 0.0
            rec = tuning.SWIM_LUNG_RECOVER_EXH if self.lungs_exhausted else tuning.SWIM_LUNG_RECOVER
            if self.swim_resting:
                rec *= tuning.SWIM_REST_RECOVER_MULT
            self.air_in_lungs += 1.0 / rec
            if self.air_in_lungs >= 1.0:
                self.air_in_lungs = 1.0
                self.lungs_exhausted = False
                self.drown = 0.0
            self.submerged = False
            self.drown_burst = False
            if self.lungs_exhausted and not (self.swimming and self.swim_mode == "surface"):
                self.swim_cycle += 0.1  # 出水回气时喘粗气的节拍
        if self.air_in_lungs > 1.0:
            self.air_in_lungs = 1.0

    def _emit_bubble(self, x, y, vx, vy):
        if self.bubble_cb is None:
            return
        bf = self.stats.bubble_fac
        if bf < 1.0 and random.random() >= bf:
            return
        self.bubble_cb(x, y, vx, vy)

    def _drown_bubbles(self, prev_frac):
        c0 = self.chunk0
        air = self.air_frac
        vx0 = c0.vx * tuning.BUBBLE_INHERIT_VEL
        vy0 = c0.vy * tuning.BUBBLE_INHERIT_VEL
        if prev_frac >= 2.0 / 3.0 > air:  # 跌破 2/3 首泡
            self._emit_bubble(c0.x, c0.y, vx0, vy0)
        if air < self.stats.drown_threshold:
            if (random.random() > air * 2.0 or self.lungs_exhausted) and random.random() > 0.5:
                ang = random.random() * math.tau
                kick = lerp(tuning.BUBBLE_SPAWN_KICK, 0.0, air)
                self._emit_bubble(c0.x, c0.y, vx0 + math.cos(ang) * kick, vy0 + math.sin(ang) * kick)

    def _step_swim(self):
        if self.stun > 0:
            self.stun -= 1
        if self.water_jump_delay > 0:
            self.water_jump_delay -= 1
        self.bodyMode = "Swimming"
        self.feet_stuck = None
        self.crawl_anchor = None
        self._floor_h = self.H
        ws = self.water_surface
        c0, c1 = self.chunk0, self.chunk1
        self._decide_swim_mode(ws)
        self.swim_pass.step(ws)
        if not self.swim_pass.active:
            sdx, sdy = self._swim_dir()
            if abs(sdx) > tuning.SWIM_TURN_DOT:
                self.swim_input_x = 1 if sdx > 0.0 else -1
            elif (sdx == 0.0 and sdy == 0.0) or self._swim_x_arrived():
                self.swim_input_x = 0
            if self.swim_mode == "deep":
                self._deep_swim()
            else:
                self._surface_swim(ws)
        wf = self._swim_wf
        vmax = tuning.SWIM_VEL_MAX  # 封顶游泳速度，防冲刺穿墙
        for c, wy, wx in ((c0, self._wy0, self._wyx0), (c1, self._wy1, self._wyx1)):
            sp = math.hypot(c.vx, c.vy)
            if sp > vmax:
                k = vmax / sp
                c.vx *= k
                c.vy *= k
            c.update(
                self.W,
                self._floor_h,
                impact=self._impact,
                room_gravity=self.room_gravity,
                water_y=wy if c.x == wx else ws.level_at(c.x),
                buoyancy=self.stats.buoyancy,
                water_friction=wf,
            )
        self.conn_rest = CONN_STAND
        solve_conn(c0, c1, self.conn_rest, ctype="Normal")
        c0.clamp_inside(self.W, self._floor_h)
        c1.clamp_inside(self.W, self._floor_h)
        self._breath_update()
        self._apply_carry()

    def _decide_swim_mode(self, ws):
        if self.no_dive or self.swim_resting:
            self.swim_mode = "surface"
            return
        m = self.swim_pass.mode()
        if m is not None:
            self.swim_mode = m
            return
        c0 = self.chunk0
        wl0 = self._wy0 if c0.x == self._wyx0 else ws.level_at(c0.x)
        diving = self.swim_target is not None and (self.swim_target[1] - c0.y) > 5.0
        prev_surface = self.swim_mode == "surface"
        below_full = c0.y > wl0 + tuning.SWIM_DEEP_FULL
        depth = tuning.SWIM_DEEP_DIVE_DEPTH if diving else tuning.SWIM_DEEP_DEPTH
        below_entry = c0.y > wl0 + depth
        hip_req = -1.0 if diving else tuning.SWIM_DEEP_HIP_SUB
        if (not prev_surface or diving or below_full) and below_entry and self.hip_sub > hip_req:
            self.swim_mode = "deep"
        else:
            self.swim_mode = "surface"

    def _deep_swim(self):
        s = self.stats
        c0, c1 = self.chunk0, self.chunk1
        self.standing = False
        axx, axy = c0.x - c1.x, c0.y - c1.y
        axl = math.hypot(axx, axy) or 1.0
        ax_u, ay_u = axx / axl, axy / axl
        axis_align = (_dot_norm(c0.vx, c0.vy, ax_u, ay_u) + _dot_norm(c1.vx, c1.vy, ax_u, ay_u)) / 2.0
        swim_x, swim_y = self._swim_dir()
        have_dir = swim_x != 0.0 or swim_y != 0.0
        boost_ready = self.submerged if self.aquatic else (self.air_frac > tuning.SWIM_BOOST_MIN_AIR)
        dist = math.hypot(self.swim_target[0] - c0.x, self.swim_target[1] - c0.y) if self.swim_target else 0.0
        if have_dir and self.water_jump_delay == 0 and boost_ready and dist > self.swim_boost_dist:
            self.swim_cycle = 2.7
            if self.aquatic:
                f = 300.0 if (-ay_u) > 0.5 else 50.0  # y↓ 取负为上
                c0.vx += ax_u * f
                c0.vy += ay_u * f
            else:
                c0.vx += ax_u * s.swim_boost_force
                c0.vy += ay_u * s.swim_boost_force
            self.air_in_lungs -= s.swim_boost_cost
            self.water_jump_delay = s.swim_boost_cd
            self._emit_bubble(
                c0.x, c0.y, c0.vx * tuning.BUBBLE_INHERIT_VEL, c0.vy * tuning.BUBBLE_INHERIT_VEL
            )
        self.swim_cycle += 0.01
        if have_dir:
            axis_turn = _angle_deg(c0.last_x - c1.last_x, c0.last_y - c1.last_y, axx, axy)
            force_target = 0.2 + inv_lerp(0.0, 12.0, axis_turn) * 0.8
            if force_target > self.swim_force:
                self.swim_force = lerp(self.swim_force, force_target, 0.7)
            else:
                self.swim_force = lerp(self.swim_force, force_target, 0.05)
            self.swim_cycle += lerp(self.swim_force, 1.0, 0.5) / 10.0
            if 1.0 / 6.0 < self.air_frac < 0.5:
                self.swim_cycle += 0.05
            if c0.cx or c0.cy:
                self.swim_force *= 0.5
            stroke_kick = 0.7 * lerp(self.swim_force * s.swim_force_fac, 1.0, 0.5) * self.head_sub
            if self.swim_cycle > 4.0:
                self.swim_cycle = 0.0
            elif self.swim_cycle > 3.0:
                c0.vx += ax_u * stroke_kick
                c0.vy += ay_u * stroke_kick
            aim_x, aim_y = swim_x, swim_y  # 缺氧时泳向逐渐转向正上方
            if self.air_frac < s.drown_threshold:
                t = inv_lerp(s.drown_threshold, 0.0, self.air_frac)
                aim_x, aim_y = _slerp2(aim_x, aim_y, 0.0, -1.0, t)
            k = 0.5 * self.swim_force * lerp(axis_align, 1.0, 0.5) * self.head_sub * s.swim_force_fac
            c0.vx += aim_x * k
            c0.vy += aim_y * k
            c1.vx -= aim_x * (0.1 * self.head_sub)
            c1.vy -= aim_y * (0.1 * self.head_sub)
            k2 = 0.4 * self.swim_force * axis_align * self.head_sub * s.swim_force_fac
            c0.vx += ax_u * k2
            c0.vy += ay_u * k2
            sp = math.hypot(c0.vx, c0.vy)
            if sp < 3.0:
                add = 0.2 * inv_lerp(3.0, 1.5, sp)
                c0.vx += aim_x * add
                c0.vy += aim_y * add
                sub = 0.1 * inv_lerp(3.0, 1.5, sp)
                c1.vx -= aim_x * sub
                c1.vy -= aim_y * sub
        if self.aquatic and self.water_jump_delay >= 5:
            self._swim_wf = 0.99
        else:
            self._swim_wf = lerp(0.92, 0.96, axis_align)
        if self.swim_cycle >= 6.0:
            self.swim_cycle %= 6.0

    def _surface_swim(self, ws):
        """浮泳推进。"""
        s = self.stats
        c0, c1 = self.chunk0, self.chunk1
        self.canJump = 0
        self.swim_cycle += 0.006 if self.swim_resting else 0.025
        if self.aquatic and self.water_jump_delay >= 5:
            self._swim_wf = 0.999
        else:
            self._swim_wf = 0.96
        self.swim_force *= 0.5
        swim_x, swim_y = self._swim_dir()
        have_dir = swim_x != 0.0 or swim_y != 0.0
        iy = 0  # 竖直意图：+1 上 / -1 下
        if have_dir:
            if swim_y > 0.5:
                iy = -1  # 目标在下(y↓)=潜
            elif swim_y < -0.5:
                iy = 1  # 目标在上=浮
        if iy == -1 and self.no_dive:
            iy = 1
        wl0 = self._wy0 if c0.x == self._wyx0 else ws.level_at(c0.x)
        target_head_y = wl0 - tuning.SWIM_SURFACE_HEAD_ABOVE
        if iy > -1 and (-3.0 < c0.vy < 5.0) and self.water_jump_delay == 0:
            c0.vy *= 0.8
            c1.vy *= 0.8
            c0.vy += clampf((target_head_y - c0.y) * 0.1, -1.5, 0.5)
            c1.vy += 0.5
        elif iy == -1:
            c0.vy += 0.2
            c1.vy -= 0.1
        elif iy == 1:
            c0.vy -= 0.5
        ix = 1 if swim_x > 0.0 else (-1 if swim_x < 0.0 else 0)
        if self._swim_x_arrived():
            ix = 0
        if ix != 0:
            self._swim_accel_h(c0, ix, s.swim_surface_speed)
            hip_drag_mult = 1.5 if self.aquatic else 1.0
            c1.vx -= ix * 0.2 * hip_drag_mult
            self.swim_cycle += 1.0 / 30.0
        else:
            c0.vx *= tuning.SWIM_STOP_DAMP
            c1.vx *= tuning.SWIM_STOP_DAMP
        if self.swim_cycle >= 3.0:
            self.swim_cycle %= 3.0

    def _swim_accel_h(self, c, dir_sign, dyn):
        if dir_sign < 0:
            step = H_ACCEL
            if c.vx - step < -dyn:
                step = dyn + c.vx
            if step > 0:
                c.vx -= step
        elif dir_sign > 0:
            step = H_ACCEL
            if c.vx + step > dyn:
                step = dyn - c.vx
            if step > 0:
                c.vx += step

    def _step_zerog(self):
        if self.stun > 0:
            self.stun -= 1
        self.bodyMode = "ZeroG"
        self.feet_stuck = None
        self.crawl_anchor = None
        if self._ctrl_on and self.stun <= 0:
            from ..control.moves_zerog import ctrl_zerog_update

            ctrl_zerog_update(self)
        rg = self.room_gravity
        c0, c1 = self.chunk0, self.chunk1
        grabbing = self._zerog_pole_grab()
        for c in (c0, c1):
            c.update(
                self.W, self.H, gravity=cp.GRAVITY * rg, air_friction=cp.AIR_FRICTION, impact=self._impact
            )
        if not grabbing:
            # 贴着窗边时允许蹬墙，记下离墙的法向
            nx, ny = self._zerog_wall_normal()
            if nx or ny:
                self.canJump = ZEROG_CANJUMP
                self._zerog_wall = (nx, ny)
            if self.canJump > 0:
                if self._zerog_kick:
                    self._do_zerog_kick()
                self.canJump -= 1
        self._zerog_kick = False
        self.conn_rest = CONN_STAND
        solve_conn(c0, c1, self.conn_rest, ctype="Normal")
        c0.clamp_inside(self.W, self.H)
        c1.clamp_inside(self.W, self.H)
        self._breath_update()
        self._apply_carry()

    def _zerog_pole_grab(self) -> bool:
        """失重时抓着杆推一 tick；头离开杆那一格就松手。"""
        pole = self.zerog_pole
        if pole is None:
            self._zerog_release_pose()
            return False
        c0, c1 = self.chunk0, self.chunk1
        if c0.pinned or c1.pinned or not _in_beam(pole, c0.x, c0.y):
            self.zerog_pole = None
            self._zerog_release_pose()
            return False
        vertical = _is_vertical(pole)
        fx, fy = self.zerog_pole_intent
        ix = (fx > 0.3) - (fx < -0.3)
        iy = (fy < -0.3) - (fy > 0.3)  # 向上为正
        self.animation = "ZeroGPoleGrab"
        self.standing = vertical
        k = lerp(0.7, 0.3, inv_lerp(2.0, 5.0, math.hypot(c0.vx, c0.vy)))
        c0.vx *= k
        c0.vy *= k
        if ix:
            self.zerog_grab_dir[0] = ix
        if iy:
            self.zerog_grab_dir[1] = iy
        dx, dy = self.zerog_grab_dir
        climb = ZEROG_POLE_CLIMB * self.stats.pole_fac
        off_end = False
        if vertical:
            px = pole.ax
            c0.vx += (px + ZEROG_POLE_SIDE * dx - c0.x) * ZEROG_POLE_PULL
            if iy:
                if _in_beam(pole, c0.x, c0.y - iy * 10.0):
                    c0.vy -= iy * climb
                    self._zerog_frame_step()
                else:
                    off_end = True
            if not off_end and _in_beam(pole, c1.x, c1.y):
                c1.vx *= ZEROG_POLE_HIP
                c1.vy *= ZEROG_POLE_HIP
                c1.vx += (px - ZEROG_POLE_SIDE * dx - c1.x) * ZEROG_POLE_PULL
            self.pole_x, self.pole_y = px, c0.y
        else:
            py = pole.ay
            c0.vy += (py - ZEROG_POLE_SIDE * dy - c0.y) * ZEROG_POLE_PULL
            if ix:
                if _in_beam(pole, c0.x + ix * 10.0, c0.y):
                    c0.vx += ix * climb
                    self._zerog_frame_step()
                else:
                    off_end = True
            if not off_end and _in_beam(pole, c1.x, c1.y):
                c1.vx *= ZEROG_POLE_HIP
                c1.vy *= ZEROG_POLE_HIP
                c1.vy += (py + ZEROG_POLE_SIDE * dy - c1.y) * ZEROG_POLE_PULL
            self.pole_x, self.pole_y = c0.x, py
        return True

    def zerog_pole_kick(self, ux, uy):
        """失重时朝 (ux, uy) 从杆上蹬开；零向量只松手。"""
        for c, cap in ((self.chunk0, ZEROG_POLE_KICK_HEAD), (self.chunk1, ZEROG_POLE_KICK_HIP)):
            vx, vy = c.vx + ux * cap, c.vy + uy * cap
            m = math.hypot(vx, vy)
            if m > cap:
                vx, vy = vx / m * cap, vy / m * cap
            c.vx, c.vy = vx, vy
        self.zerog_pole = None
        self.animation = None
        self.standing = False

    def _zerog_frame_step(self):
        self.pole_frame += 1
        if self.pole_frame > 20:
            self.pole_frame = 0

    def _zerog_release_pose(self):
        self.animation = None
        self.standing = True

    def _zerog_wall_normal(self):
        for c in (self.chunk0, self.chunk1):
            if c.cx or c.cy:
                return (-c.cx, -c.cy)
        return (0, 0)

    def _do_zerog_kick(self):
        nx, ny = self._zerog_wall
        d = math.hypot(nx, ny) or 1.0
        ax, ay = nx / d, ny / d
        ix, iy = self._zerog_kick_dir
        kick_x = ix + (ax - ix) * ZEROG_KICK_LERP
        kick_y = iy + (ay - iy) * ZEROG_KICK_LERP
        c0, c1 = self.chunk0, self.chunk1
        hx, hy = c0.x - c1.x, c0.y - c1.y
        hd = math.hypot(hx, hy) or 1.0
        bx, by = hx / hd, hy / hd
        kick_x = kick_x + (bx - kick_x) * ZEROG_KICK_BODYAXIS
        kick_y = kick_y + (by - kick_y) * ZEROG_KICK_BODYAXIS
        c0.vx += kick_x * ZEROG_KICK
        c0.vy += kick_y * ZEROG_KICK
        c1.vx += kick_x * ZEROG_HIP_KICK
        c1.vy += kick_y * ZEROG_HIP_KICK
        _clamp_vel_mag(c0, ZEROG_HEAD_MAX)
        _clamp_vel_mag(c1, ZEROG_HIP_MAX)
        c0.vx += ax
        c0.vy += ay
        self.canJump = 0

    def zerog_swim(self, ux, uy, on_wall):
        c0, c1 = self.chunk0, self.chunk1
        if c0.vx * ux + c0.vy * uy >= ZEROG_SWIM_MAX:
            return
        push = ZEROG_SWIM_WALL if on_wall else ZEROG_SWIM_PUSH
        c0.vx += ux * push
        c0.vy += uy * push
        c1.vx -= ux * ZEROG_SWIM_HIP
        c1.vy -= uy * ZEROG_SWIM_HIP

    def request_zerog_kick(self, ux: float = 0.0, uy: float = 0.0):
        self._zerog_kick = True
        self._zerog_kick_dir = (ux, uy)

    def _settle_ragdoll_integrate(self):
        self.hip_sink += (1.0 - self.hip_sink) * HIP_SINK_EASE
        self._floor_h = self.H + self.hip_sink * self.crawl_sink
        ws = self.water_surface
        for c in (self.chunk0, self.chunk1):
            wy = ws.level_at(c.x) if ws is not None else None
            c.update(
                self.W,
                self._floor_h,
                air_friction=cp.AIR_FRICTION,
                impact=self._impact,
                room_gravity=self.room_gravity,
                water_y=wy,
                buoyancy=self.stats.buoyancy,
                water_friction=WATER_FRICTION,
            )
            if c.on_floor:
                c.vx *= 0.7

    # ── 撞击伤害 ──
    def _held_chunk(self):
        c0, c1 = self.chunk0, self.chunk1
        return c0 if c0.pinned else (c1 if c1.pinned else None)

    def _drag_impacts(self):
        ch = self._held_chunk()
        k = 1.0
        if ch is not None and self.drag_speed > 0.0:
            sp = math.hypot(ch.vx, ch.vy)
            if sp > 0.5:
                k = clampf(self.drag_speed / sp, 1.0, tuning.DRAG_IMPACT_MAX_SCALE)
        # 只有被抓的那节被限过速，另一节按真实速度算
        self.chunk0.impact_scale = self.chunk1.impact_scale = 1.0
        if ch is not None:
            ch.impact_scale = k
        if ch is None or self.dead:
            self._edge_contact = (False, False, False)
            self._spin_reset()
            return
        # 被抓的那节不参与墙体碰撞，撞到屏幕边要单独判
        r = ch.rad
        now = (
            ch.y + r >= self._floor_h - 0.5 and ch.vy > 0.0,
            ch.x + r >= self.W - 0.5 and ch.vx > 0.0,
            ch.x - r <= 0.5 and ch.vx < 0.0,
        )
        for hit, was, d in zip(now, self._edge_contact, ((0, 1), (1, 0), (-1, 0))):
            if hit and not was:
                cp.fire_impact(ch, d, math.hypot(ch.vx, ch.vy), True, self._impact)
        self._edge_contact = now
        self._spin_check()

    def _spin_reset(self):
        self._spin_prev = None
        self._spin_rate = 0.0
        self._spin_hold = 0
        self._spin_done = False

    def _spin_check(self):
        c0, c1 = self.chunk0, self.chunk1
        ang = math.atan2(c0.y - c1.y, c0.x - c1.x)
        if self._spin_prev is None:
            self._spin_prev = ang
            return
        d = (ang - self._spin_prev + math.pi) % (2.0 * math.pi) - math.pi
        self._spin_prev = ang
        self._spin_rate += (math.degrees(d) * TICK_RATE - self._spin_rate) * 0.25
        rate = abs(self._spin_rate)
        if rate >= tuning.SPIN_STUN_DEG_PER_S:
            self._spin_hold += 1
            if self._spin_hold >= tuning.SPIN_STUN_TICKS:
                self._spin_hold = 0
                self._spin_rate = 0.0
                if self.stun <= 0 and not self._spin_done and self.on_hurt is not None:
                    self._spin_done = True
                    self.on_hurt("stun", tuning.SPIN_STUN_DURATION)
        else:
            self._spin_hold = 0
            if rate < tuning.SPIN_STUN_DEG_PER_S * 0.5:
                self._spin_done = False

    def _impact(self, chunk, direction, speed, strength, ix, iy):
        if self.impact_cb is not None:
            self.impact_cb(chunk, direction, speed, strength, ix, iy)
        s = self.stats
        if self.dead or self.stun > 0 or self.on_hurt is None or speed <= s.fall_stun_speed:
            return
        if speed > s.fall_death_speed and direction[1] > 0 and self._held_chunk() is None:
            self.on_hurt("die", 0)
            return
        t = inv_lerp(s.fall_stun_speed, s.fall_death_speed, speed) ** 2.5
        self.on_hurt("stun", int(lerp(tuning.FALL_STUN_MIN, tuning.FALL_STUN_MAX, t)))

    def _step_dead(self):
        self._settle_ragdoll_integrate()
        solve_conn(self.chunk0, self.chunk1, self.conn_rest, ctype="Normal")
        self.chunk0.clamp_inside(self.W, self._floor_h)
        self.chunk1.clamp_inside(self.W, self._floor_h)

    def _step_stunned(self):
        self.bodyMode = "Stunned"
        self.feet_stuck = None
        self.crawl_anchor = None
        self._settle_ragdoll_integrate()
        self.conn_rest = CONN_STAND if self.standing else CONN_CRAWL
        solve_conn(self.chunk0, self.chunk1, self.conn_rest, ctype="Normal")
        self.chunk0.clamp_inside(self.W, self._floor_h)
        self.chunk1.clamp_inside(self.W, self._floor_h)
        self._gait_update()
        self._breath_update()

    def _movement_update(self):
        if self._ctrl_on:
            # 手动控制走操控逻辑；延迟导入避免循环引用
            from ..control.moves import ctrl_movement_update

            return ctrl_movement_update(self)
        c0, c1 = self.chunk0, self.chunk1
        if self.animation == "Flip" and self._flip_spin != 0:
            # 自动翻跳的力矩，碰到东西就停
            from ..control.vmath import perpendicular

            vx, vy = perpendicular(c1.x, c1.y, c0.x, c0.y)
            k = self._flip_spin * (0.38 * self._flip_mult)
            c0.vx += vx * k
            c0.vy += vy * k
            c1.vx -= vx * k
            c1.vy -= vy * k
            self.standing = False
            if c0.cx != 0 or c0.cy != 0 or c1.cx != 0 or c1.cy != 0:
                self.animation = None
                self.standing = c0.y < c1.y
                self._flip_spin = 0
        move_x = 0
        self.holding = False
        if self.walk_target_x is not None:
            dx = self.target_x - c1.x
            if abs(dx) > WALK_STOP_EPS:
                move_x = 1 if dx > 0 else -1
                self.facing = move_x
                if move_x == self.lane_hold:
                    move_x = 0  # 跟在前面的猫后面、或在等对方过去
                    self.holding = True
            elif self.on_floor() or self.on_support():  # 空中经过不算到，落地偏了接着走
                self.walk_target_x = self.target_x = self.stride = None
                self.walk_result = ARRIVED
        else:
            move_x = self.move_dir
            if move_x > 0:
                self.facing = 1
            elif move_x < 0:
                self.facing = -1
        if self.stride is not None and self.walk_target_x is not None and not self._ctrl_on:
            m = self.stride.move(self)
            if m is not None:
                move_x = m
                if m:
                    self.facing = m
            if self.stride.done:
                self.stride = None

        target_sink = 1.0 if not self.standing else 0.0
        self.hip_sink += (target_sink - self.hip_sink) * HIP_SINK_EASE
        self._floor_h = self.H + self.hip_sink * self.crawl_sink

        if self.standing or move_x != 0:
            self.crawl_anchor = None
            self.crawl_pose = max(0.0, self.crawl_pose - 0.08)

        flag = move_x == 0 and self.standing and c1.on_floor
        if self.feet_stuck is not None and not flag:
            self.feet_stuck = None
        elif self.feet_stuck is None and flag:
            self.feet_stuck = [c1.x, self._floor_h - c1.rad]
        if self.feet_stuck is not None:
            self.feet_stuck[0] += (c1.x - self.feet_stuck[0]) * FEET_EASE
            self.feet_stuck[1] = self._floor_h - c1.rad
            if not c1.pinned:
                c1.x = self.feet_stuck[0]
                c1.y = self.feet_stuck[1]

        on_ground = self.on_floor()
        if not on_ground:
            self.bodyMode = "Default"
        elif self.standing:
            self.bodyMode = "Stand"
        else:
            self.bodyMode = "Crawl"

        dyn0 = RUN_UPPER * self.stats.runspeed_fac * self.slow_fac(0.5)  # 顶速×种族因子，不乘加速度
        dyn1 = RUN_LOWER * self.stats.runspeed_fac * self.slow_fac(0.5)
        accel = H_ACCEL * self.slow_fac(0.4)
        if self.bodyMode == "Stand":
            c0.vy += STAND_HEAD
            c1.vy += STAND_FEET
        elif self.bodyMode == "Default" and self.standing:
            c0.vy += DEF_STAND_HEAD
            c1.vy += DEF_STAND_FEET
        elif self.bodyMode == "Crawl":
            dyn0 = dyn1 = CRAWL_SPEED  # 平地趴行恒速，不乘隧道爬速因子
            if move_x == 0 and c1.on_floor and not c0.pinned and not c1.pinned and self._jump_pending is None:
                self._crawl_pose()

        stop_x = self.target_x if self.stride is None else self.stride.stop_x
        if self.walk_target_x is not None and stop_x is not None:
            dist = abs(stop_x - c1.x)
            if dist < WALK_SLOW_DIST:
                scale = max(CRAWL_SPEED / max(dyn1, 1.0), dist / WALK_SLOW_DIST)
                dyn0 *= scale
                dyn1 *= scale

        if self.walk_speed_target is not None:
            dyn0 = min(dyn0, self.walk_speed_target)
            dyn1 = min(dyn1, self.walk_speed_target)
        grounded = c0.on_floor or c1.on_floor
        for c, dyn in ((c0, dyn0), (c1, dyn1)):
            if c.pinned:
                continue
            if move_x < 0:
                step = accel
                if c.vx - step < -dyn:
                    step = dyn + c.vx
                if step > 0:
                    c.vx -= step
            elif move_x > 0:
                step = accel
                if c.vx + step > dyn:
                    step = dyn - c.vx
                if step > 0:
                    c.vx += step
            if grounded:
                target = max(-dyn, min(dyn, c.vx)) if move_x != 0 else 0.0
                c.vx += (target - c.vx) * SKID_DAMP
        # 踩着别的猫也按平地跳，和规划时的弧一致
        if self._jump_pending is not None and (on_ground or self.on_support()):
            self._do_jump(self._jump_pending, move_x, self._jump_hold)
            self._jump_pending = None
            self._jump_hold = None

        if on_ground:
            pass
        elif self.jump_boost > 0:
            if self._jump_hold_left is not None and self._jump_hold_left <= 0:
                self.jump_boost = 0.0
            else:
                if self._jump_hold_left is not None:
                    self._jump_hold_left -= 1
                self.jump_boost -= JUMPBOOST_DECAY
                kick = (self.jump_boost + 1) * JUMPBOOST_GAIN
                c0.vy -= kick
                c1.vy -= kick
                if self.jump_boost < 0:
                    self.jump_boost = 0.0

        if self.walk_min is not None and move_x != 0:
            for c in (c0, c1):
                if c.x < self.walk_min:
                    c.x = self.walk_min
                elif c.x > self.walk_max:
                    c.x = self.walk_max

    def _pole_update(self):
        self.bodyMode = "ClimbingOnBeam"
        self.feet_stuck = None
        self.crawl_anchor = None
        self._floor_h = self.H

    def _suspended_update(self):
        if not self.on_floor():
            self.bodyMode = "Default"
            self.feet_stuck = None
            self.crawl_anchor = None
        elif self.standing:
            self.bodyMode = "Stand"
        else:
            self.bodyMode = "Crawl"

    def _do_jump(self, kind, move_x, hold_ticks=None):
        c0, c1 = self.chunk0, self.chunk1
        self.feet_stuck = None
        self.crawl_anchor = None
        self.crawl_pose = 0.0
        self._jump_hold_left = hold_ticks
        self.aerobic_increase(self.stats.jump_aerobic)
        if kind == "wake":
            c0.y -= 6.0
            c0.vy += JUMP_STAND_FEET * 1.5
            c1.vy += JUMP_STAND_HEAD
            self.jump_boost = 6
        elif kind == "protest":
            c0.vy = PROTEST_HEAD
            c1.vy = PROTEST_FEET
            self.jump_boost = PROTEST_BOOST
        else:
            c0.vy = self.stats.jump_head
            c1.vy = self.stats.jump_feet
            self.jump_boost = self.stats.jump_boost
            # 横速由此前加速循环保留

    def _crawl_pose(self):
        c0, c1 = self.chunk0, self.chunk1
        if self.crawl_anchor is None:
            self.crawl_anchor = c1.x
        elif self.sleeping and self._prev_end_x1 is not None:
            # 被别的猫挤开就睡在新位置，不和碰撞来回拉扯
            self.crawl_anchor += c1.x - self._prev_end_x1
        # 躺平时头要伸出去，贴墙时臀离墙留出整个身长
        lo, hi = c1.rad, self.W - c1.rad
        reach = self.conn_rest + c0.rad
        if self.facing < 0:
            lo = max(lo, reach)
        elif self.facing > 0:
            hi = min(hi, self.W - reach)
        self.crawl_anchor = clampf(self.crawl_anchor, lo, hi)
        h = self.lie_haste
        self.crawl_pose = min(1.0, self.crawl_pose + 0.025 * h)
        c1.x += (self.crawl_anchor - c1.x) * (tuning.SLEEP_ANCHOR_EASE if self.sleeping else 0.06 * h)
        c1.y = self._floor_h - c1.rad
        c1.vx *= 0.75
        c1.vy = 0.0
        low_y = self._floor_h - c0.rad
        high_y = c1.y - max(self.conn_rest, 8.0)
        target_y = high_y + (low_y - high_y) * self.crawl_pose
        dy = max(1.0, abs(target_y - c1.y))
        ahead = math.sqrt(max(0.0, self.conn_rest * self.conn_rest - dy * dy)) * self.facing
        tx = c1.x + ahead
        ty = target_y
        if self.sleeping:
            k = tuning.SLEEP_ANCHOR_EASE
        else:
            k = (0.045 + 0.035 * self.crawl_pose) * h
        c0.x += (tx - c0.x) * k
        c0.y += (ty - c0.y) * k
        c0.vx *= 0.82
        c0.vy *= 0.82
        if self.sleeping:
            # 睡着时位置交给锚点，碰撞推出的速度不留
            c1.vx = 0.0
            c0.vx *= 0.3
            c0.vy *= 0.3

    def aerobic_increase(self, f):
        self.aerobic = min(1.0, self.aerobic + f / 9.0)

    def _moving_input(self):
        """本 tick 有没有移动意图（决定气喘回落快慢）。"""
        if self._ctrl_on and self._ctrl_input is not None:
            i = self._ctrl_input[0]
            return i.x != 0 or i.y != 0
        return self.move_dir != 0 or self.is_moving() or self.swim_input_x != 0

    def _aerobic_update(self):
        if self.slow_stun > 0:
            self.slow_stun -= 1
        a = self.aerobic
        if self.lungs_exhausted and not self.exhausted:
            a = 1.0
        else:
            if not self.exhausted:
                moving, still = 1100.0, 400.0
            elif self.bodyMode == "Crawl":
                moving, still = 400.0, 125.0
            else:
                moving, still = 800.0, 200.0
            t = moving if self._moving_input() else still
            a -= 1.0 / (t * (1.0 + 3.0 * inv_lerp(0.9, 1.0, a)))
            a = max(1.0 - self.air_in_lungs, a, 0.0)
        self.aerobic = a
        if self.exhausted:
            self.slow_stun = max(self.slow_stun, int(lerp(6.0, 0.0, inv_lerp(0.7, 0.4, a))))

    def slow_fac(self, lo):
        """迟缓时的速度倍率：slow_stun 0→10 映射到 1→lo。"""
        return lerp(1.0, lo, self.slow_stun / 10.0)

    def _breath_update(self):
        self.last_breath = self.breath
        if self.sleeping:
            period = 80.0
        else:
            period = lerp(60.0, 15.0, self.aerobic**1.5)
        self.breath += 1.0 / period

    def breath_phase(self, ts=1.0):
        b = self.last_breath + (self.breath - self.last_breath) * ts
        return 0.5 + 0.5 * math.sin(b * 2 * math.pi)

    def energy_change(self, d):
        self.energy = max(0.0, min(1.0, self.energy + d))

    def temper_shift(self, d):
        d *= self.temper_fac[0] if d > 0.0 else self.temper_fac[1]
        self.temper = max(-1.0, min(1.0, self.temper + d))

    def _temper_update(self):
        if self.temper > 0.0:
            self.temper = max(0.0, self.temper - tuning.TEMPER_DECAY)
        elif self.temper < 0.0:
            self.temper = min(0.0, self.temper + tuning.TEMPER_DECAY)

    def food_eat(self, n):
        """整格增减饱食；吃满时清掉攒着的 1/4。"""
        self.food = max(0, min(self.food_max, self.food + int(n)))
        if self.food >= self.food_max:
            self.food_q = 0

    def feed(self, quarters):
        """吃下 quarters 个 1/4 格：整格直接加，余下逐个攒，攒满 4 个进一格；好感、体力一起结算。"""
        self.temper_shift(tuning.TEMPER_FEED)
        whole, rest = divmod(int(quarters), 4)
        self.food_eat(whole)
        for _ in range(rest):
            if self.food < self.food_max:
                self.food_q += 1
                if self.food_q >= 4:
                    self.food_q -= 4
                    self.food_eat(1)
        self.energy_change(tuning.EN_EAT_RESTORE)

    def karma_gain(self):
        self.karma = min(self.karma_max, self.karma + 1)

    def karma_drop(self):
        self.karma = max(0, self.karma - 1)

    def karma_bottomed(self) -> bool:
        """业力上限为 0：死亡不扣业力、不真死。"""
        return self.karma_max <= 0

    def _gait_update(self):
        moving = self.is_moving() or (self.move_dir != 0)
        fy = self.H
        lead = (self.step_predict * self.step_threshold) * self.facing if moving else 0.0
        hx = self.chunk1.x
        feet = (
            (self.lfoot, self._lstep, hx - self.stance + lead, self._rstep),
            (self.rfoot, self._rstep, hx + self.stance + lead, self._lstep),
        )
        for foot, st, should_x, other in feet:
            if not st["on"]:
                if abs(foot[0] - should_x) > self.step_threshold and not other["on"]:
                    st["on"] = True
                    if moving and self.bodyMode == "Stand" and self.aerobic < 0.7:
                        self.aerobic_increase(0.05)
                    st["start"] = foot[0]
                    st["goal"] = should_x
                    st["lerp"] = 0.0
            if st["on"]:
                st["lerp"] += self.step_speed
                if st["lerp"] >= 1.0:
                    st["lerp"] = 1.0
                    st["on"] = False
                t = st["lerp"]
                tt = t * t * (3.0 - 2.0 * t)
                foot[0] = st["start"] + (st["goal"] - st["start"]) * tt
                foot[1] = fy - self.foot_lift * 4.0 * t * (1.0 - t)
            else:
                foot[1] = fy
        target_turn = 1.0 if moving else 0.0
        self.turn += (target_turn - self.turn) * self.turn_rate
        if moving:
            if self._stride_prev_x is None:
                self._stride_prev_x = hx
            self.stride_phase = (self.stride_phase + abs(hx - self._stride_prev_x) * self.stride_rate) % 1.0
            self._stride_prev_x = hx
            self.walk_bob_y = (
                math.sin(self.stride_phase * 2 * math.pi * self.walk_bob_freq) * self.walk_bob_amp
            )
        else:
            self._stride_prev_x = None
            self.walk_bob_y *= 0.85

    def _carry_pos(self, side):
        """手持静止姿态点：上身侧下方，随脊柱转。"""
        c0 = self.chunk0
        sgn = -1.0 if side == "l" else 1.0
        ang = _ang_from_up(c0.x - self.chunk1.x, c0.y - self.chunk1.y)
        ox, oy = _rot(sgn * CARRY_OFF_X, CARRY_OFF_Y, ang)
        return c0.x + ox, c0.y + oy

    def reach_for(self, obj, side):
        """用空手伸向 obj；双手都满时用 side。"""
        side = self.free_hand(side) or side
        self.arm_aim[side] = (obj.x, obj.y)
        self.arm_aim[other_hand(side)] = None

    def held(self, side):
        return self.grasps[side]

    def hand_of(self, obj):
        """obj 在哪只手里；不在手里返回 None。"""
        if obj is None:
            return None
        for side in HANDS:
            if self.grasps[side] is obj:
                return side
        return None

    def usable_hands(self):
        """能拿东西的手，按主手顺序。"""
        return HANDS[: self.stats.hands]

    def free_hand(self, prefer=None):
        """空手：优先 prefer，否则按主手顺序；能用的手都满、或拉着成年猫时返回 None。"""
        if self.hand_load > 0.0:
            return None
        hands = self.usable_hands()
        if prefer in hands and self.grasps[prefer] is None and prefer != self.hand_cat:
            return prefer
        for side in hands:
            if self.grasps[side] is None and side != self.hand_cat:
                return side
        return None

    def held_items(self):
        return [o for o in (self.grasps["l"], self.grasps["r"]) if o is not None]

    def can_hold(self, obj):
        """再拿 obj 合不合规矩：大件同时只能拿一件，能双持的猫不限（不管有没有空手）。"""
        if not getattr(obj, "bulky", False) or self.stats.dual_wield:
            return True
        return not any(getattr(o, "bulky", False) for o in self.held_items() if o is not obj)

    def holding_any(self):
        return self.grasps["l"] is not None or self.grasps["r"] is not None

    def held_throwable(self):
        """按主手顺序第一个可投掷的手持物。"""
        for side in HANDS:
            o = self.grasps[side]
            if o is not None and getattr(o, "throwable", False):
                return o
        return None

    def grab(self, obj, side):
        self.grasps[side] = obj
        obj.state = "carried"
        obj.held_by_hand = side
        self.eat_raise[side] = 0.0
        self.arm_aim[side] = None
        stalk = getattr(obj, "stalk", None)
        if stalk is not None:
            stalk.release_counter = 2

    def release(self, obj, to_free=False):
        """松开 obj；to_free 时把它放回自由态。"""
        side = self.hand_of(obj)
        if side is None:
            return
        self.grasps[side] = None
        obj.held_by_hand = None
        if to_free:
            obj.state = "free"
        self.eat_raise[side] = 0.0
        self.arm_aim[side] = None

    def set_eat_raise(self, obj, k):
        """拿 obj 的手进入啃食姿态的程度，k∈[0,1]。"""
        side = self.hand_of(obj)
        if side is not None:
            self.eat_raise[side] = k

    def can_switch_hands(self):
        return self.switch_hands == 0.0 and self.stats.hands > 1 and self.holding_any()

    def start_switch_hands(self):
        """开始换手：两手收向中线，过半时交换手持物。"""
        if self.can_switch_hands():
            self.switch_hands = 0.01

    def _switch_update(self):
        p = self.switch_hands
        if p <= 0.0:
            return
        q = p + SWITCH_HANDS_RATE
        if p < 0.5 <= q:
            g = self.grasps
            g["l"], g["r"] = g["r"], g["l"]
            e = self.eat_raise
            e["l"], e["r"] = e["r"], e["l"]
            for side in HANDS:
                if g[side] is not None:
                    g[side].held_by_hand = side
        self.switch_hands = 0.0 if q >= 1.0 else q

    def bite(self, obj):
        obj.bites -= 1
        return obj.bites < EAT_BITES_MIN

    def consume(self, obj, quarters):
        """啃一口，吃完则按 quarters 个 1/4 格结算并释放，返回是否吃完。尸体每口都结算，啃完留在手里。"""
        if self.hand_of(obj) is None:
            return False
        if getattr(obj, "carcass", False):
            if obj.meat > 0:
                obj.meat -= 1
                self.feed(quarters)
            return obj.meat <= 0
        if self.bite(obj):
            obj.state = "eaten"
            self.feed(quarters)
            self.release(obj)
            return True
        return False

    def drop_carried(self):
        for obj in self.held_items():
            if getattr(obj, "stalk", None) is not None:
                obj.stalk = None
            self.release(obj, to_free=True)

    def throw_origin(self, dir_x, dir_y=0):
        """出手点：上身朝投掷方向偏出，与哪只手拿着无关。"""
        c0 = self.chunk0
        return c0.x + float(dir_x) * 10.0, c0.y + float(dir_y) * 10.0 - 4.0

    def throw(
        self, obj, dir_x, base_speed, up=3.0, recoil=1.0, vy=None, dir_y=0, aim=None, spare_cursor=False
    ):
        """抛出手中 obj，返回 obj 或 None；dir_y≠0 时竖直扔（y↓），vy 覆盖竖直初速，aim 交给物体出手，spare_cursor 时不打光标。"""
        if self.hand_of(obj) is None:
            return None
        c0, c1 = self.chunk0, self.chunk1
        obj.last_x, obj.last_y = obj.x, obj.y
        force = 0.5 if self.exhausted else 1.0
        base_speed *= force
        if dir_y:
            dir_x = 0
        obj.set_rotation_to_grabber(c0.x, c0.y)
        obj.x, obj.y = self.throw_origin(dir_x, dir_y)
        if dir_y:
            obj.vx = c0.vx * 0.5
            obj.vy = float(dir_y) * base_speed
        else:
            obj.vx = c0.vx * 0.2 + float(dir_x) * base_speed
            obj.vy = c0.vy * 0.5 - up if vy is None else float(vy)
        throw = Throw(self, dir_x, dir_y, force, aim, spare_cursor)
        obj.on_thrown(throw)
        obj.state = "free"
        self.release(obj)
        self.aerobic_increase(0.75)
        c0.vx += float(dir_x) * 8.0 * recoil
        c1.vx -= float(dir_x) * 4.0 * recoil
        for fn in self.throw_hooks:
            fn(self, obj, throw)
        return obj

    def throw_direction(self, input_x=0):
        """出手方向：身体竖着时看输入或朝向，趴着时看头朝哪边。"""
        c0, c1 = self.chunk0, self.chunk1
        if self.bodyMode == "Default" or abs(c0.x - c1.x) < 15.0:
            return input_x if input_x else self.facing
        return 1 if c0.x >= c1.x else -1

    def toss(self, obj, dir_x, ang_deg, power):
        """抛开手中非武器物：从身体外侧按角度出手，ang_deg 自竖直向上量起。"""
        if self.hand_of(obj) is None:
            return None
        c0, c1 = self.chunk0, self.chunk1
        obj.last_x, obj.last_y = obj.x, obj.y
        if dir_x < 0:
            obj.x = min(obj.x, min(c0.x, c1.x) - 8.0)
        elif dir_x > 0:
            obj.x = max(obj.x, max(c0.x, c1.x) + 8.0)
        obj.y = c0.y
        mass = getattr(obj, "mass", 0.1)
        k = clampf((mass - 0.2) / 0.3, 0.0, 1.0)
        speed = clampf(power / (mass * 0.8 + 0.08), 4.0, 14.0)
        a = math.radians(ang_deg * dir_x)
        mix = 0.6 + (0.3 - 0.6) * k
        obj.vx = c0.vx * mix + math.sin(a) * speed
        obj.vy = c0.vy * mix - math.cos(a) * speed
        if getattr(obj, "stalk", None) is not None:
            obj.stalk = None
        obj.state = "free"
        self.release(obj)
        self.aerobic_increase(0.75)
        return obj

    def _apply_carry(self):
        """手持物的位置由图形层贴到手上，这里只推进果柄。"""
        for obj in self.held_items():
            stalk = getattr(obj, "stalk", None)
            if stalk is not None and stalk.step(obj):
                obj.stalk = None


def _dot_norm(vx, vy, ux, uy):
    """归一化速度与单位轴的点积绝对值（|v̂·û|）；零速→0。"""
    m = math.hypot(vx, vy)
    if m < 1e-9:
        return 0.0
    return abs((vx * ux + vy * uy) / m)


def _angle_deg(ax, ay, bx, by):
    """两向量夹角（度，[0,180]）。"""
    am = math.hypot(ax, ay)
    bm = math.hypot(bx, by)
    if am < 1e-9 or bm < 1e-9:
        return 0.0
    d = (ax * bx + ay * by) / (am * bm)
    d = -1.0 if d < -1.0 else (1.0 if d > 1.0 else d)
    return math.degrees(math.acos(d))


def _slerp2(ax, ay, bx, by, t):
    """两单位向量的球面插值，t∈[0,1]。"""
    d = ax * bx + ay * by
    d = -1.0 if d < -1.0 else (1.0 if d > 1.0 else d)
    theta = math.acos(d) * (0.0 if t < 0.0 else (1.0 if t > 1.0 else t))
    rx, ry = bx - ax * d, by - ay * d
    rl = math.hypot(rx, ry)
    if rl < 1e-9:
        return ax, ay
    rx /= rl
    ry /= rl
    ct, st = math.cos(theta), math.sin(theta)
    return ax * ct + rx * st, ay * ct + ry * st


def _clamp_vel_mag(c, maxv):
    """把 chunk 速度矢量长度钳到 maxv（零重力蹬墙上限）。"""
    sp = math.hypot(c.vx, c.vy)
    if sp > maxv and sp > 1e-9:
        k = maxv / sp
        c.vx *= k
        c.vy *= k


def _is_vertical(pole):
    return abs(pole.bx - pole.ax) < abs(pole.by - pole.ay)


def _in_beam(pole, x, y):
    """(x, y) 在这根杆那一格里。"""
    if _is_vertical(pole):
        lo, hi = min(pole.ay, pole.by), max(pole.ay, pole.by)
        return abs(x - pole.ax) < ZEROG_GRAB_DIST and lo <= y <= hi
    lo, hi = min(pole.ax, pole.bx), max(pole.ax, pole.bx)
    return abs(y - pole.ay) < ZEROG_GRAB_DIST and lo <= x <= hi


def _closest_on_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    dd = dx * dx + dy * dy
    if dd < 1e-12:
        return ax, ay, math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / dd
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx, cy = ax + dx * t, ay + dy * t
    return cx, cy, math.hypot(px - cx, py - cy)

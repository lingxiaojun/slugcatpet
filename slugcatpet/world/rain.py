"""雨：每列的落点（被窗口挡住的列只落到窗口顶边）、强度缓变、风向漂移、雨丝与水花。"""

from __future__ import annotations
import math
import random
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPixmap,
    QRegion,
)

from .._paths import assets_dir, bundled_assets_dir
from ..core.units import clampf

COL_W = 4.0  # 落点数组的列宽（逻辑 px）
I_MAX = 0.7  # 雨量满格对应的强度
DANGER = 0.35  # 露天开始被雨推的强度
RISE = I_MAX / 900  # 每 tick 涨多少
FALL = I_MAX / 500  # 每 tick 落多少
KICK_SIDE = 20.0  # 判断旁边有无遮挡的距离
SHELTER_MARGIN = 36.0  # 躲雨落脚点离遮挡边缘的最小距离，约一个身长；遮挡太窄就站正中

DIR_RETARGET = 0.025  # 每 tick 换风向目标的概率
DIR_LERP = 0.01
DIR_STEP = 0.0125
SLANT_MAX = 0.11  # 风向 ±1 时雨丝的横移比
SLANT_BUCKETS = 5  # 雨丝贴图按斜度分档缓存

TILE = 256  # 雨丝贴图边长（逻辑 px）
# 雨丝是并排的一暗一亮两道细线，深浅背景上都看得见
RAIN_LIGHT = (235, 242, 255)
RAIN_DARK = (20, 26, 36)
# 远、近、雨幕三层：每张贴图的条数、长度范围、亮线峰值不透明度
LAYERS = (
    (110, (26.0, 44.0), 60),
    (36, (52.0, 84.0), 95),
    (240, (90.0, 170.0), 70),
)
FALL_SPEED = 44.0  # 合成后的雨每 tick 下落距离
SOFT = (1, 2)  # 雨丝贴图的横、纵模糊半径（雨层 px）
STREAK_FADE = 1.0  # 暴雨阶段雨丝淡出多少，让给雨幕
KEYS = 32  # 画面强度每隔 1/KEYS 预合成一张关键帧，之间按比例叠画
KEY_KEEP = 2 * (2 * SLANT_BUCKETS + 2)  # 雨强不变时要用的张数：雨丝、淡雨每斜度档两张，露天、屋檐下各两张
BLEND_STEPS = 64  # 两张关键帧之间按这么多档混合
JITTER = 2.5  # 满雨时雨层每帧随机错开的最大距离（逻辑 px），模仿原版的画面微震
RES_W = 1366  # 各层贴图生成时的宽度上限（原版分辨率），合成时放大到屏幕分辨率
OP_STEPS = 24  # 合成贴图按各层不透明度分档缓存
BAKE_STRIP = 64  # 后台合成时每段的行数
# 雨幕：照原版 DeathRain 着色器，每列一像素、相邻三列取平均，雨越大有效的列越多；
# 每列沿竖直方向连续明暗起伏，偏暗处叠黑、偏亮处叠白
CURTAIN_W, CURTAIN_H = 256, 512  # 贴图大小（逻辑 px）
CURTAIN_LEVELS = 12  # 按有效列占比分档
# 普通、暴雨、急雨三层：暗、亮峰值不透明度，纵向模糊半径（逻辑 px）；
# 急雨只在露天，拉得更长，像糊住的瀑布
CURTAIN = ((80, 66, 10), (165, 130, 10), (235, 195, 24))
SHELTER_K = 0.375  # 屋檐下和窗口里的淡雨：雨丝与普通、暴雨两层雨幕按这个比例打折
VEIL_RGB = (128, 136, 148)  # 露天暴雨的水幕颜色，背景被雨糊成一片
VEIL_MAX = 0.3
DIM_RGB = (19, 19, 21)  # 暴雨时猫和物品压暗到的颜色
DIM_GRAY = 38  # 桌面压暗到的灰度上限，按列在 0 到它之间（原版 0.15 × rand）
DIM_FROM = 0.5  # 画面强度过这个值开始压暗，和暴雨阶段同起（原版 0.9，桌面没法真的扭曲，提前一些）
DIM_MAX = 0.36

SPLASH_ATLAS = "waterSprites"
SPLASH_FRAMES = ("90DegreeSplash", "TallSplash")  # 宽、高两种水花
SPLASH_ANCHOR_Y = (0.8, 0.9)  # 落点在贴图里的高度（自上而下）
SPLASH_PER_PX = 0.04  # 每逻辑 px 露天落点上的水花数
SPLASH_SCALE = (0.2, 0.38)  # 相对贴图原尺寸的缩放范围
SPLASH_OPACITY = 0.35
SPLASH_JITTER = 10.0
FOG_RGB = (175, 185, 198)  # 水花从雾色到白色
SPLASH_TINTS = 5
SPLASH_RETRY = 80  # 没有水花图集时隔多少 tick 再找


def inv_lerp(a, b, v):
    return clampf((v - a) / (b - a), 0.0, 1.0)


def column_share(vis):
    """有雨的列占比：三个均匀随机数的平均值超过 1 − 1.5·vis 的概率。"""
    x = 3.0 * (1.0 - 1.5 * vis)
    if x <= 0.0:
        return 1.0
    if x >= 3.0:
        return 0.0
    if x < 1.0:
        cdf = x**3 / 6.0
    elif x < 2.0:
        cdf = (-2.0 * x**3 + 9.0 * x**2 - 9.0 * x + 3.0) / 6.0
    else:
        cdf = 1.0 - (3.0 - x) ** 3 / 6.0
    return 1.0 - cdf


def visual(i):
    """画面强度 0~1：雨量满格对应原版强度 1。"""
    return clampf(i / I_MAX, 0.0, 1.0)


def smooth(x):
    """0~1 的 S 形缓动。"""
    x = clampf(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def storm(g):
    """暴雨阶段的进度（原版 RainUnderCeilings），g 为画面强度。"""
    return inv_lerp(0.5, 1.0, g)


def layer_opacity(k, g):
    """第 k 层雨丝在画面强度 g 下的不透明度。"""
    vis = inv_lerp(0.0, 0.5, g)
    if k == 0:
        return smooth(vis) * 0.85
    if k == 1:
        return smooth(inv_lerp(0.15, 1.0, vis))
    return column_share(vis)


def _dim_alpha(g):
    """画面强度 g 下暴雨压暗的不透明度 0~255。"""
    return round(255 * DIM_MAX * inv_lerp(DIM_FROM, 1.0, g))


def _box(a, r, axis):
    """沿 axis 首尾相接的盒式模糊。"""
    if r <= 0:
        return a
    n = a.shape[axis]

    def cut(lo, hi):
        return a_pad[(slice(None),) * axis + (slice(lo, hi),)]

    head = (slice(None),) * axis
    a_pad = np.concatenate([a[head + (slice(n - r, n),)], a, a[head + (slice(0, r),)]], axis)
    out = cut(0, n).copy()
    for d in range(1, 2 * r + 1):
        out += cut(d, d + n)
    out /= 2 * r + 1
    return out


def _blur(a, rx, ry):
    """首尾相接的盒式模糊，a 的前两维为 (h, w)。"""
    return _box(_box(a, rx, 1), ry, 0)


def _streak_rgba(layer, bucket, q):
    """一层雨丝贴图：并排一暗一亮两道细线，沿长度渐显，首尾相接。"""
    f32 = np.float32
    count, (len_lo, len_hi), alpha = LAYERS[layer]
    slant = (bucket / (SLANT_BUCKETS - 1) * 2.0 - 1.0) * SLANT_MAX
    n = round(TILE * q)
    rng = random.Random(layer * 131 + 7)
    lines = [
        (
            rng.uniform(0, TILE) * q,
            rng.uniform(0, TILE) * q,
            rng.uniform(len_lo, len_hi),
            rng.uniform(0.6, 1.0),
        )
        for _ in range(count)
    ]
    rows = np.array([max(2, round(ln / math.hypot(slant, 1.0) * q)) for _, _, ln, _ in lines])
    which = np.repeat(np.arange(count), rows)  # 每个点属于哪条线
    j = np.arange(rows.sum()) - np.repeat(np.cumsum(rows) - rows, rows)  # 点在线上的行号
    t = (j / (rows[which] - 1)).astype(f32)
    a = np.array([alpha * k for *_, k in lines], f32)[which]
    prof = np.where(t < 0.75, t / 0.75, 1.0 - (t - 0.75) / 0.25 * 0.4).astype(f32) * a
    ys = (np.array([int(y) for _, y, _, _ in lines])[which] + j) % n
    xs = np.array([x for x, *_ in lines], f32)[which] + f32(slant) * j
    x0 = np.floor(xs).astype(int)
    fr = (xs - x0).astype(f32)
    light = np.zeros((n, n), f32)
    dark = np.zeros((n, n), f32)
    for img, k, shift in ((light, 1.0, 0), (dark, 0.55, -1)):
        np.add.at(img, (ys, (x0 + shift) % n), prof * (1.0 - fr) * k)
        np.add.at(img, (ys, (x0 + shift + 1) % n), prof * fr * k)
    light = np.minimum(light, 255.0)
    dark = np.minimum(dark, 255.0)
    rgba = np.empty((n, n, 4), f32)
    for c in range(3):
        rgba[..., c] = (light * RAIN_LIGHT[c] + dark * RAIN_DARK[c]) / 255.0
    rgba[..., 3] = np.minimum(light + dark, 255.0)
    return _blur(rgba, *SOFT)


def curtain_level(share):
    """有效列占比 share 用哪一档雨幕；0 为不画。"""
    return math.ceil(clampf(share, 0.0, 1.0) * CURTAIN_LEVELS)


def _columns(rng, w):
    """每列的随机值，相邻三列取平均（首尾相接）。"""
    r0 = rng.random(w)
    return (np.roll(r0, 1) + r0 + np.roll(r0, -1)) / 3.0


def _veil_rgba(scale, alpha):
    """露天暴雨的水幕：纯色，不透明度 alpha（0~1）已乘进去。"""
    n = max(1, round(64 * scale))
    rgba = np.empty((n, n, 4), np.float32)
    rgba[..., :3] = np.array(VEIL_RGB, np.float32) * alpha
    rgba[..., 3] = 255.0 * alpha
    return rgba


def _gray_rgba(scale, alpha):
    """桌面压暗用的按列深灰，不透明度 alpha（0~1）已乘进去。"""
    w = round(CURTAIN_W * scale)
    g = _columns(np.random.default_rng(29), w) * DIM_GRAY * alpha
    rgba = np.empty((64, w, 4), np.float32)
    rgba[..., :3] = g[None, :, None]
    rgba[..., 3] = 255.0 * alpha
    return rgba


def _curtain_rgba(level, q, layer):
    f32 = np.float32
    w, h = round(CURTAIN_W * q), round(CURTAIN_H * q)
    rng = np.random.default_rng(layer * 71 + 13)  # 各档同一套列，档越高亮起的列越多
    r = _columns(rng, w).astype(f32)
    live = (r >= 1.0 - level / CURTAIN_LEVELS).astype(f32)
    y = (np.arange(h, dtype=f32) * f32(2.0 * np.pi / h))[:, None]
    wave = np.zeros((h, w), f32)
    for lo, hi, amp in ((1, 3, 0.5), (2, 5, 0.3), (4, 9, 0.2)):
        c = rng.integers(lo, hi, w).astype(f32)
        ph = (rng.random(w) * 6.283).astype(f32)
        wave += np.sin(y * c + ph) * f32(amp)
    light = np.clip(wave * f32(0.85) + (r - f32(0.5)) * f32(0.8), -1.0, 1.0)
    peak_dark, peak_light = CURTAIN[layer][:2]
    ry = max(1, round(CURTAIN[layer][2] * q))
    dark = _blur((np.maximum(-light, 0.0) ** 1.3 * live * peak_dark).astype(f32), 0, ry)
    bright = _blur((np.maximum(light, 0.0) ** 1.3 * live * peak_light).astype(f32), 0, ry)
    rgba = np.empty((h, w, 4), f32)
    rgba[..., :3] = bright[..., None]
    rgba[..., 3] = dark + bright
    return rgba


def _scaled(pm, w):
    """贴图每个像素（预乘）乘以 w 的一份拷贝，设备像素比为 1。"""
    out = pm.copy()
    out.setDevicePixelRatio(1.0)
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    p.fillRect(out.rect(), QColor(0, 0, 0, round(255 * w)))
    p.end()
    return out


def push_force(i):
    """露天时被雨推的力度 0~1。"""
    return inv_lerp(DANGER, I_MAX, i) ** 0.8


class Rain:
    def __init__(self, seed=0):
        self._rng = random.Random(0x7A1 ^ (int(seed or 0) * 2654435761 & 0xFFFFFFFF))
        self.intensity = 0.0
        self.target = 0.0
        self.direction = 0.0
        self._dir_to = 0.0
        self._fall = [0.0, 0.0]  # 合成贴图的 (x, y) 卷动量
        self._last_fall = [0.0, 0.0]
        self._regions = None  # 设备 px 的 (缩放, 露天, 屋檐下, 压暗) 裁剪区
        self.window_block = False
        self._WL = 0.0
        self._HL = 0.0
        self._rects = []
        self._reach = []
        self.version = 0  # 遮挡变一次加一
        self.runs = []  # 露天段 (x0, x1, y)：雨从顶上落到 y，按窗口精确边缘切
        self.open_frac = 1.0  # 露天面积占全屏的比例
        self.splashes = []  # (落点 y, x, 种类, 转角, x 缩放, y 缩放, 色档)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rain")  # 贴图在后台生成
        self._parts = {}  # 各层贴图（只在后台线程里读写）：(种类, 层, 档, 分辨率) → QImage
        self._keys = OrderedDict()  # (块, 缩放, 宽, 斜度档, 关键帧) → 贴图；None 为这一帧什么都不画
        self._jobs = {}  # 同上的键 → 后台任务
        self._blend = {}  # 块 → (关键帧甲, 关键帧乙, 比例档, 混好的贴图)
        self._drawn = {}  # 块 → 正在画的斜度档
        self._splash_pm = None
        self._splash_wait = 0
        self._frame = None  # 定格的一帧：(画面强度, 斜度档, 水花, 卷动偏移)

    @property
    def active(self) -> bool:
        return self.intensity > 0.0

    @property
    def shows(self) -> bool:
        """屏上有没有雨可画；全被窗口挡住时也有淡雨。"""
        return self.intensity > 0.0 and self._WL > 0.0 and (not self.window_block or bool(self.runs))

    def set_target(self, i):
        self.target = clampf(float(i), 0.0, I_MAX)

    def resize(self, WL, HL):
        if (WL, HL) != (self._WL, self._HL):
            self._WL, self._HL = WL, HL
            self._rebuild()

    def set_shelters(self, rects):
        """rects：挡雨窗口的逻辑坐标 (x0, y0, x1, y1)。"""
        rects = sorted(rects)
        if rects != self._rects:
            self._rects = rects
            self._rebuild()

    def _rebuild(self):
        self.version += 1
        WL, HL = self._WL, self._HL
        rects = [
            (max(x0, 0.0), max(y0, 0.0), min(x1, WL), min(y1, HL))
            for x0, y0, x1, y1 in self._rects
            if y1 > 0.0 and y0 < HL and x1 > 0.0 and x0 < WL
        ]
        n = max(1, math.ceil(WL / COL_W))
        reach = [HL] * n
        for x0, y0, x1, _ in rects:
            for i in range(int(x0 // COL_W), min(n, math.ceil(x1 / COL_W))):
                if y0 < reach[i]:
                    reach[i] = y0
        self._reach = reach
        xs = sorted({0.0, WL, *(r[0] for r in rects), *(r[2] for r in rects)})
        runs = []
        for a, b in zip(xs, xs[1:]):
            if b <= a:
                continue
            top = min((y0 for x0, y0, x1, _ in rects if x0 <= a and x1 >= b), default=HL)
            if runs and runs[-1][2] == top and runs[-1][1] == a:
                runs[-1] = (runs[-1][0], b, top)
            else:
                runs.append((a, b, top))
        self.runs = [r for r in runs if r[2] > 0.0]
        area = WL * HL
        self.open_frac = sum((x1 - x0) * y for x0, x1, y in self.runs) / area if area > 0 else 1.0
        self._regions = None

    def reach_at(self, x):
        """这一列的雨落到的 y。"""
        if not self._reach:
            return self._HL
        i = int(x // COL_W)
        return self._reach[min(max(i, 0), len(self._reach) - 1)]

    def exposed(self, x, y):
        return y < self.reach_at(x)

    def touches(self, o):
        """质点（中心 ± 半径，和雨砸到的范围一样）有没有一处露天。"""
        r = getattr(o, "rad", 0.0)
        return self.exposed(o.x - r, o.y - r) or self.exposed(o.x + r, o.y - r)

    def dangerous(self) -> bool:
        """雨正在或将要大到露天会被推。"""
        return self.intensity > 0.0 and max(self.target, self.intensity) >= DANGER

    def keeps_out(self, x, y, margin=SHELTER_MARGIN) -> bool:
        """大雨时这里露天或离遮挡边缘不到 margin，猫不去；遮挡太窄就只留正中。"""
        if not self.dangerous():
            return False
        if self.exposed(x, y) or not self._reach:
            return self.exposed(x, y)
        reach, n = self._reach, len(self._reach)
        i = min(max(int(x // COL_W), 0), n - 1)
        k = math.ceil(2.0 * margin / COL_W) + 1
        a = b = None
        for j in range(1, k + 1):
            if i - j < 0:
                break
            if reach[i - j] > y:
                a = (i - j + 1) * COL_W
                break
        for j in range(1, k + 1):
            if i + j >= n:
                break
            if reach[i + j] > y:
                b = (i + j) * COL_W
                break
        if a is not None and b is not None:
            margin = min(margin, (b - a) * 0.5 - COL_W)
        return (a is not None and x - a < margin) or (b is not None and b - x < margin)

    def floor_spans(self, lo, hi, margin):
        """地面上有遮挡的 x 段，限在 [lo, hi] 内、两头各收 margin。"""
        out = []
        start = None
        HL = self._HL
        for i, r in enumerate(self._reach + [HL]):
            if r < HL:
                if start is None:
                    start = i
            elif start is not None:
                x0, x1 = max(start * COL_W, lo), min(i * COL_W, hi)
                a, b = x0 + margin, x1 - margin
                if a > b and x1 > x0:
                    a = b = (x0 + x1) * 0.5
                if a <= b:
                    out.append((a, b))
                start = None
        return out

    def nearest_shelter_x(self, x, lo, hi, margin):
        """离 x 最近的有遮挡的地面 x；没有返回 None。"""
        best = None
        for a, b in self.floor_spans(lo, hi, margin):
            p = min(max(x, a), b)
            if best is None or abs(p - x) < abs(best - x):
                best = p
        return best

    def kick(self, o, force, grounded, chunks=0, cap=None):
        """推一下露天的质点（着地向上乱踢、偏向有遮挡的一侧，空中往下砸），返回晕几 tick，没淋到返回 None。"""
        rng = self._rng
        r = getattr(o, "rad", 0.0)
        x, y = o.x + rng.uniform(-r, r), o.y + rng.uniform(-r, r)
        if not self.exposed(x, y):
            return None
        if grounded:
            bias = (self.reach_at(x + KICK_SIDE) <= y) - (self.reach_at(x - KICK_SIDE) <= y)
            a = math.radians(rng.uniform(-30.0, 30.0) + bias * 16.0)
            m = rng.random() * 9.0 * force / o.mass
            if cap is not None:
                m = min(m, cap)
            o.vx += math.sin(a) * m
            o.vy -= math.cos(a) * m
        else:
            m = rng.random() ** 5 * 16.5 * force / o.mass
            o.vy += m if cap is None else min(m, cap)
        if chunks and rng.random() ** 1.2 * 2.0 * chunks < force:
            return rng.randint(1, max(1, int(9.0 * force)))
        return 0

    def advance(self, ticks):
        """不画的时候把雨强度按 tick 数往目标推进。"""
        i, tgt = self.intensity, self.target
        self.intensity = min(i + RISE * ticks, tgt) if i < tgt else max(i - FALL * ticks, tgt)

    def step(self):
        i, tgt = self.intensity, self.target
        if i < tgt:
            i = min(i + RISE, tgt)
        elif i > tgt:
            i = max(i - FALL, tgt)
        self.intensity = i
        rng = self._rng
        if rng.random() < DIR_RETARGET:
            self._dir_to = rng.uniform(-1.0, 1.0)
        d = self.direction + (self._dir_to - self.direction) * DIR_LERP
        d = min(d + DIR_STEP, self._dir_to) if d < self._dir_to else max(d - DIR_STEP, self._dir_to)
        self.direction = d
        slant = d * SLANT_MAX
        f, last = self._fall, self._last_fall
        last[0], last[1] = f[0], f[1]
        f[0] = (f[0] + FALL_SPEED * slant) % CURTAIN_W
        f[1] = (f[1] + FALL_SPEED) % CURTAIN_H
        self._step_splashes()

    def _step_splashes(self):
        """每 tick 在露天落点上随机摆一批水花。"""
        size = inv_lerp(0.0, 0.5, visual(self.intensity)) ** 2
        self.splashes = []
        if size < 0.05 or not self.runs:
            return
        rng = self._rng
        total = sum(x1 - x0 for x0, x1, _ in self.runs)
        tint = self.intensity / I_MAX * (SPLASH_TINTS - 1)
        for _ in range(int(total * SPLASH_PER_PX * (1.0 + push_force(self.intensity)))):
            pick = rng.random() * total
            for x0, x1, top in self.runs:
                if pick < x1 - x0:
                    break
                pick -= x1 - x0
            x = x0 + pick + rng.uniform(-SPLASH_JITTER, SPLASH_JITTER)
            sy = size * rng.uniform(*SPLASH_SCALE)
            if rng.random() < 0.5:
                kind, rot, sx = 0, rng.uniform(-45.0, 45.0), (-sy if rng.random() < 0.5 else sy)
            else:
                kind, rot, sx = 1, rng.uniform(-25.0, 25.0), rng.uniform(-1.0, 1.0) * sy
            self.splashes.append((top, x, kind, rot, sx, sy, round(rng.random() * tint)))
        self.splashes.sort(key=lambda sp: sp[0])

    def _slant_bucket(self):
        b = round((self.direction + 1.0) * 0.5 * (SLANT_BUCKETS - 1))
        return min(max(b, 0), SLANT_BUCKETS - 1)

    def _part(self, kind, k, lv, q):
        """一层贴图（后台线程里调用）：c 雨幕、s 雨丝、g 压暗的深灰、v 水幕；预乘 RGBA，8 位。"""
        key = (kind, k, lv, q)
        a = self._parts.get(key)
        if a is None:
            if kind == "c":
                rgba = _curtain_rgba(lv, q, k)
            elif kind == "s":
                rgba = _streak_rgba(k, lv, q)
            elif kind == "v":
                rgba = _veil_rgba(q, lv / OP_STEPS)
            else:
                rgba = _gray_rgba(q, lv / OP_STEPS)
            a = self._parts[key] = np.clip(rgba, 0, 255).astype(np.uint8)
        return a

    def _bake(self, scale, q, parts):
        """把各层按最近邻放大到屏幕分辨率、自下而上叠成一张（后台线程里调用，全用 numpy）。

        按横条分段做，每段很短，主线程随时能拿到解释器锁。"""
        f32 = np.float32
        h, w = round(CURTAIN_H * scale), round(CURTAIN_W * scale)
        layers = []
        for kind, k, lv, op in parts:
            flat = kind in ("g", "v")  # 不透明度已做进贴图，按屏幕分辨率
            d = scale if flat else q
            a = self._part(kind, k, op if flat else lv, d)
            yi = (np.arange(h) * d / scale).astype(int) % a.shape[0]
            xi = (np.arange(w) * d / scale).astype(int) % a.shape[1]
            layers.append((a, yi, xi, 1.0 if flat else op / OP_STEPS))
        out = np.empty((h, w, 4), np.uint8)
        for y0 in range(0, h, BAKE_STRIP):
            strip = np.zeros((min(BAKE_STRIP, h - y0), w, 4), f32)
            for a, yi, xi, mul in layers:
                src = a[yi[y0 : y0 + BAKE_STRIP]][:, xi].astype(f32)
                if mul != 1.0:
                    src *= f32(mul)
                strip *= 1.0 - src[..., 3:4] / f32(255.0)
                strip += src
            out[y0 : y0 + BAKE_STRIP] = np.clip(strip, 0, 255)
        bgra = np.ascontiguousarray(out[..., [2, 1, 0, 3]])
        return QImage(bgra.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()

    def _key_pm(self, key):
        """一张关键帧：做好了返回贴图（什么都不画的为 None），还在做返回 False。"""
        if key in self._keys:
            self._keys.move_to_end(key)
            return self._keys[key]
        job = self._jobs.get(key)
        if job is None:
            slot, scale, _, bucket, k = key
            parts = self._parts_for(slot, k / KEYS, bucket)
            if not parts:
                self._keys[key] = None
                return None
            q = scale * min(1.0, RES_W / max(1.0, self._WL * scale))
            self._jobs[key] = self._pool.submit(self._bake, scale, q, parts)
            return False
        if not job.done():
            return False
        del self._jobs[key]
        try:
            pm = QPixmap.fromImage(job.result())
            pm.setDevicePixelRatio(key[1])
        except Exception:
            return False
        self._keys[key] = pm
        while len(self._keys) > KEY_KEEP:
            self._keys.popitem(last=False)
        return pm

    def _layers(self, slot, scale, bucket, g):
        """slot 在画面强度 g 下要铺的贴图：两张相邻关键帧按比例混成一张，没做好的用最近做好的代替。

        换斜度档时先接着画旧档、旧档的强度照常跟上，新档这两张做好了再换过去。"""
        x = g * KEYS
        k0 = min(int(x), KEYS)
        f = x - k0
        k1 = min(k0 + 1, KEYS)
        want = {k0, k1}
        if self.target > self.intensity:  # 雨强在变时多备下一张
            want.add(k1 + 1)
        elif self.target < self.intensity:
            want.add(k0 - 1)
        for key in [key for key in self._jobs if key[0] == slot and key[4] not in want]:
            job = self._jobs[key]
            if job.cancel() or job.done():
                del self._jobs[key]
        base = (slot, scale, self._WL)
        drawn = self._drawn.get(slot, bucket)
        if drawn != bucket:
            for k in (k0, k1):
                self._key_pm((*base, bucket, k))
            if all(
                self._keys.get((*base, bucket, k)) is not None or (*base, bucket, k) in self._keys
                for k in (k0, k1)
            ):
                drawn = bucket
        self._drawn[slot] = drawn
        bucket = drawn
        for k in sorted(want, key=lambda k: abs(k - x)):
            if 0 <= k <= KEYS:
                self._key_pm((*base, bucket, k))

        def pick(k):
            pm = self._key_pm((*base, bucket, k))
            if pm is not False:
                return pm
            near = [kk for kk in self._keys if kk[:3] == base]  # 换斜度档时先用别档的
            if not near:
                return False
            best = min(near, key=lambda kk: (abs(kk[4] - k), kk[3] != bucket))
            return self._keys[best]

        a = pick(k0)
        b = pick(k1) if f > 0.001 else a
        a = a or None
        b = b or None
        if b is a or b is None or a is None:
            pm = a or b
            return pm, (1.0 if b is a else (1.0 - f if pm is a else f))
        step = round(f * BLEND_STEPS)
        last = self._blend.get(slot)
        if last is not None and last[0] is a and last[1] is b and last[2] == step:
            return last[3], 1.0
        pm, top = _scaled(a, 1.0 - step / BLEND_STEPS), _scaled(b, step / BLEND_STEPS)
        p = QPainter(pm)
        p.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Plus
        )  # 两张各自按比例缩好再相加，不会溢出
        p.drawPixmap(0, 0, top)
        p.end()
        pm.setDevicePixelRatio(a.devicePixelRatio())
        self._blend[slot] = (a, b, step, pm)
        return pm, 1.0

    def release(self):
        """雨停了放掉贴图，下次下雨再做。"""
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()
        self._keys.clear()
        self._blend.clear()
        self._drawn.clear()
        self._pool.submit(self._parts.clear)

    def _parts_for(self, slot, g, bucket):
        """slot 在画面强度 g 下的各层清单：(种类, 层, 档, 不透明度档)。"""
        dim = _dim_alpha(g) / 255.0
        fade = 1.0 - STREAK_FADE * inv_lerp(0.5, 0.95, g)
        streaks = [("s", k, bucket, layer_opacity(k, g) * fade) for k in range(len(LAYERS))]
        curtains = [
            (
                "c",
                0,
                curtain_level(inv_lerp(0.0, 0.5, g)),
                smooth(inv_lerp(0.0, 0.5, g)) ** 0.6 * (1.0 - 0.7 * inv_lerp(0.5, 0.9, g)),
            ),
            ("c", 1, curtain_level(inv_lerp(0.4, 0.75, g)), inv_lerp(0.4, 0.85, g)),  # 暴雨那层
        ]
        if slot == "shelter":
            parts = [("g", 0, 1, dim)]
        elif slot == "indoor":
            parts = [(kind, k, lv, op * SHELTER_K) for kind, k, lv, op in curtains + streaks]
        elif slot == "streak":
            parts = streaks
        else:
            torrent = inv_lerp(0.55, 1.0, g)
            parts = [
                ("g", 0, 1, dim),
                ("v", 0, 1, VEIL_MAX * torrent),
                *curtains,
                ("c", 2, curtain_level(inv_lerp(0.5, 1.0, g)), torrent),
            ]
        return tuple(
            (kind, k, lv, round(op * OP_STEPS))
            for kind, k, lv, op in parts
            if op > 0.02 and (lv or kind == "s")
        )

    def _splashes(self):
        """各色档的两种水花贴图；没有图集时返回 None。"""
        if self._splash_pm is None:
            if self._splash_wait > 0:
                self._splash_wait -= 1
                return None
            self._splash_wait = SPLASH_RETRY
            for d in (assets_dir(), bundled_assets_dir()):
                if (d / SPLASH_ATLAS).is_file() and (d / f"{SPLASH_ATLAS}.png").is_file():
                    from ..rendering.atlas import Atlas

                    try:
                        at = Atlas(SPLASH_ATLAS, d)
                    except (OSError, ValueError, KeyError):
                        continue
                    self._splash_pm = [
                        [at.sprite(f, self._tint(t)) for f in SPLASH_FRAMES] for t in range(SPLASH_TINTS)
                    ]
                    break
        return self._splash_pm

    @staticmethod
    def _tint(level):
        f = level / (SPLASH_TINTS - 1)
        return QColor(*(round(a + (255 - a) * f) for a in FOG_RGB))

    def snapshot(self, ts):
        """定格当前画面；两次定格之间重画都画同一帧。"""
        if not self.shows:
            self._frame = None
            return
        f, last = self._fall, self._last_fall
        fx = last[0] + ((f[0] - last[0] + CURTAIN_W / 2) % CURTAIN_W - CURTAIN_W / 2) * ts
        fy = last[1] + ((f[1] - last[1] + CURTAIN_H / 2) % CURTAIN_H - CURTAIN_H / 2) * ts
        j = JITTER * smooth(storm(visual(self.intensity)))
        fx += self._rng.uniform(-j, j)
        fy += self._rng.uniform(-j, j)
        self._frame = (self.intensity, self._slant_bucket(), self.splashes, (fx, fy))

    def paint_rect(self, scale, margin=0.0) -> QRect:
        """本帧雨覆盖的范围（设备 px）。"""
        if not self.active:
            return QRect()
        return QRectF(0.0, -margin * scale, self._WL * scale, (self._HL + margin) * scale).toAlignedRect()

    def _clip_regions(self, scale):
        """露天、屋檐下、淡雨（屋檐下和窗口里）、深灰盖不到（窗口里和地面以下）四块裁剪区（设备 px），几何不变时复用。"""
        if self._regions is not None and self._regions[0] == scale:
            return self._regions[1:]

        def box(x0, y0, x1, y1):
            return QRect(
                round(x0 * scale),
                round(y0 * scale),
                round(x1 * scale) - round(x0 * scale),
                round(y1 * scale) - round(y0 * scale),
            )

        open_air = QRegion()
        for x0, x1, y in self.runs:
            open_air = open_air.united(box(x0, 0.0, x1, y))
        screen = QRegion(box(0.0, 0.0, self._WL, self._HL))
        dim = screen
        for x0, y0, x1, y1 in self._rects:
            dim = dim.subtracted(QRegion(box(x0, y0, x1, y1)))
        lit = QRegion(box(-self._WL, -self._HL, self._WL * 2.0, self._HL * 3.0)).subtracted(dim)
        self._regions = (scale, open_air, dim.subtracted(open_air), screen.subtracted(open_air), lit)
        return self._regions[1:]

    def draw(self, p, scale, scene=None):
        """画定格的那一帧；scene 为已画的猫和物品的范围（设备 px），暴雨时把它们压暗。"""
        if self._frame is None:
            return
        i, bucket, splashes, (fx, fy) = self._frame
        g = visual(i)
        open_air, shelter, indoor, lit = self._clip_regions(scale)
        p.save()
        xf = p.transform()
        full = QRectF(0.0, 0.0, self._WL, self._HL)
        dim = _dim_alpha(g)
        if dim > 0 and scene is not None and not scene.isEmpty():
            # 深灰盖在猫和物品上就压暗了它们；窗口里和地面以下盖不到，单独压暗那一块
            cats = lit.intersected(QRegion(QRect(scene).adjusted(-2, -2, 2, 2)))
            if not cats.isEmpty():
                self._clip(p, xf, cats)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                p.fillRect(
                    QRectF(-self._WL, -self._HL, self._WL * 3.0, self._HL * 4.0), QColor(*DIM_RGB, dim)
                )
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        offset = QPointF(-fx % CURTAIN_W, -fy % CURTAIN_H)
        for region, slot, slot_bucket in (
            (open_air, "open", 0),
            (open_air, "streak", bucket),
            (shelter, "shelter", 0),
            (indoor, "indoor", bucket),
        ):
            if region.isEmpty() or (region is indoor and self.window_block):
                continue
            pm, op = self._layers(slot, scale, slot_bucket, g)
            if pm is None:
                continue
            self._clip(p, xf, region)
            p.setOpacity(op)
            p.drawTiledPixmap(full, pm, offset)
        p.setOpacity(1.0)
        p.setClipping(False)
        pms = self._splashes() if splashes else None
        if pms:
            p.setOpacity(SPLASH_OPACITY)
            clip = None
            for y, x, kind, rot, sx, sy, tint in splashes:
                if y != clip:
                    p.setClipRect(QRectF(0.0, y - 1e4, self._WL, 1e4))  # 只画到落点以上
                    clip = y
                spm = pms[tint][kind]
                w, h = spm.width(), spm.height()
                p.save()
                p.translate(x, y)
                p.rotate(rot)
                p.scale(sx, sy)
                p.drawPixmap(QPointF(-w * 0.5, -h * SPLASH_ANCHOR_Y[kind]), spm)
                p.restore()
        p.restore()

    @staticmethod
    def _clip(p, xf, region):
        """按设备 px 的区域裁剪，之后接着用逻辑坐标画。"""
        p.resetTransform()
        p.setClipRegion(region)
        p.setTransform(xf)

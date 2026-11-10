"""珍珠的物理：和石头一样的单个质点，另带文字、颜色、悬浮符文和闪烁。"""

from __future__ import annotations

import math
import random

from ...core.units import lerp
from ..state import ItemState
from ..stone.model import Stone

TEXT_MAX = 1024
GLYPH_COUNT = 14
PLAIN_RGB = (179, 179, 179)

GLIMMER_WAIT = (20, 40)  # 两次闪烁的间隔
GLIMMER_LEN = (5.0, 15.0)  # 一次闪烁的帧数

CALM_SPEED = 0.8  # 位移低于此值算静止
CALM_TICKS = 12  # 静止多久符文才浮出来
GLYPH_IN = 0.05
GLYPH_OUT = 0.2


class Pearl(Stone):
    throwable = False

    __slots__ = (
        "text",
        "color",
        "glyph",
        "editing",
        "glimmer",
        "last_glimmer",
        "hover_ticks",
        "note_cache",
        "glyph_fade",
        "_calm",
        "_glim_prog",
        "_glim_speed",
        "_glim_wait",
        "_fx_rng",
    )

    def __init__(self, x: float, y: float, seed: int = 0):
        super().__init__(x, y, seed)
        self.text = ""
        self.color = None  # None 为没写过的普通珍珠
        self.glyph = None  # 悬浮符文的编号
        self.editing = False
        self.glimmer = self.last_glimmer = 0.0
        self.hover_ticks = 0
        self.note_cache = None
        self.glyph_fade = 0.0
        self._calm = 0
        self._glim_prog = 1.0
        self._glim_speed = 0.0
        self._glim_wait = 0
        self._fx_rng = random.Random(int(seed) * 7919 + 1)

    @property
    def written(self) -> bool:
        return self.color is not None

    def rgb(self):
        return PLAIN_RGB if self.color is None else self.color

    def on_thrown(self, throw) -> None:
        """被猫抛出：只带点自旋，不算投掷的武器。"""
        self.spin = throw.dir_x * 8.0

    def at_rest_on_ground(self, HL: float) -> bool:
        return not self.editing and super().at_rest_on_ground(HL)

    def collision_chunks(self):
        super().collision_chunks()
        if self.editing:
            self.collide_with_objects = False
        return (self,)

    def step(self, WL: float, HL: float) -> None:
        if self.editing:
            self.last_x, self.last_y = self.x, self.y
            self.last_rotation = self.rotation_deg
            self.vx = self.vy = 0.0
        else:
            super().step(WL, HL)
        self._tick_look()

    def _tick_look(self) -> None:
        """闪烁和符文的浮现：被拿起、拖着、飞行或刚落下时符文收起，静止一会儿才浮出。"""
        rng = self._fx_rng
        self.last_glimmer = self.glimmer
        self.glimmer = math.sin(self._glim_prog * math.pi) * rng.random()
        if self._glim_prog < 1.0:
            self._glim_prog = min(1.0, self._glim_prog + self._glim_speed)
        elif self._glim_wait > 0:
            self._glim_wait -= 1
        else:
            self._glim_wait = rng.randint(*GLIMMER_WAIT)
            self._glim_prog = 0.0
            self._glim_speed = 1.0 / lerp(GLIMMER_LEN[0], GLIMMER_LEN[1], rng.random())

        still = (
            self.state == ItemState.FREE
            and not self.editing
            and math.hypot(self.x - self.last_x, self.y - self.last_y) < CALM_SPEED
        )
        self._calm = self._calm + 1 if still else 0
        show = self.glyph is not None and self._calm >= CALM_TICKS
        if show:
            self.glyph_fade = min(1.0, self.glyph_fade + GLYPH_IN)
        else:
            self.glyph_fade = max(0.0, self.glyph_fade - GLYPH_OUT)

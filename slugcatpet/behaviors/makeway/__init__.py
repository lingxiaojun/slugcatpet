"""让路。"""

from __future__ import annotations

from ...brain.social.traffic import YIELD
from ...brain.state import CROWD, STALL
from ...core.creature import BLOCKED
from ..base import Behavior
from .state import MakeWay


class MakeWayBehavior(Behavior):
    key = "makeway"
    title = {"zh": "让路", "en": "Make way"}
    doc = "别的猫要从这里过时让开：地上趴下让它跨过去，横杆上换到杆的另一面，竖杆上顺着挪开；停在别的猫身上又不走时自己跳下来。自己走到一半路断了就放下这件事。"

    def mount(self, fsm):
        fsm.add_state(MakeWay)

        def scan(cursor):
            o = fsm.social.order(fsm.win)
            if o is not None and o.kind in YIELD and not fsm.blocked(CROWD):
                fsm._transition("MakeWay")

        def stall(cursor):
            b = fsm.body
            if b.walk_result == BLOCKED or b.lane_blocked:
                fsm.interrupt(STALL, "MakeWay")

        fsm.register_rule(stall, STALL)
        fsm.register_rule(scan, CROWD)

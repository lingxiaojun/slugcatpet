"""珍珠编辑弹窗：写文字、选颜色、勾选是否有悬浮符文。"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
)

from ...i18n import t
from ..theme import qss
from ..widgets.dialogs import CardDialog

EDIT_MIN_H = 96
EDIT_MAX_H = 300
FIT_SLACK = 2


def _u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class PearlEditor(CardDialog):
    def __init__(self, text, color, glyph_on, palette, limit, parent=None):
        super().__init__(t("pearl_title"), parent, width=320)
        self._limit = limit
        self._palette = palette
        self._prev = text[:limit]
        self.edit = QPlainTextEdit()
        self.edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.edit.setFixedHeight(EDIT_MIN_H)
        self._measure = QTextDocument(self)
        self.edit.setPlainText(text[:limit])
        self.edit.textChanged.connect(self._on_text)
        self.body.addWidget(self.edit)
        self._count = QLabel()
        self._count.setObjectName("pearlCount")
        self._count.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.body.addWidget(self._count)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._group = QButtonGroup(self)
        for i, rgb in enumerate(palette):
            b = QPushButton()
            b.setObjectName("pearlSwatch")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"background:rgb({rgb[0]},{rgb[1]},{rgb[2]});")
            b.setChecked(tuple(rgb) == tuple(color))
            self._group.addButton(b, i)
            row.addWidget(b)
        row.addStretch(1)
        self.body.addLayout(row)
        if self._group.checkedId() < 0:
            self._group.button(0).setChecked(True)

        self._glyph = QCheckBox(t("pearl_glyph"))
        self._glyph.setChecked(glyph_on)
        self.body.addWidget(self._glyph)

        self.cancel_btn = self.add_button(t("settings_cancel"), "ghost")
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = self.add_button(t("settings_ok"), "primary")
        self.ok_btn.clicked.connect(self.accept)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.accept)
        self._clamp()
        self.setStyleSheet(qss.PEARL_EDITOR)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._fit()

    def _on_text(self):
        self._clamp()
        self._fit()

    def _clamp(self):
        s = self.edit.toPlainText()
        over = len(s) - self._limit
        if over > 0:
            old = self._prev
            a, n = 0, min(len(s), len(old))
            while a < n and s[a] == old[a]:
                a += 1
            b = 0
            while b < n - a and s[-1 - b] == old[-1 - b]:
                b += 1
            new = s[a : len(s) - b]
            keep = new[: max(0, len(new) - over)]
            cur = QTextCursor(self.edit.document())
            cur.setPosition(_u16(s[:a]) + _u16(keep))
            cur.setPosition(_u16(s[: len(s) - b]), QTextCursor.MoveMode.KeepAnchor)
            cur.joinPreviousEditBlock()
            cur.removeSelectedText()
            cur.endEditBlock()
            s = self.edit.toPlainText()
        self._prev = s
        self._count.setText(f"{len(s)}/{self._limit}")

    def _fit(self):
        e = self.edit
        m = self._measure
        m.setDefaultFont(e.font())
        m.setDefaultTextOption(e.document().defaultTextOption())
        m.setDocumentMargin(e.document().documentMargin())
        m.setPlainText(e.toPlainText())
        m.setTextWidth(e.viewport().width())
        extra = e.height() - e.viewport().height()
        want = max(EDIT_MIN_H, min(EDIT_MAX_H, math.ceil(m.size().height()) + extra + FIT_SLACK))
        if want != e.height():
            e.setFixedHeight(want)
            self.refit()
            e.ensureCursorVisible()

    def focus_text(self):
        self.edit.setFocus()
        self.edit.moveCursor(self.edit.textCursor().MoveOperation.End)

    def text(self) -> str:
        return self.edit.toPlainText()

    def color(self):
        return tuple(self._palette[max(0, self._group.checkedId())])

    def glyph_on(self) -> bool:
        return self._glyph.isChecked()

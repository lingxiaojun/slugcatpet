"""自绘卡片弹窗：show()=非模态，open()=WindowModal，不走 exec。"""

from __future__ import annotations
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QButtonGroup,
)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QGuiApplication, QTextDocument, QTextOption
from ..theme import qss
from .screens import clamp_rect

# 壳级 QSS：防父窗样式渗透
_CARD_QSS = qss.CARD

_BTN_KIND = {"primary": "btnPrimary", "ghost": "btnGhost", "danger": "btnDanger"}


class CardDialog(QDialog):
    def __init__(self, title: str, parent=None, width: int = 300):
        super().__init__(parent)
        self.setObjectName("cardShell")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._drag_off = None
        self._finished = False
        self._card = QWidget(self)
        self._card.setObjectName("cardRoot")
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setFixedWidth(width)
        col = QVBoxLayout(self._card)
        col.setContentsMargins(24, 20, 24, 18)
        col.setSpacing(12)
        lbl = QLabel(title)
        lbl.setObjectName("cardTitle")
        col.addWidget(lbl)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        col.addLayout(self.body)
        self._btn_row = QHBoxLayout()
        self._btn_row.setSpacing(10)
        self._btn_row.addStretch(1)
        col.addLayout(self._btn_row)
        self.setStyleSheet(_CARD_QSS)

    def done(self, result):
        if self._finished:  # 防二次发射 finished
            return
        self._finished = True
        super().done(result)

    def add_button(self, text: str, kind: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName(_BTN_KIND[kind])
        self._btn_row.addWidget(btn)
        return btn

    def place_center(self, host=None, offset: QPoint | None = None):
        self._card.adjustSize()
        size = self._card.size()
        self.resize(size)
        self._card.setGeometry(0, 0, size.width(), size.height())
        if host is not None:
            g = host.frameGeometry()
            x = g.x() + (g.width() - size.width()) // 2
            y = g.y() + (g.height() - size.height()) // 2
        else:
            scr = QGuiApplication.primaryScreen().availableGeometry()
            x = scr.x() + (scr.width() - size.width()) // 2
            y = scr.y() + (scr.height() - size.height()) // 3
        if offset is not None:
            x += offset.x()
            y += offset.y()
        self.move(self._clamped(QPoint(x, y)))

    def refit(self):
        self._card.layout().activate()
        self._card.adjustSize()
        size = self._card.size()
        self.resize(size)
        self._card.setGeometry(0, 0, size.width(), size.height())
        self.move(self._clamped(self.pos()))

    def _clamped(self, pos: QPoint) -> QPoint:
        return clamp_rect(pos, self.width(), self.height())

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._drag_off = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_off is not None and (ev.buttons() & Qt.MouseButton.LeftButton):
            self.move(self._clamped(ev.globalPosition().toPoint() - self._drag_off))
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_off = None
        super().mouseReleaseEvent(ev)


NOTE_MAX_HEIGHT = 170


class ConfirmDialog(CardDialog):
    """确认弹窗（Accepted=确认）；note 是附在正文下的一段原文；cancel_btn 供外部程序化点击。"""

    def __init__(
        self,
        title: str,
        text: str,
        confirm_text: str,
        cancel_text: str,
        parent=None,
        width: int = 300,
        note: str | None = None,
    ):
        super().__init__(title, parent, width)
        self._note = None
        lbl = QLabel(text)
        lbl.setObjectName("cardText")
        lbl.setWordWrap(True)
        self.body.addWidget(lbl)
        if note is not None:
            nl = QPlainTextEdit(note)
            nl.setObjectName("cardNote")
            nl.setReadOnly(True)
            nl.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            self._note = nl
            nl.setFixedHeight(40)
            self.body.addWidget(nl)
        self.confirm_btn = self.add_button(confirm_text, "danger")
        self.confirm_btn.clicked.connect(self.accept)
        self.cancel_btn = self.add_button(cancel_text, "primary")
        self.cancel_btn.setDefault(True)
        self.cancel_btn.clicked.connect(self.reject)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.cancel_btn.setFocus()
        if self._note is not None:
            self._fit_note()

    def _fit_note(self):
        """按换行后的文字高度定文字区高度，最高 NOTE_MAX_HEIGHT，并让弹窗跟着变。"""
        n = self._note
        n.ensurePolished()
        extra = n.height() - n.viewport().height()
        doc = QTextDocument()
        doc.setDefaultFont(n.font())
        doc.setDefaultTextOption(n.document().defaultTextOption())
        doc.setDocumentMargin(n.document().documentMargin())
        doc.setPlainText(n.toPlainText())
        doc.setTextWidth(n.viewport().width())
        n.setFixedHeight(min(NOTE_MAX_HEIGHT, int(doc.size().height()) + extra + 8))
        self.refit()


class PickDialog(CardDialog):
    """选择弹窗；Accepted 后读 selected_index()。"""

    def __init__(
        self,
        title: str,
        label: str,
        items: list,
        ok_text: str,
        cancel_text: str,
        parent=None,
        current: int = 0,
        width: int = 320,
    ):
        super().__init__(title, parent, width)
        lbl = QLabel(label)
        lbl.setObjectName("cardText")
        lbl.setWordWrap(True)
        self.body.addWidget(lbl)
        self._group = QButtonGroup(self)
        for i, item in enumerate(items):
            rb = QRadioButton(item)
            rb.setChecked(i == current)
            self._group.addButton(rb, i)
            self.body.addWidget(rb)
        self.cancel_btn = self.add_button(cancel_text, "ghost")
        self.cancel_btn.clicked.connect(self.reject)
        self.ok_btn = self.add_button(ok_text, "primary")
        self.ok_btn.setDefault(True)
        self.ok_btn.clicked.connect(self.accept)

    def selected_index(self) -> int:
        return max(0, self._group.checkedId())

"""设置窗：左栏猫、外观、环境，右栏交互、性能、声音、系统；关窗即写盘。"""

from __future__ import annotations
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QFrame,
    QDialog,
    QComboBox,
    QLineEdit,
    QScrollArea,
    QSlider,
)
from PySide6.QtCore import Qt, QTimer, Signal

from ...characters import REGISTRY, all_variants
from ... import i18n
from ...i18n import t
from ..._paths import resource_dir
from ...window import MAX_PETS
from .catmenu import variant_label, pet_label
from .personality import open_personality
from ..widgets.dialogs import ConfirmDialog, PickDialog
from ..theme import qss
from ...platform import autostart


_CHECK = (resource_dir() / "icons" / "check.svg").as_posix()  # 缺 QtSvg 时退化为高亮块

_QSS = qss.settings_window(_CHECK)
CAT_ROWS_SHOWN = 6  # 猫列表超过这么多行就在自身区域里滚动


class _Combo(QComboBox):
    """悬停滚轮不改选项，交给外层滚动。"""

    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class _Slider(QSlider):
    def __init__(self, lo, hi, value):
        super().__init__(Qt.Orientation.Horizontal)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setRange(lo, hi)
        self.setValue(value)
        self.setFixedWidth(120)

    def wheelEvent(self, e):
        if self.hasFocus():
            super().wheelEvent(e)
        else:
            e.ignore()


class _NameEdit(QLineEdit):
    """猫名输入：回车确认，Esc 撤回。"""

    def __init__(self, text):
        super().__init__(text)
        self.committed = text

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.setText(self.committed)
            self.clearFocus()
            return
        super().keyPressEvent(e)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clearFocus()


class SettingsWindow(QWidget):
    visible_changed = Signal(bool)

    def __init__(self, window, hud, write_state):
        super().__init__()
        self._window = window
        self._hud = hud
        self._write_state = write_state
        window._settings_panel = self  # 供 window 反向同步世界态
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle(t("settings_title"))
        self.setStyleSheet(_QSS)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._outer.addWidget(self._scroll)
        self._body = None
        self._dlg = None  # 弹窗单例守卫
        self._roster = None  # 当前显示的 (猫, 是否收起)
        self._cats_area = None
        self._rebuild()

    def open(self):
        self._window.reload_skins(force=False)
        self._rebuild()
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, ev):
        self._write_state()
        super().closeEvent(ev)

    def showEvent(self, ev):
        super().showEvent(ev)
        self.refresh_fps()  # 隐藏期间档位可能变了
        self.visible_changed.emit(True)

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self.visible_changed.emit(False)

    def _rebuild(self):
        cats_pos = self._cats_area.verticalScrollBar().value() if self._cats_area else 0
        self._cats_area = None
        old = self._scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self._body = QWidget()
        cols = QHBoxLayout(self._body)
        cols.setContentsMargins(16, 14, 16, 14)
        cols.setSpacing(16)
        left = self._column(cols)
        cols.addWidget(self._vdivider())
        right = self._column(cols)
        from .appearance import build_skin_section

        for v, build in (
            (left, self._section_cats),
            (left, lambda v: build_skin_section(self, v)),
            (left, self._section_env),
            (right, self._section_interact),
            (right, self._section_perf),
            (right, self._section_sound),
            (right, self._section_system),
        ):
            if v.count():
                v.addSpacing(8)
            build(v)
        left.addStretch(1)
        right.addStretch(1)
        self._scroll.setWidget(self._body)
        self._roster = self._roster_now()
        self._fit()
        if self._cats_area is not None and cats_pos:
            area = self._cats_area
            QTimer.singleShot(0, lambda: area.verticalScrollBar().setValue(cats_pos))

    def _roster_now(self):
        return tuple((id(p), p.hidden) for p in self._window.all_pets())

    def pets_changed(self):
        if self.isVisible():
            QTimer.singleShot(0, self._refresh_roster)

    def _refresh_roster(self):
        if self.isVisible() and self._roster != self._roster_now():
            self._rebuild()

    @staticmethod
    def _column(cols):
        v = QVBoxLayout()
        v.setSpacing(8)
        cols.addLayout(v, 1)
        return v

    def _fit(self):
        hint = self._body.sizeHint()
        screen = self.screen() or self._window.screen()
        h = hint.height()
        if screen is not None:
            h = min(h, screen.availableGeometry().height() - 80)
        bar = self._scroll.verticalScrollBar().sizeHint().width() if h < hint.height() else 0
        self.resize(hint.width() + bar, h)

    @staticmethod
    def _header(text):
        lbl = QLabel(text)
        lbl.setObjectName("secHeader")
        return lbl

    @staticmethod
    def _vdivider():
        f = QFrame()
        f.setFixedWidth(1)
        f.setStyleSheet(qss.DIVIDER_LINE)
        return f

    @staticmethod
    def _row(v, label_key, widget):
        row = QHBoxLayout()
        row.addWidget(QLabel(t(label_key)))
        row.addStretch(1)
        row.addWidget(widget)
        v.addLayout(row)

    def _range(self, row, lo, hi, value, fmt, apply):
        """两端标着上下限的滑条，拖动即时生效，松手写盘。"""
        sl = _Slider(lo, hi, value)
        val = QLabel(fmt(value))
        val.setObjectName("rangeValue")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        for text in (fmt(lo), None, fmt(hi)):
            if text is None:
                row.addWidget(sl)
                continue
            end = QLabel(text)
            end.setObjectName("rangeEnd")
            row.addWidget(end)
        row.addWidget(val)

        def changed(n):
            val.setText(fmt(n))
            apply(n)
            if not sl.isSliderDown():
                self._write_state()

        sl.valueChanged.connect(changed)
        sl.sliderReleased.connect(self._write_state)
        return sl

    def _section_cats(self, v):
        v.addWidget(self._header(t("settings_cats_section")))
        w = self._window
        pets = w.all_pets()
        self._vis_btns = []
        rows = QWidget()
        rows_v = QVBoxLayout(rows)
        rows_v.setContentsMargins(0, 0, 0, 0)
        rows_v.setSpacing(v.spacing())
        for pet in pets:
            row = QHBoxLayout()
            row.setSpacing(8)
            name = _NameEdit(pet.name)
            name.setPlaceholderText(pet_label(pet, pets, use_name=False))
            name.setMaxLength(24)
            name.setMinimumWidth(110)
            name.editingFinished.connect(lambda p=pet, e=name: self._on_rename(p, e))
            row.addWidget(name, 1)
            sk = QPushButton(t("look_button"))
            sk.clicked.connect(lambda _c, p=pet: self._on_look(p))
            row.addWidget(sk)
            ps = QPushButton(t("pers_button"))
            ps.clicked.connect(lambda _c, p=pet: open_personality(p, pets, parent=self))
            row.addWidget(ps)
            if pet.hidden:
                vis = QPushButton(t("settings_show_pet"))
                vis.clicked.connect(lambda _c, p=pet: w.show_pet(p) and self._rebuild())
            else:
                vis = QPushButton(t("settings_hide_pet"))
                vis.clicked.connect(lambda _c, p=pet: w.hide_pet(p) and self._rebuild())
            fm = vis.fontMetrics()
            vis.setMinimumWidth(
                max(fm.horizontalAdvance(t(k)) for k in ("settings_hide_pet", "settings_show_pet")) + 28
            )
            row.addWidget(vis)
            rm = QPushButton(t("settings_remove"))
            rm.clicked.connect(lambda _c, p=pet: self._on_remove(p))
            row.addWidget(rm)
            self._vis_btns.append((pet, vis, rm))
            rows_v.addLayout(row)
        if len(pets) > CAT_ROWS_SHOWN:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setWidget(rows)
            gap = rows_v.spacing()
            row_h = (rows.sizeHint().height() - gap * (len(pets) - 1)) / len(pets)
            area.setFixedHeight(round(row_h * CAT_ROWS_SHOWN + gap * (CAT_ROWS_SHOWN - 1)))
            area.setMinimumWidth(rows.sizeHint().width() + area.verticalScrollBar().sizeHint().width())
            self._cats_area = area
            v.addWidget(area)
        else:
            v.addWidget(rows)
        row = QHBoxLayout()
        self._add_btn = QPushButton(t("settings_add"))
        self._add_btn.clicked.connect(self._on_add)
        row.addWidget(self._add_btn)
        row.addStretch(1)
        row.addWidget(QLabel(t("settings_max_pets_label")))
        self._range(row, 1, MAX_PETS, w.cfg_max_pets, str, self._on_max_pets)
        v.addLayout(row)
        self._update_cat_buttons()

    def _update_cat_buttons(self):
        w = self._window
        on_stage = len(w.pets)
        full = on_stage >= w.cfg_max_pets
        self._add_btn.setEnabled(not full)
        self._add_btn.setToolTip(t("settings_max_pets") if full else "")
        for pet, vis, rm in self._vis_btns:
            vis.setEnabled(not full if pet.hidden else on_stage > 1)
            can_remove = pet.hidden or on_stage > 1
            rm.setEnabled(can_remove)
            rm.setToolTip("" if can_remove else t("settings_min_pets"))

    def _on_rename(self, pet, edit):
        edit.committed = edit.text()
        if edit.text().strip() != pet.name:
            self._window.rename_pet(pet, edit.text())  # 内部写盘

    def _on_max_pets(self, n):
        self._window.set_max_pets(n)
        self._update_cat_buttons()

    def _section_env(self, v):
        v.addWidget(self._header(t("settings_env_section")))
        grp = QButtonGroup(self._body)
        self._env_group = grp
        self._env_radios = {}
        current = self._current_env()
        grid = QGridLayout()
        grid.setSpacing(6)
        for i, (key, label_key) in enumerate(
            (
                ("none", "settings_env_none"),
                ("blizzard", "settings_snow"),
                ("zerog", "settings_zerog"),
                ("water", "settings_water"),
                ("rain", "settings_rain"),
            )
        ):
            rb = QRadioButton(t(label_key))
            rb.setChecked(key == current)
            rb.toggled.connect(lambda checked, k=key: checked and self._on_env_selected(k))
            grp.addButton(rb)
            grid.addWidget(rb, i // 2, i % 2)
            self._env_radios[key] = rb
        v.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(QLabel(t("settings_water_depth")))
        row.addStretch(1)
        depth = self._range(
            row,
            5,
            95,
            round(self._window.cfg_water_depth * 100),
            lambda n: f"{n}%",
            lambda n: self._window.set_water_depth(n / 100.0),
        )
        depth.setSingleStep(5)
        depth.setPageStep(10)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel(t("settings_rain_amount")))
        row.addStretch(1)
        amount = self._range(
            row,
            5,
            100,
            round(self._window.cfg_rain_amount * 100),
            lambda n: f"{n}%",
            lambda n: self._window.set_rain_amount(n / 100.0),
        )
        amount.setSingleStep(5)
        amount.setPageStep(10)
        v.addLayout(row)
        w = self._window
        self._check(
            v,
            "settings_rain_window_block",
            w.cfg_rain_window_block,
            lambda on: (w.set_rain_window_block(on), self._write_state()),
        )
        fs = _Combo()
        fs.addItem(t("settings_fs_hide"), "hide")
        fs.addItem(t("settings_fs_sink"), "sink")
        fs.setCurrentIndex(max(0, fs.findData(w.cfg_fullscreen_mode)))
        fs.currentIndexChanged.connect(
            lambda _i: (w.set_fullscreen_mode(fs.currentData()), self._write_state())
        )
        self._row(v, "settings_fullscreen", fs)

    def _current_env(self):
        w = self._window
        if getattr(w, "blizzard_on", False):
            return "blizzard"
        if getattr(w, "zerog_on", False):
            return "zerog"
        if getattr(w, "water_on", False):
            return "water"
        if getattr(w, "rain_on", False):
            return "rain"
        return "none"

    def _on_env_selected(self, key):
        """切到 key 对应的环境并关掉其余，none 为全关。"""
        w = self._window
        if key != "blizzard" and getattr(w, "blizzard_on", False):
            w.set_blizzard(False)
        if key != "zerog" and getattr(w, "zerog_on", False):
            w.set_zerog(False)
        if key != "water" and getattr(w, "water_on", False):
            w.set_water(False)
        if key != "rain" and getattr(w, "rain_on", False):
            w.set_rain(False)
        if key == "blizzard" and not w.blizzard_on:
            w.set_blizzard(True)
        elif key == "zerog" and not w.zerog_on:
            w.set_zerog(True)
        elif key == "water" and not w.water_on:
            w.set_water(True)
        elif key == "rain" and not w.rain_on:
            w.set_rain(True)

    def _check(self, v, key, on, slot):
        chk = QCheckBox(t(key))
        chk.setChecked(on)
        if slot is not None:
            chk.toggled.connect(slot)
        v.addWidget(chk)
        return chk

    def _section_interact(self, v):
        v.addWidget(self._header(t("settings_interact_section")))
        w = self._window
        self._check(
            v,
            "settings_cursor_hijack",
            w.cfg_cursor_hijack,
            lambda on: (w.set_cursor_hijack(on), self._write_state()),
        )
        self._check(
            v,
            "settings_cursor_shape",
            w.cfg_cursor_shape,
            lambda on: (w.set_cursor_shape(on), self._write_state()),
        )
        self._check(
            v,
            "settings_friendly_fire",
            w.cfg_friendly_fire,
            lambda on: (w.set_friendly_fire(on), self._write_state()),
        )
        self._check(
            v,
            "settings_freeze_hide_ui",
            w.cfg_freeze_hide_ui,
            lambda on: (w.set_freeze_hide_ui(on), self._write_state()),
        )
        btn = QPushButton(t("settings_keybinds_btn"))
        btn.clicked.connect(self._on_keybinds)
        self._row(v, "settings_keybinds", btn)
        self._check(
            v,
            "settings_keep_items",
            w.cfg_keep_items,
            lambda on: (w.set_keep_items(on), self._write_state()),
        )
        self._check(
            v,
            "settings_fruit_regrow",
            w.cfg_fruit_regrow,
            lambda on: (w.set_fruit_regrow(on), self._write_state()),
        )
        btn = QPushButton(t("settings_keybinds_btn"))
        btn.clicked.connect(self._on_blueprints)
        self._row(v, "settings_blueprints", btn)

    def _section_perf(self, v):
        v.addWidget(self._header(t("settings_perf_section")))
        w = self._window
        combo = _Combo()
        combo.addItem(t("settings_fps_auto", hz=round(w.auto_fps())), "auto")
        for fps in (30, 60, 90, 120, 144, 165, 240):
            combo.addItem(f"{fps} FPS", fps)
        combo.setCurrentIndex(max(0, combo.findData(w.cfg_fps)))
        combo.currentIndexChanged.connect(
            lambda _i: (w.set_fps(combo.currentData()), self.refresh_fps(), self._write_state())
        )
        self._row(v, "settings_fps", combo)
        self._fps_combo = combo
        self._check(
            v, "settings_power_save", w.cfg_power_save, lambda on: (w.set_power_save(on), self._write_state())
        )

    def _section_system(self, v):
        v.addWidget(self._header(t("settings_sys_section")))
        w = self._window
        lang = _Combo()
        for code in ("zh", "en"):
            lang.addItem(i18n.lang_name(code), code)
        lang.setCurrentIndex(max(0, lang.findData(i18n.LANG)))
        lang.currentIndexChanged.connect(lambda _i: w.set_lang(lang.currentData()))
        self._row(v, "settings_lang", lang)
        auto = self._check(v, "settings_autostart", autostart.is_enabled(), None)
        auto.toggled.connect(lambda on: self._on_autostart(auto, on))
        self._check(v, "settings_show_hud", self._hud.isVisible(), self._on_hud_toggled)
        dial = getattr(w, "_rain_dial", None)
        if dial is not None:
            self._dial_chk = self._check(v, "settings_show_dial", dial.wanted, dial.set_wanted)

    @staticmethod
    def _on_autostart(chk, on):
        autostart.set_enabled(on)
        if autostart.is_enabled() != on:  # 写注册表失败就回到真实状态
            chk.blockSignals(True)
            chk.setChecked(not on)
            chk.blockSignals(False)

    def _section_sound(self, v):
        w = self._window
        player = getattr(w, "_music_player", None)
        if player is None:
            return
        v.addWidget(self._header(t("settings_sound_section")))
        self._check(v, "settings_mute_all", player.muted, player.set_mute)
        amb = getattr(w, "_rain_ambience", None)
        if amb is not None:
            row = QHBoxLayout()
            row.addWidget(QLabel(t("settings_rain_volume")))
            row.addStretch(1)
            vol = self._range(
                row, 0, 100, round(amb.volume * 100), lambda n: f"{n}%", lambda n: amb.set_volume(n / 100.0)
            )
            vol.setSingleStep(5)
            vol.setPageStep(10)
            v.addLayout(row)
        meow = getattr(w, "_meow", None)
        if meow is not None:
            row = QHBoxLayout()
            row.addWidget(QLabel(t("settings_meow_volume")))
            row.addStretch(1)
            vol = self._range(
                row, 0, 100, round(meow.volume * 100), lambda n: f"{n}%", lambda n: meow.set_volume(n / 100.0)
            )
            vol.setSingleStep(5)
            vol.setPageStep(10)
            v.addLayout(row)
            if not meow.has_sounds:
                note = QLabel(t("settings_meow_missing"))
                note.setObjectName("rangeEnd")
                note.setWordWrap(True)
                v.addWidget(note)
        director = getattr(w, "_music_director", None)
        if director is not None:
            btn = QPushButton(t("settings_scene_music_btn"))
            btn.clicked.connect(lambda: self._on_scene_music(director))
            self._row(v, "settings_scene_music", btn)

    def _on_scene_music(self, director):
        if self._dlg is not None:
            return
        from .scenemusic import SceneMusicDialog

        dlg = SceneMusicDialog(director, self._window._music_player, self._write_state, parent=self)
        dlg.finished.connect(lambda _r: self._look_finished(dlg))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _on_keybinds(self):
        if self._dlg is not None:
            return
        from .keybinds import KeybindDialog

        dlg = KeybindDialog(self._keybinds_saved, self._window._hotkey_filter, parent=self)
        dlg.finished.connect(lambda _r: self._look_finished(dlg))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _on_blueprints(self):
        if self._dlg is not None:
            return
        from .blueprints import BlueprintDialog

        dlg = BlueprintDialog(self._window, self._write_state, parent=self)
        dlg.finished.connect(lambda _r: self._look_finished(dlg))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _keybinds_saved(self):
        hud = getattr(self._window, "_control_hud", None)
        if hud is not None:
            hud.reload_keys()
        if self._window._hotkeys_cb is not None:
            self._window._hotkeys_cb()

    # 各弹窗都用卡片弹窗的 open()，不走 exec
    def _on_look(self, pet):
        if self._dlg is not None:
            return
        from .appearance import AppearanceDialog

        dlg = AppearanceDialog(pet, parent=self)
        dlg.finished.connect(lambda _r: self._look_finished(dlg))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _look_finished(self, dlg):
        self._dlg = None
        dlg.deleteLater()
        self._write_state()

    def _on_add(self):
        if self._dlg is not None:
            return
        labels = []
        self._pick_variants = all_variants()
        for variant in self._pick_variants:
            lbl = variant_label(variant)
            if variant in REGISTRY and REGISTRY[variant].wip:
                lbl += t("variant_wip_note")
            labels.append(lbl)
        dlg = PickDialog(
            t("settings_pick_title"),
            t("settings_pick_cat"),
            labels,
            t("settings_ok"),
            t("settings_cancel"),
            parent=self,
        )
        dlg.finished.connect(lambda r, d=dlg: self._add_finished(d, r))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _add_finished(self, dlg, result):
        self._dlg = None
        dlg.deleteLater()
        if result == QDialog.DialogCode.Accepted:
            self._window.add_pet(self._pick_variants[dlg.selected_index()])  # 里面会刷新 HUD 并写盘
            self._rebuild()

    def _on_remove(self, pet):
        if self._dlg is not None:
            return
        name = pet_label(pet, self._window.all_pets())
        dlg = ConfirmDialog(
            t("settings_remove"),
            t("settings_remove_confirm", name=name),
            t("settings_remove"),
            t("settings_cancel"),
            parent=self,
        )
        dlg.finished.connect(lambda r, p=pet, d=dlg: self._remove_finished(p, d, r))
        self._dlg = dlg
        dlg.place_center(self)
        dlg.open()

    def _remove_finished(self, pet, dlg, result):
        self._dlg = None
        dlg.deleteLater()
        if result == QDialog.DialogCode.Accepted and self._window.remove_pet(pet):
            self._rebuild()

    def _on_hud_toggled(self, checked):
        if checked != self._hud.isVisible():
            self._hud.toggle_visible()

    def refresh_fps(self):
        """自动项显示当前实际档位。"""
        combo = getattr(self, "_fps_combo", None)
        if combo is not None and self.isVisible():
            combo.setItemText(0, t("settings_fps_auto", hz=round(self._window.auto_fps())))

    def refresh_dial(self, on):
        """表盘常驻开关在别处改了，勾选框跟上。"""
        chk = getattr(self, "_dial_chk", None)
        try:
            if chk is not None and chk.isChecked() != on:
                chk.blockSignals(True)
                chk.setChecked(on)
                chk.blockSignals(False)
        except RuntimeError:  # 重建后旧勾选框已销毁
            self._dial_chk = None

    def refresh_env(self):
        if not self.isVisible():
            return
        radios = getattr(self, "_env_radios", None)
        if not radios:
            return
        rb = radios.get(self._current_env())
        if rb is not None and not rb.isChecked():
            rb.setChecked(True)  # 单选组自动取消其余项

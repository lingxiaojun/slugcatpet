"""启动应用，管理 PetWindow、TabBar、托盘、全局热键。"""

from __future__ import annotations
import sys
import os
import json
import signal
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtCore import QPoint, QRect, QSize, QTimer

from .characters import default_def
from .window import PetWindow
from .ui.panels.tabbar import TabBar
from .ui.panels.hud import HudPanel
from .ui.panels.settings import SettingsWindow
from .ui.raintimer import RainCard, RainDial, tray_icon
from .ui.raintimer.text import status_line
from .ui.raintimer.tray import arc_key
from .world.raintimer import IDLE, RainTimer
from .control.hotkey import (
    FROZEN_KEYS,
    GLOBAL_KEYS,
    HK_FREEZE,
    HK_HUD,
    HK_MEOW,
    HK_PLACE_ESC,
    HK_QUIT,
    HK_RAIN,
    HK_STEP,
    HK_STEP10,
    HotkeyFilter,
    combo_text,
    key_down,
    load_hotkeys,
)
from .platform import cursorfx
from .platform.topmost import own_by
from ._paths import user_dir
from .i18n import t

_APP_PARAMS = user_dir() / "app.json"
SCHEMA_VERSION = 2  # pets[] 分猫存档
AUTOSAVE_MS = 60_000


def _tray_icon(frozen=False, timer=None) -> QIcon:
    """托盘图标；计时中带进度弧，定格时右下角加暂停角标。"""
    return tray_icon(default_def().body_color, frozen, timer)


def _migrate_params(params: dict) -> dict:
    """旧 schema 迁移为 v2。"""
    if params.get("schema_version") == SCHEMA_VERSION and isinstance(params.get("pets"), list):
        return params
    from . import tuning

    old_keys = ("energy", "temper", "food", "karma", "cold", "dead")
    if any(k in params for k in old_keys) or not params.get("pets"):
        pet_state = {
            "id": "pet-0",
            "variant": "saint",
            "energy": params.pop("energy", 1.0),
            "temper": params.pop("temper", 0.0),
            "food": params.pop("food", tuning.FOOD_INIT),
            "karma": params.pop("karma", tuning.KARMA_INIT),
            "cold": params.pop("cold", 0.0),
            "dead": params.pop("dead", False),
        }
        params["pets"] = [pet_state]
    params["schema_version"] = SCHEMA_VERSION
    return params


_DROPPED_KEYS = (
    "music_water_enabled",
    "music_hud_enabled",
    "music_theme_loop",
    "music_hud_ambient_ms",
    "music_hud_ambient_ts",
)  # 已弃用的设置键，载入时丢弃


def _load_params(path: Path | None = None) -> dict:
    p = path or _APP_PARAMS
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    for k in _DROPPED_KEYS:
        raw.pop(k, None)
    return _migrate_params(raw)


def _save_params(params: dict, path: Path | None = None):
    """原子写盘，失败重试。"""
    p = path or _APP_PARAMS
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.01)
    except Exception as e:
        print("[slugcatpet] save failed:", ascii(e), file=sys.stderr)


def setup_signal_handling(app):
    def _h(sig, frame):
        app.quit()

    signal.signal(signal.SIGINT, _h)
    t = QTimer()
    t.timeout.connect(lambda: None)
    t.start(500)  # 定时唤醒解释器，Ctrl+C 才能及时处理
    return t


def _ensure_assets_interactive() -> bool:
    from .gameassets import atlases_present
    from ._paths import assets_dir

    if atlases_present(assets_dir()):
        return True
    from .ui.panels.setup_panel import SetupPanel

    return SetupPanel().run()


def _extract_scene_music_later(names):
    """缺游戏配乐且找得到游戏时在后台补提取；这次启动没赶上的下次就有。"""
    from .gameassets import detect_install, extract_music, music_missing, prune_music

    prune_music(names)
    if not music_missing(names):
        return

    def run():
        try:
            install = detect_install()
            if install is not None:
                extract_music(install, names)
        except Exception:
            pass

    import threading

    threading.Thread(target=run, name="scene-music", daemon=True).start()


def _extract_rain_later():
    """缺雨和业力梯的素材且找得到游戏时在后台补提取。"""
    from .gameassets import (
        detect_install,
        extract_dial_sounds,
        extract_rain_atlas,
        extract_rain_sounds,
        dial_sounds_missing,
        rain_atlas_missing,
        rain_sounds_missing,
    )

    atlas, sounds, karma = rain_atlas_missing(), bool(rain_sounds_missing()), bool(dial_sounds_missing())
    if not (atlas or sounds or karma):
        return

    def run():
        try:
            install = detect_install()
            if install is None:
                return
            if atlas:
                extract_rain_atlas(install)
            if sounds:
                extract_rain_sounds(install)
            if karma:
                extract_dial_sounds(install)
        except Exception:
            pass

    import threading

    threading.Thread(target=run, name="rain-assets", daemon=True).start()


def _freeze_heap():
    """把启动时建好的对象移出分代回收。"""
    import gc

    gc.collect()
    gc.freeze()


def _setup_log():
    import logging
    from logging.handlers import RotatingFileHandler

    h = RotatingFileHandler(
        user_dir() / "slugcatpet.log", maxBytes=1 << 20, backupCount=1, encoding="utf-8", delay=True
    )
    h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logging.getLogger("slugcatpet").addHandler(h)


def main():
    from .platform.appid import set_app_id

    set_app_id()  # 须在建任何窗口之前
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from .platform.singleton import SingleInstance

    si = SingleInstance()
    if not si.acquire():
        si.notify_primary()
        return
    app._singleton = si  # 保持引用
    _setup_log()

    from .platform import autostart

    autostart.sync_path()

    _sig = setup_signal_handling(app)  # 保持引用

    params = _load_params()
    from . import i18n

    i18n.set_lang(params.get("lang"))
    params.setdefault("lang", i18n.LANG)  # 首次启动按系统语言定下，之后不随系统变

    if not _ensure_assets_interactive():
        return

    from . import skins
    from .items import scene

    skins.configure(params.get("skins_scan_external", True))
    skins.reload()
    from .skins.folder import ensure_folder

    ensure_folder()  # 不导出模板

    pet = PetWindow(params=params)
    pet.show()

    hud = HudPanel(pet, params)
    own_by(pet, hud)
    if params.get("hud_visible"):
        hud.toggle_visible()

    tab = TabBar(pet, app, params)
    own_by(pet, tab)
    tab.show()

    def _write_state():
        params["tab_y"] = tab._y
        params["tab_expanded"] = tab.expanded
        params["hud_x"] = hud.x()
        params["hud_y"] = hud.y()
        params["pets"] = [
            {
                "id": p.id,
                "variant": p.variant,
                **p.look_state(),
                "name": p.name,
                "personality": p.personality,
                "hidden": p.hidden,
                "energy": p.body.energy,
                "temper": p.body.temper,
                "food": p.body.food,
                "food_q": p.body.food_q,
                "karma": p.body.karma,
                "cold": p.body.cold,
                "sleepiness": p.body.sleepiness,
                "dead": p.behavior is not None and p.behavior.is_truly_dead(),
            }
            for p in pet.all_pets()
        ]
        params["bonds"] = pet.social.bonds.dump()
        if pet.cfg_keep_items:
            params["scene_last"] = scene.capture(pet)
        else:
            params.pop("scene_last", None)
        params["skins_scan_external"] = skins.scan_external()
        params["schema_version"] = SCHEMA_VERSION
        _save_params(params)

    settings = SettingsWindow(pet, hud, _write_state)
    pet._hud = hud  # 供增删猫后 rebuild_rows
    pet._pets_changed_cb = _write_state
    pet._open_settings_cb = settings.open
    app._settings = settings

    if params.get("tab_expanded", True):
        tab.toggle()

    def _watch_skins():
        if pet.reload_skins(force=False) and settings.isVisible():
            settings._rebuild()

    skin_timer = QTimer()
    skin_timer.timeout.connect(_watch_skins)
    skin_timer.start(4000)
    app._skin_timer = skin_timer

    rain_cfg = params.setdefault("rain_timer", {})
    rain_timer = RainTimer(rain_cfg)
    tray = QSystemTrayIcon(_tray_icon(timer=rain_timer))
    keys = load_hotkeys()

    def _kt(action):
        return combo_text(*keys[action])

    menu = QMenu()

    # 音乐：播放列表 = 音乐目录 + 导入的歌
    from .audio import MusicDirector, MusicPlayer
    from .audio.director import GAME_TRACKS
    from .ui.panels.musicpanel import MusicPanel
    from ._paths import music_asset_dirs, user_music_dir

    def _save_music_state():
        params["music_mode"] = player.mode
        params["music_volume"] = player.volume
        params["mute_all"] = player.muted
        params["music_tracks"] = [str(tr.path) for tr in player.tracks]
        params["music_hidden"] = player.hidden
        params["music_sources"] = player.sources
        cur = player.current()
        if cur is not None:
            params["music_last_track"] = str(cur.path)
            params["music_last_ms"] = 0 if player.is_special_active else player.position()
        _save_params(params)

    player = MusicPlayer(params, on_change=_save_music_state, theme_dirs=music_asset_dirs())
    _extract_scene_music_later(GAME_TRACKS)
    _extract_rain_later()
    saved_tracks = params.get("music_tracks")
    if isinstance(saved_tracks, list):
        player.add_paths([p for p in saved_tracks if Path(p).is_file()], record=False)
    player.add_paths([user_music_dir()])
    player.restore_state(params.get("music_last_track"), int(params.get("music_last_ms", 0) or 0))
    director = MusicDirector(player, params)
    pet.env_listeners.append(director.on_env)
    settings.visible_changed.connect(director.on_settings)
    music = MusicPanel(player, _write_state)
    pet._open_music_cb = music.open
    pet._music_player = player
    pet._music_director = director
    from .audio.ambience import RainAmbience

    pet._rain_ambience = RainAmbience(pet, player, params)
    from .meow import MeowService

    pet._meow = MeowService(pet, params, muted=lambda: player.muted)
    app._music = (player, director, music)

    def _dial_context():
        if pet.frozen and pet.cfg_freeze_hide_ui:
            return "frozen"
        return "env" if pet.env_hidden else None

    dial = RainDial(rain_timer, pet.atlas, rain_cfg, muted=lambda: player.muted, context=_dial_context)
    own_by(pet, dial)
    pet._rain_dial = dial
    dial.on_wanted.append(settings.refresh_dial)

    def _open_rain(anchor=None):
        """侧边栏按钮旁或托盘旁打开雨循环卡片。"""
        if anchor is not None:
            rect = QRect(anchor.mapToGlobal(QPoint(0, 0)), anchor.size())
            RainCard.open_for(rain_timer, dial, rect, ("left", "right"))
            return
        rect = tray.geometry()
        if not rect.isValid() or rect.isEmpty():
            rect = QRect(QCursor.pos(), QSize(1, 1))
        RainCard.open_for(rain_timer, dial, rect, ("above", "below", "left", "right"))

    pet._open_rain_cb = _open_rain
    tray_look = [arc_key(rain_timer), pet.frozen]

    def _refresh_tray():
        """进度弧变了才换图标；提示每秒跟着倒计时走。"""
        look = [arc_key(rain_timer), pet.frozen]
        if look != tray_look:
            tray_look[:] = look
            tray.setIcon(_tray_icon(pet.frozen, rain_timer))
        if pet.frozen:
            tray.setToolTip(t("tray_frozen_tip", combo=_kt("freeze")))
        elif rain_timer.phase != IDLE:
            tray.setToolTip(status_line(rain_timer))
        else:
            tray.setToolTip(t("app_title"))

    dial.on_poll.append(_refresh_tray)

    def _sync_cycle_rain():
        """让桌面上的雨跟上雨循环的时段。"""
        level = rain_timer.rain_level()
        if level != pet.cycle_rain:
            pet.set_cycle_rain(level)
            director.refresh()

    def _on_rain_timer(ev):
        _sync_cycle_rain()
        if ev in ("start", "rain", "clear", "stop", "extend", "lost"):
            _save_params(params)

    rain_timer.listeners.append(_on_rain_timer)
    director.env_muted = lambda kind: (
        kind == "rain" and pet.cycle_rain is not None and not rain_timer.music_worthy()
    )
    pet._cycle_rain_stopped_cb = lambda: rain_timer.stop_rain()
    _sync_cycle_rain()

    def _start_scene_music():
        env = next(
            (k for k in ("blizzard", "zerog", "water", "rain") if getattr(pet, f"{k}_on", False)), "none"
        )
        director.start(env)

    QTimer.singleShot(300, _start_scene_music)

    def _build_tray_menu():
        menu.clear()
        menu.addAction(t("btn_rain_cycle"), _open_rain)
        menu.addAction(t("tray_settings"), settings.open)
        menu.addAction(t("tray_music"), music.open)
        freeze_act = menu.addAction(t("tray_freeze", combo=_kt("freeze")))
        freeze_act.setCheckable(True)
        freeze_act.setChecked(pet.frozen)
        freeze_act.triggered.connect(lambda on: pet.set_frozen(on))
        menu.addAction(t("tray_hud", combo=_kt("hud")), hud.toggle_visible)
        menu.addAction(t("refresh_top"), pet.refresh_top)
        menu.addAction(t("tray_abort", combo=_kt("abort")), cursorfx.abort_all)
        menu.addSeparator()
        other = "en" if i18n.LANG == "zh" else "zh"
        menu.addAction(i18n.lang_name(other), lambda: _set_lang(other))
        menu.addSeparator()
        menu.addAction(t("tray_quit"), app.quit)

    def _set_lang(lang):
        if lang == i18n.LANG:
            return
        i18n.set_lang(lang)
        params["lang"] = lang
        tab.rebuild_ui()
        hud.rebuild_rows()
        settings._rebuild()
        music._rebuild()
        _build_tray_menu()
        from .skins.folder import ensure_folder, write_report

        ensure_folder()
        write_report()
        _write_state()

    pet._set_lang_cb = _set_lang
    _build_tray_menu()
    _refresh_tray()
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: _open_rain() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
    )
    tray.show()
    started = t("tray_started", freeze=_kt("freeze"), abort=_kt("abort"), quit=_kt("quit"))
    tray.showMessage(t("app_title"), started, QSystemTrayIcon.MessageIcon.Information, 4000)

    si.on_secondary_attempt = lambda: tray.showMessage(
        t("app_title"), t("already_running"), QSystemTrayIcon.MessageIcon.Information, 4000
    )

    hotkey = HotkeyFilter()
    hotkey.install(app)

    def _register(table):
        for hk_id, action in table.items():
            hotkey.register(hk_id, *keys[action])

    _register(GLOBAL_KEYS)
    pet._hotkey_filter = hotkey  # 放置模式临时挂/摘 Esc

    def _reload_hotkeys():
        """键位弹窗保存后按新表重挂；先全摘再挂，互换的组合键才不会撞上。"""
        keys.update(load_hotkeys())
        tables = [GLOBAL_KEYS, FROZEN_KEYS] if pet.frozen else [GLOBAL_KEYS]
        for table in tables:
            for hk_id in table:
                hotkey.unregister(hk_id)
        for table in tables:
            _register(table)
        _build_tray_menu()
        _refresh_tray()

    pet._hotkeys_cb = _reload_hotkeys

    def _on_hotkey(hk_id):
        if hk_id == HK_QUIT:
            app.quit()
        elif hk_id == HK_HUD:
            hud.toggle_visible()
        elif hk_id == HK_RAIN:
            dial.peek()
        elif hk_id == HK_PLACE_ESC:
            pet._exit_place_mode()
        elif hk_id == HK_FREEZE:
            pet.set_frozen(not pet.frozen)
        elif hk_id == HK_STEP:
            pet.step_frozen(1)
        elif hk_id == HK_STEP10:
            pet.step_frozen(10)
        elif hk_id == HK_MEOW:
            vk = keys["meow_hover"][1]
            pet._meow.press(pet._meow.target(), lambda: key_down(vk))
        else:
            cursorfx.abort_all()

    hotkey.triggered.connect(_on_hotkey)
    app._hotkey = hotkey

    autosave = QTimer()
    autosave.timeout.connect(_write_state)
    autosave.start(AUTOSAVE_MS)
    app._autosave = autosave

    from .platform.envwatch import EnvironmentWatcher

    envwatch = EnvironmentWatcher(
        [pet, tab, hud, dial], pet, plain=[settings, music]
    )  # 先下后上：界面盖在桌宠层上
    envwatch.start()
    pet._refresh_top_cb = envwatch.force_top
    app._envwatch = envwatch

    hidden_ui = []  # 定格时收起的界面窗口

    def _on_freeze(on):
        """定格开关：挂摘单步热键、换托盘图标、收起或放回界面。"""
        if on:
            _register(FROZEN_KEYS)
            if pet.cfg_freeze_hide_ui:
                hidden_ui[:] = [w for w in (tab, hud, pet._control_hud) if w is not None and w.isVisible()]
                for w in hidden_ui:
                    w.hide()
            if not params.get("freeze_tip_shown"):
                params["freeze_tip_shown"] = True
                tray.showMessage(
                    t("app_title"),
                    t("freeze_tip", step=_kt("step"), combo=_kt("freeze")),
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
        else:
            for hk_id in FROZEN_KEYS:
                hotkey.unregister(hk_id)
            back = [w for w in hidden_ui if w in (tab, hud) or w is pet._control_hud]  # 控制可能已结束
            hidden_ui.clear()
            envwatch.reveal(back)
        _refresh_tray()
        _build_tray_menu()

    pet._freeze_cb = _on_freeze

    def _cleanup():
        pet.shutdown()
        hotkey.uninstall(app)  # 先摘掉原生事件过滤器，免得关停时回调进来崩溃
        cursorfx.abort_all()
        try:
            from .control.mouse import set_passthrough

            if pet._hwnd:
                set_passthrough(pet._hwnd, False)
        except Exception:
            pass
        envwatch.stop()
        from .audio import sfx

        sfx.shutdown()
        autosave.stop()
        _write_state()
        hotkey.unregister_all()

    app.aboutToQuit.connect(_cleanup)

    _freeze_heap()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

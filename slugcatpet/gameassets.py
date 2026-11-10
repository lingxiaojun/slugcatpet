"""从用户本机 Rain World 安装提取图集和场景曲到 ~/.slugcatpet/assets。"""

from __future__ import annotations
import functools
import gc
import re
import sys
from pathlib import Path

from ._paths import assets_dir, bundled_assets_dir, music_asset_dirs, user_dir
from .i18n import t

# 图集名：base + MSC 各含贴图与 UI
ATLASES = ("rainWorld", "rainworldmsc", "uiSprites", "uispritesmsc")
RAIN_ATLAS = "waterSprites"  # 雨的水花，可缺
_ATLAS_REL = Path("RainWorld_Data") / "resources.assets"


class SetupError(RuntimeError):
    """导入失败，message 面向用户、可照做。"""


def atlases_present(d: Path) -> bool:
    return all(
        (d / f).exists()
        for f in (
            "rainWorld",
            "rainWorld.png",
            "rainworldmsc",
            "rainworldmsc.png",
            "uiSprites",
            "uiSprites.png",
            "uispritesmsc",
            "uispritesmsc.png",
        )
    )


def _drive_roots() -> list[Path]:
    if sys.platform == "win32":
        return [Path(f"{c}:\\") for c in "CDEFGHIJKLMNOP" if Path(f"{c}:\\").exists()]
    home = Path.home()
    return [home / "Library/Application Support/Steam", home / ".steam/steam", home / ".local/share/Steam"]


def _steam_root() -> Path | None:
    if sys.platform == "win32":
        try:
            import winreg

            for hive, key, val in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        p = Path(winreg.QueryValueEx(k, val)[0])
                        if p.exists():
                            return p
                except OSError:
                    pass
        except Exception:
            pass
    return None


@functools.cache  # 要探测每个盘符，皮肤监视又每隔几秒用一次
def _steam_libraries() -> list[Path]:
    roots: list[Path] = []
    sr = _steam_root()
    if sr:
        roots.append(sr)
    for d in _drive_roots():
        roots += [d / "Program Files (x86)" / "Steam", d / "Steam", d / "SteamLibrary", d]
    libs: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        if r in seen or not r.exists():
            continue
        seen.add(r)
        libs.append(r)
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            txt = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'"path"\s*"([^"]+)"', txt):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p not in seen and p.exists():
                    seen.add(p)
                    libs.append(p)
    return libs


INSTALL_FILE = "game_path.txt"  # 导入图集时记下游戏目录，手动选的也能再找到


def _saved_install() -> Path | None:
    for d in {assets_dir(), user_dir() / "assets"}:
        try:
            game = Path((d / INSTALL_FILE).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if (game / _ATLAS_REL).exists():
            return game
    return None


def detect_install() -> Path | None:
    saved = _saved_install()
    if saved is not None:
        return saved
    for lib in _steam_libraries():
        game = lib / "steamapps" / "common" / "Rain World"
        if (game / _ATLAS_REL).exists():
            return game
    return None


def extract_atlases(install: Path, dest: Path | None = None) -> Path:
    install = Path(install)
    res = install / _ATLAS_REL
    if not res.exists():
        raise SetupError(t("err_atlas_missing", res=res))
    try:
        import UnityPy
    except ImportError as e:
        raise SetupError(t("err_no_unitypy")) from e

    dest = Path(dest or assets_dir())
    got = _extract_named(UnityPy, res, (*ATLASES, RAIN_ATLAS), dest)

    if got.get("rainWorld") != {"png", "map"}:
        raise SetupError(t("err_base_fail"))
    if got.get("rainworldmsc") != {"png", "map"}:
        raise SetupError(t("err_msc_missing"))
    if got.get("uiSprites") != {"png", "map"}:
        raise SetupError(t("err_ui_fail"))
    if got.get("uispritesmsc") != {"png", "map"}:
        raise SetupError(t("err_uimsc_missing"))
    try:
        (dest / INSTALL_FILE).write_text(str(install.resolve()), encoding="utf-8")
    except OSError:
        pass
    return dest


def _frees_unity(fn):
    """UnityPy 读出的对象互相成环，不立即回收的话整个资源包（音效包上 G）会一直占着内存。"""

    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            return fn(*a, **k)
        finally:
            gc.collect()

    return wrap


@_frees_unity
def _extract_named(UnityPy, res: Path, names, dest: Path) -> dict[str, set[str]]:
    """从资源文件取出这些图集的贴图与帧表，返回 {名: {"png", "map"}}。"""
    dest.mkdir(parents=True, exist_ok=True)
    want = set(names)
    got: dict[str, set[str]] = {}
    for obj in UnityPy.load(str(res)).objects:
        if obj.type.name not in ("Texture2D", "TextAsset"):
            continue
        d = obj.read()
        name = getattr(d, "m_Name", None) or getattr(d, "name", None)
        if name not in want:
            continue
        if obj.type.name == "Texture2D":
            d.image.save(str(dest / f"{name}.png"))
            got.setdefault(name, set()).add("png")
        else:
            raw = d.m_Script if hasattr(d, "m_Script") else d.script
            if isinstance(raw, str):
                raw = raw.encode("utf-8", "surrogateescape")
            (dest / name).write_bytes(raw)
            got.setdefault(name, set()).add("map")
    return got


RAIN_SOUNDS = (
    "shortLightRainLoop",
    "rainHeavy1",
    "rainLightUnderground1",
    "rainMedUnderground1",
    "shortDeathRainLoop",
    "RWTW_RainRumble_LOOP",
    "click1A",
)
_SFX_BUNDLES = ("loadedsoundeffects",)
_TRIED = ".tried"  # 读过资源包仍取不出的片名（首行是 ogg 格式版本），同一版本下不再重读整包


def _tried(d: Path) -> set[str]:
    try:
        ver, *names = (d / _TRIED).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return set()
    return set(names) if ver == str(_ogg_format()) else set()


def _mark_tried(d: Path, names) -> None:
    names = _tried(d) | set(names)
    try:
        (d / _TRIED).write_text("\n".join([str(_ogg_format()), *sorted(names)]), encoding="utf-8")
    except OSError:
        pass


def _sfx_missing(names, d: Path) -> list[str]:
    tried = _tried(d)
    return [n for n in names if n not in tried and not (d / f"{n}.wav").is_file()]


def _extract_sfx(install: Path, todo, out: Path) -> list[str]:
    """从音效资源包取出这些片段、封装成 wav；返回这次写出的片名。"""
    from .audio.fsbogg import fsb_to_wav

    if not todo:
        return []
    install = Path(install)
    out.mkdir(parents=True, exist_ok=True)
    done = []
    for name, fsb in _bundle_clips(install, todo, _SFX_BUNDLES).items():
        try:
            data = fsb_to_wav(fsb)
        except ValueError:
            continue
        tmp = out / f"{name}.wav.part"
        tmp.write_bytes(data)
        tmp.replace(out / f"{name}.wav")
        done.append(name)
    if _bundles_present(install, _SFX_BUNDLES):
        _mark_tried(out, set(todo) - set(done))
    return done


def rain_sounds_dir() -> Path:
    return assets_dir() / "rain"


def rain_sounds_missing() -> list[str]:
    return _sfx_missing(RAIN_SOUNDS, rain_sounds_dir())


def extract_rain_sounds(install: Path) -> list[str]:
    """雨声循环；返回这次写出的片名。"""
    return _extract_sfx(install, rain_sounds_missing(), rain_sounds_dir())


DIAL_SOUNDS = (
    "karmaFuzz",
    "karmaWheelC_1",
    "karmaWheelC_2",
    "karmaWheelC_3",
    "karmaWheelC_4",
    "karmaWheelLow",
    "capBell2",
    "karmaRiseA",
    "UIWoodHit",
    "tick",
    "tock",
)


def dial_sounds_dir() -> Path:
    return assets_dir() / "karma"


def dial_sounds_missing() -> list[str]:
    return _sfx_missing(DIAL_SOUNDS, dial_sounds_dir())


def extract_dial_sounds(install: Path) -> list[str]:
    """表盘的音效；返回这次写出的片名。"""
    return _extract_sfx(install, dial_sounds_missing(), dial_sounds_dir())


def rain_atlas_missing() -> bool:
    return not any(
        (d / RAIN_ATLAS).is_file() and (d / f"{RAIN_ATLAS}.png").is_file()
        for d in (assets_dir(), bundled_assets_dir())
    )


def extract_rain_atlas(install: Path) -> bool:
    """提取水花图集。"""
    import UnityPy

    res = Path(install) / _ATLAS_REL
    if not res.is_file():
        return False
    return _extract_named(UnityPy, res, (RAIN_ATLAS,), assets_dir()).get(RAIN_ATLAS) == {"png", "map"}


_STREAMING = Path("RainWorld_Data") / "StreamingAssets"
_AUDIO_BUNDLES = ("music_songs",)
_LOOSE_AUDIO = (("music/songs", ".mp3"),)


def _bundle_path(install: Path, bundle: str) -> Path:
    return install / _STREAMING / "AssetBundles" / bundle


def _bundles_present(install: Path, bundles) -> bool:
    return any(_bundle_path(install, b).is_file() for b in bundles)


@_frees_unity
def _bundle_clips(install: Path, names, bundles=_AUDIO_BUNDLES) -> dict[str, bytes]:
    import UnityPy
    from UnityPy.helpers.ResourceReader import get_resource_data

    want, got = set(names), {}
    for bundle in bundles:
        path = _bundle_path(install, bundle)
        if not path.is_file() or not want - got.keys():
            continue
        for obj in UnityPy.load(str(path)).objects:
            if obj.type.name != "AudioClip":
                continue
            d = obj.read()
            if d.m_Name in want:
                r = d.m_Resource
                got[d.m_Name] = bytes(get_resource_data(r.m_Source, d.assets_file, r.m_Offset, r.m_Size))
    return got


def read_bundle_clip(install: Path, name: str) -> bytes:
    return _bundle_clips(Path(install), [name])[name]


_MUSIC_MARK = ".format"  # 记录提取时的 ogg 格式版本，对不上就重新提取


def _ogg_format() -> int:
    from .audio.fsbogg import FORMAT

    return FORMAT


def _music_current(d: Path) -> bool:
    try:
        return (d / _MUSIC_MARK).read_text(encoding="ascii").strip() == str(_ogg_format())
    except OSError:
        return False


def music_missing(names, dirs=None) -> list[str]:
    dirs = [d for d in (music_asset_dirs() if dirs is None else dirs) if _music_current(d)]
    tried = set().union(*(_tried(d) for d in dirs))
    return [
        n
        for n in names
        if n not in tried and not any((d / (n + ext)).is_file() for d in dirs for ext in (".ogg", ".mp3"))
    ]


def prune_music(keep) -> None:
    d = music_asset_dirs()[0]
    try:
        files = list(d.iterdir())
    except OSError:
        return
    for f in files:
        if f.suffix == ".part" or (f.suffix in (".ogg", ".mp3") and f.stem not in keep):
            try:
                f.unlink()
            except OSError:
                pass


def extract_music(install: Path, names, dest: Path | None = None) -> list[str]:
    """游戏里有完整散文件就直接复制，否则从资源包重新封装成 ogg；已有的跳过，单首失败不影响别的，返回这次写出的曲名。"""
    from .audio.fsbogg import fsb_to_ogg

    install = Path(install)
    out = Path(dest) / "music" if dest is not None else music_asset_dirs()[0]
    todo = music_missing(names, [out] if dest is not None else None)
    if not todo:
        return []
    out.mkdir(parents=True, exist_ok=True)
    blobs: dict[str, tuple[str, bytes]] = {}
    for name in todo:
        for sub, ext in _LOOSE_AUDIO:
            f = install / _STREAMING / sub / (name + ext)
            try:
                if f.is_file() and f.stat().st_size > 0:
                    blobs[name] = (ext, f.read_bytes())
                    break
            except OSError:
                pass
    rest = [n for n in todo if n not in blobs]
    read = False
    if rest:
        try:
            clips = _bundle_clips(install, rest)
            read = _bundles_present(install, _AUDIO_BUNDLES)
        except Exception:
            clips = {}
        for name, fsb in clips.items():
            try:
                blobs[name] = (".ogg", fsb_to_ogg(fsb))
            except ValueError:
                pass
    for name, (ext, data) in blobs.items():
        tmp = out / (name + ext + ".part")
        tmp.write_bytes(data)
        tmp.replace(out / (name + ext))
    (out / _MUSIC_MARK).write_text(str(_ogg_format()), encoding="ascii")
    if read:
        _mark_tried(out, set(rest) - blobs.keys())
    return list(blobs)


def ensure_atlases() -> Path:
    for d in (assets_dir(), bundled_assets_dir()):
        if atlases_present(d):
            return d
    _steam_libraries.cache_clear()  # 用户可能刚装好 Steam 再点重试
    install = detect_install()
    if not install:
        raise SetupError(t("err_no_install"))
    return extract_atlases(install)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    install = Path(argv[0]) if argv else detect_install()
    if install is None:
        print("未找到 Rain World 安装。用法：python -m slugcatpet.gameassets <游戏根目录>")
        return 2
    try:
        dest = extract_atlases(install)
    except SetupError as e:
        print(f"导入失败：{e}")
        return 1
    print(f"已从 {install} 导入图集到 {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

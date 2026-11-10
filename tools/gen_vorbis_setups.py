"""生成 slugcatpet/audio/vorbis_setups.py：场景曲和 Vorbis 音效片段各自的 Vorbis setup 头和模式表。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "slugcatpet" / "audio" / "vorbis_setups.py"
sys.path.insert(0, str(ROOT))


def main(argv):
    fsb5_src, libs = Path(argv[0]), Path(argv[1])
    sys.path.insert(0, str(fsb5_src))
    os.chdir(libs)  # fsb5 从当前目录找 lib*.dll
    from fsb5.vorbis_headers import lookup
    from fsb5 import vorbis as V
    from slugcatpet.audio.director import GAME_TRACKS
    from slugcatpet.gameassets import _SFX_BUNDLES, _bundle_clips, detect_install, read_bundle_clip
    from slugcatpet.items.lizard.assets import LIZARD_VORBIS_SOUNDS
    from slugcatpet.audio.fsbogg import parse_fsb5

    game = Path(argv[2]) if len(argv) > 2 else detect_install()
    table = {}
    sfx = _bundle_clips(game, LIZARD_VORBIS_SOUNDS, _SFX_BUNDLES)
    clips = [(n, read_bundle_clip(game, n)) for n in GAME_TRACKS] + [
        (n, sfx[n]) for n in LIZARD_VORBIS_SOUNDS
    ]
    for name, blob in clips:
        s = parse_fsb5(blob)
        q, ch, rate = lookup[s["crc32"]]
        short, long_, setup = V.get_header_info(q, ch, rate)
        assert zlib.crc32(setup) == s["crc32"], name
        info, com = V.VorbisInfo(), V.VorbisComment()
        for h in (
            V.rebuild_id_header(ch, rate, short, long_),
            V.rebuild_comment_header(),
            V.rebuild_setup_header(setup),
        ):
            V.vorbis.vorbis_synthesis_headerin(info, com, h)
        modes = []
        for i in range(128):  # 首字节 bit1..7 → 该包的块长
            pkt, buf = V.OggPacket(), ctypes.create_string_buffer(bytes([i << 1, 0, 0, 0]), 4)
            pkt.packet, pkt.bytes = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)), 4
            try:
                b = V.vorbis.vorbis_packet_blocksize(info, pkt)
            except Exception:
                b = -1
            modes.append("1" if b == long_ else "0" if b == short else "x")
        table[s["crc32"]] = [short, long_, base64.b64encode(setup).decode(), "".join(modes)]
        print(f"{name}: crc32={s['crc32']} q={q} setup={len(setup)}B")
    blob = base64.b64encode(zlib.compress(json.dumps(table).encode(), 9)).decode()
    lines = [blob[i : i + 96] for i in range(0, len(blob), 96)]
    OUT.write_text(
        '"""场景曲的 Vorbis setup 头（libvorbis 生成，BSD），由 tools/gen_vorbis_setups.py 生成，勿手改。"""\n\n'
        "import base64\nimport json\nimport zlib\n\n_BLOB = (\n"
        + "".join(f'    "{ln}"\n' for ln in lines)
        + ")\n\n# crc32 → (短块长, 长块长, setup 头, 模式表)\n"
        "SETUPS = {\n"
        "    int(k): (v[0], v[1], base64.b64decode(v[2]), v[3])\n"
        "    for k, v in json.loads(zlib.decompress(base64.b64decode(_BLOB))).items()\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUT.relative_to(ROOT)} ({len(table)} setups)")


if __name__ == "__main__":
    main(sys.argv[1:])

"""ピアニストの個性を選んでリアルタイム演奏するピアノ。

  python main.py                       GUIを起動
  python main.py --list                曲とプリセットの一覧
  python main.py --render out.wav --piece bach --preset グールド風
  python main.py --render out.wav --piece elise --instrument nylon --preset クラシック
  python main.py --render out.wav --piece korobeiniki --instrument bass --part bass8
  python main.py --render out.wav --piece korobeiniki --instrument band_steel
"""
from __future__ import annotations

import argparse
import copy
import sys
import wave

import numpy as np


PLUCKED = ("nylon", "steel", "bass", "band_steel", "band_nylon")


def _find_preset(name: str, instrument: str = "piano"):
    from play_piano.expression import presets_for

    for key, (_, params) in presets_for(instrument).items():
        if key.startswith(name) or name in key:
            return key, copy.copy(params)
    raise SystemExit(f"プリセットが見つかりません: {name}")


def _load_score(name: str):
    from play_piano.score import BUILTIN, find_piece, load_piece

    if name in BUILTIN:
        return BUILTIN[name]()
    return load_piece(find_piece(name))


def make_engine(instrument: str):
    from play_piano.engine import AudioEngine
    from play_piano.sampler import SFZBank, find_sfz

    if instrument in PLUCKED:
        from play_piano.strings import BANDS, StringBank

        # 合奏ではギターを主の音源にし、ベースは音符ごとに切り替える
        return AudioEngine(bank=StringBank(BANDS[instrument][0] if instrument in BANDS else instrument))
    sfz = find_sfz() if instrument == "sample" else None
    return AudioEngine(sampler=SFZBank(sfz) if sfz else None)


def render(path: str, piece: str, preset: str | None, seed: int, instrument: str, part: str | None) -> None:
    from play_piano.expression import Performer, presets_for
    from play_piano.synth import SR

    score = _load_score(piece)
    family = instrument if instrument in PLUCKED else "piano"
    if family != "piano":
        from play_piano.arrange import arrange, instrument_name

        score = arrange(score, instrument, part)
    # 既定の奏者: ピアノはルービンシュタイン風、ギター・ベースは2番目(「機械的」の次)
    preset = preset or ("ルービンシュタイン" if family == "piano" else list(presets_for(family))[1])
    key, params = _find_preset(preset, family)
    print(f"{score.title} / {key} を書き出し中...")
    engine = make_engine(instrument)
    if engine.sampler:
        print("音源: サンプル(" + engine.sampler.name + ")")
    else:
        print("音源: 合成", instrument_name(instrument) if family != "piano" else "ピアノ")
    audio = Performer(engine, score, params, seed=seed).render_offline()
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"保存しました: {path} ({len(audio) / SR:.1f}秒, ピーク {np.abs(audio).max():.2f})")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", metavar="WAV", help="GUIを使わずWAVに書き出す")
    ap.add_argument("--piece", default="bach", help="bach / elise / bach_wtc1-02 など組曲のキー / MIDIファイルのパス")
    ap.add_argument("--preset", help="奏者のプリセット名(前方一致)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--instrument", choices=("sample", "synth") + PLUCKED, default="sample",
                    help="sample: sounds/ のSFZ音源(無ければ合成), synth: 合成ピアノ, "
                         "nylon: クラシックギター, steel: アコースティックギター, bass: エレキベース, "
                         "band_steel / band_nylon: ギター＋ベースの合奏")
    ap.add_argument("--part", help="ギター: full / melody、ベース: bass / bass8 / melody、"
                                   "合奏: full / full8 / backing")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        from play_piano.arrange import PARTS
        from play_piano.expression import presets_for
        from play_piano.score import BUILTIN

        print("曲:", ", ".join(BUILTIN))
        for family, label in (("piano", "ピアノ"), ("nylon", "ギター"), ("bass", "ベース")):
            print(f"{label}の奏者:")
            for k, (desc, _) in presets_for(family).items():
                print(f"  {k}: {desc}")
        for family, parts in PARTS.items():
            print(f"{family} のパート (--part):", ", ".join(f"{k}={v}" for k, v in parts))
        return
    if args.render:
        render(args.render, args.piece, args.preset, args.seed, args.instrument, args.part)
        return

    from play_piano.gui import run

    run()


if __name__ == "__main__":
    main()

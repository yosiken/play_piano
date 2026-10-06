"""ピアニストの個性を選んでリアルタイム演奏するピアノ。

  python main.py                       GUIを起動
  python main.py --list                曲とプリセットの一覧
  python main.py --render out.wav --piece bach --preset グールド風
"""
from __future__ import annotations

import argparse
import copy
import sys
import wave

import numpy as np


def _find_preset(name: str):
    from play_piano.expression import PRESETS

    for key, (_, params) in PRESETS.items():
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

    sfz = find_sfz() if instrument == "sample" else None
    return AudioEngine(sampler=SFZBank(sfz) if sfz else None)


def render(path: str, piece: str, preset: str, seed: int, instrument: str) -> None:
    from play_piano.expression import Performer
    from play_piano.synth import SR

    score = _load_score(piece)
    key, params = _find_preset(preset)
    print(f"{score.title} / {key} を書き出し中...")
    engine = make_engine(instrument)
    print("音源:", "サンプル(" + engine.sampler.name + ")" if engine.sampler else "合成")
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
    ap.add_argument("--preset", default="ルービンシュタイン", help="プリセット名(前方一致)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--instrument", choices=("sample", "synth"), default="sample",
                    help="sample: sounds/ のSFZ音源(無ければ合成), synth: 合成ピアノ")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        from play_piano.expression import PRESETS
        from play_piano.score import BUILTIN

        print("曲:", ", ".join(BUILTIN))
        for k, (desc, _) in PRESETS.items():
            print(f"  {k}: {desc}")
        return
    if args.render:
        render(args.render, args.piece, args.preset, args.seed, args.instrument)
        return

    from play_piano.gui import run

    run()


if __name__ == "__main__":
    main()

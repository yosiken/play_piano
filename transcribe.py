"""ピアノ演奏の録音（YouTube のURL または音声ファイル）を自動採譜して MIDI にする。

  python transcribe.py https://www.youtube.com/watch?v=XXXX
  python transcribe.py https://youtu.be/XXXX --title "曲名" --composer "作曲者" --start 0:12 --end 3:40
  python transcribe.py recording.wav --name my_piece

採譜には ByteDance の piano_transcription_inference（ピアノ専用。ペダルも推定）を使う。
MIDI は pieces/youtube/ に保存し、pieces/catalog.toml に1ファイルの組曲として追記する。
ピアノソロの録音ほど精度が高い（バンド演奏などは他の楽器の音も拾ってしまう）。
GUI の「URLから採譜…」ボタンからも同じ変換ができる。
"""
from __future__ import annotations

import argparse
import sys

from play_piano.transcribe import DEFAULT_SUBDIR, convert


def main() -> None:
    ap = argparse.ArgumentParser(description="ピアノ録音を自動採譜して MIDI にする")
    ap.add_argument("source", help="YouTube などのURL、または音声ファイル")
    ap.add_argument("--name", default="", help="保存するファイル名（拡張子なし）。省略時は動画タイトル")
    ap.add_argument("--title", default="", help="一覧に出す曲名。省略時は動画タイトル")
    ap.add_argument("--composer", default="", help="一覧に出す作曲者")
    ap.add_argument("--start", help="切り出し開始（例 0:12）")
    ap.add_argument("--end", help="切り出し終了（例 3:40）")
    ap.add_argument("--dir", default=DEFAULT_SUBDIR, help=f"pieces/ 内の保存先（既定 {DEFAULT_SUBDIR}）")
    ap.add_argument("--no-catalog", action="store_true", help="catalog.toml に登録しない")
    args = ap.parse_args()
    try:
        convert(args.source, name=args.name, title=args.title, composer=args.composer,
                start=args.start, end=args.end, subdir=args.dir, catalog=not args.no_catalog)
    except (RuntimeError, FileNotFoundError) as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

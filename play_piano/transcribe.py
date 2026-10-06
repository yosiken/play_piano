"""ピアノ演奏の録音（YouTube などのURL または音声ファイル）を自動採譜して MIDI にする。

採譜には ByteDance の piano_transcription_inference（ピアノ専用。ペダルも推定）を使う。
MIDI は pieces/<保存先>/ に保存し、pieces/catalog.toml に1ファイルの組曲として追記する。
CLI（ルートの transcribe.py）と GUI の両方から使う。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from typing import Callable

from .score import CATALOG_PATH, PIECES_DIR

ROOT = os.path.dirname(PIECES_DIR)
CACHE_DIR = os.path.join(ROOT, "cache", "transcribe")
DEFAULT_SUBDIR = "youtube"

Log = Callable[[str], None]
_model = None  # 読み込んだ採譜モデル（2回目以降の変換で使い回す）


def _echo(msg: str) -> None:
    # 採譜中は sys.stdout を横取りするので、print ではなく元の標準出力に書く
    print(msg, file=sys.__stdout__, flush=True)


def is_url(s: str) -> bool:
    return re.match(r"https?://", s) is not None


def slug(s: str) -> str:
    """ファイル名に使える形にする（日本語はそのまま残す）。"""
    s = re.sub(r'[\\/:*?"<>|\[\]]+', " ", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:80].strip("._") or "untitled"


def seconds(t: str | None) -> float | None:
    """'95' / '1:35' / '0:01:35' → 秒。空なら None。"""
    if not t or not t.strip():
        return None
    sec = 0.0
    for part in t.strip().split(":"):
        sec = sec * 60 + float(part)
    return sec


class CommandError(RuntimeError):
    def __init__(self, msg: str, output: str):
        super().__init__(msg)
        self.output = output


def _run(cmd: list[str], log: Log) -> str:
    """コマンドを実行し、出力を1行ずつ log に流す。戻り値: 全出力"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # GUIから呼んだとき黒い窓を出さない
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", creationflags=flags)
    out = []
    for line in p.stdout:
        out.append(line)
        if line.strip():
            log(line.rstrip())
    if p.wait():
        raise CommandError(f"{cmd[0]} が失敗しました:\n" + "".join(out[-8:]), "".join(out))
    return "".join(out)


# YouTube はダウンロードを時々 403 で拒否する(確認用の PO Token が無い取得をランダムに断る)。
# 失敗したら取得方法(player_client)を変えて取り直す。None は yt-dlp の標準。
_CLIENT_TRIES = (None, "web_embedded", None, "web_embedded")


def download_audio(url: str, log: Log = _echo) -> tuple[str, str]:
    """yt-dlp で音声を取り出す。戻り値: (音声ファイルのパス, 動画タイトル)"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    log("動画の情報を取得中…")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["yt-dlp", "--no-playlist", "-J", url], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=flags)
    if r.returncode:
        raise RuntimeError("動画の情報を取得できませんでした:\n" + r.stderr.strip()[-600:])
    info = json.loads(r.stdout)
    path = os.path.join(CACHE_DIR, f"{info['id']}.wav")
    if not os.path.exists(path):
        log(f"音声をダウンロード中: {info.get('title', url)}")
        for i, client in enumerate(_CLIENT_TRIES):
            cmd = ["yt-dlp", "--no-playlist", "--newline", "--no-continue", "-x", "--audio-format", "wav",
                   "-o", os.path.join(CACHE_DIR, "%(id)s.%(ext)s")]
            if client:
                cmd += ["--extractor-args", f"youtube:player_client={client}"]
            try:
                _run(cmd + [url], log)
                break
            except CommandError as e:
                if "HTTP Error 403" not in e.output:
                    raise
                if i + 1 == len(_CLIENT_TRIES):
                    raise RuntimeError(
                        f"{e}\nYouTube にダウンロードを拒否されました（取得方法を変えて{len(_CLIENT_TRIES)}回試しました）。\n"
                        "時間をおいてもう一度試してください。続く場合は、yt-dlp の開発版で直っていることがあります:\n"
                        "yt-dlp --update-to nightly"
                    ) from None
                log(f"YouTube に拒否されました(403)。取得方法を変えて再試行します（{i + 2}/{len(_CLIENT_TRIES)}）…")
                time.sleep(2)
    return path, info.get("title", info["id"])


class _LineWriter(io.TextIOBase):
    """print された文字列を1行ずつ log に渡す（採譜ライブラリの進捗表示を拾う）。"""

    def __init__(self, log: Log):
        self.log, self.buf = log, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.log(line.strip())
        return len(s)


def transcribe_audio(audio_path: str, midi_path: str, start: float | None = None,
                     end: float | None = None, log: Log = _echo) -> None:
    global _model
    import librosa
    import torch
    from piano_transcription_inference import PianoTranscription, sample_rate

    log("音声を読み込み中…")
    # パッケージ同梱の load_audio は新しい librosa で動かないので librosa.load を直接使う
    duration = None if end is None else end - (start or 0.0)
    audio, _ = librosa.load(audio_path, sr=sample_rate, mono=True,
                            offset=start or 0.0, duration=duration)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    with contextlib.redirect_stdout(_LineWriter(log)):
        if _model is None:
            log("採譜モデルを読み込み中…")
            _model = PianoTranscription(device=device)
        log(f"採譜中（{device}、{len(audio) / sample_rate:.0f}秒の音声）…")
        _model.transcribe(audio, midi_path)


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def add_to_catalog(rel_stem: str, composer: str, title: str) -> bool:
    """catalog.toml に [sets] として追記する（既に登録済みなら何もしない）。"""
    key = "yt_" + os.path.basename(rel_stem)
    text = open(CATALOG_PATH, encoding="utf-8").read() if os.path.exists(CATALOG_PATH) else ""
    if f'"{rel_stem}"' in text:
        return False
    block = (f"\n[sets.{_toml_str(key)}]\n"
             f"composer = {_toml_str(composer)}\n"
             f"title = {_toml_str(title)}\n"
             f"files = [{_toml_str(rel_stem)}]\n"
             f"gap = 0.0\n")
    with open(CATALOG_PATH, "a", encoding="utf-8", newline="") as f:
        f.write(("" if text.endswith("\n") or not text else "\n") + block)
    return True


def convert(source: str, *, name: str = "", title: str = "", composer: str = "",
            start: str | None = None, end: str | None = None, subdir: str = DEFAULT_SUBDIR,
            catalog: bool = True, log: Log = _echo) -> tuple[str, str]:
    """URL か音声ファイルを MIDI にする。戻り値: (MIDIのパス, 曲名)"""
    if is_url(source):
        audio_path, src_title = download_audio(source, log)
    else:
        if not os.path.exists(source):
            raise FileNotFoundError(f"ファイルが見つかりません: {source}")
        audio_path = source
        src_title = os.path.splitext(os.path.basename(source))[0]

    title = title or src_title
    stem = slug(name or src_title)
    out_dir = os.path.join(PIECES_DIR, subdir)
    os.makedirs(out_dir, exist_ok=True)
    midi_path = os.path.join(out_dir, stem + ".mid")

    transcribe_audio(audio_path, midi_path, seconds(start), seconds(end), log)
    log(f"保存しました: {os.path.relpath(midi_path, ROOT)}")

    if catalog:
        rel_stem = f"{subdir}/{stem}".replace("\\", "/")
        if add_to_catalog(rel_stem, composer, title):
            log(f"catalog.toml に登録しました: {title}")
        else:
            log("catalog.toml には登録済みです")
    return midi_path, title

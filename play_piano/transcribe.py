"""演奏の録音（YouTube などのURL または音声ファイル）を自動採譜して MIDI にする。

ピアノ: ByteDance の piano_transcription_inference（ピアノ専用。ペダルも推定）
ギター・ベース: Meta の Demucs でバンド演奏からその楽器の音だけを取り出し（音源分離）、
               Spotify の Basic Pitch（楽器を問わない採譜モデル）で MIDI にする
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
_demucs: dict = {}  # 音源分離のモデル（名前 → モデル）

# 採譜する楽器: 表示名、Demucs のモデルと取り出すパート、音域[Hz]、MIDIの音色番号、ファイル名の末尾
INSTRUMENTS = {
    "piano": dict(label="ピアノ"),
    "guitar": dict(label="ギター", model="htdemucs_6s", stem="guitar", fmin=75.0, fmax=1400.0,
                   program=25, suffix="_guitar"),
    "bass": dict(label="ベース", model="htdemucs", stem="bass", fmin=38.0, fmax=420.0,
                 program=33, suffix="_bass"),
}


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


def _load_clip(audio_path: str, sr: int, mono: bool, start: float | None, end: float | None):
    import librosa

    duration = None if end is None else end - (start or 0.0)
    audio, _ = librosa.load(audio_path, sr=sr, mono=mono, offset=start or 0.0, duration=duration)
    return audio


def separate(audio_path: str, out_path: str, model_name: str, stem: str,
             start: float | None = None, end: float | None = None, log: Log = _echo) -> None:
    """Demucs で1つの楽器の音だけを取り出して WAV に保存する。

    demucs のコマンド(保存に torchaudio を使い、新しい版では動かないことがある)ではなく、
    モデルを直接呼んで soundfile で保存する。
    """
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    if model_name not in _demucs:
        log(f"音源分離モデル（{model_name}）を読み込み中…（初回はダウンロードします）")
        _demucs[model_name] = get_model(model_name)
    model = _demucs[model_name]
    model.eval()
    audio = _load_clip(audio_path, model.samplerate, False, start, end)
    if audio.ndim == 1:
        audio = np.stack([audio, audio])
    audio = audio[: model.audio_channels]
    if audio.shape[0] < model.audio_channels:
        audio = np.repeat(audio, model.audio_channels, axis=0)
    wav = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))
    # モデルの学習時と同じく、音量を正規化してから分離する
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(f"音源分離中（{device}、{wav.shape[1] / model.samplerate:.0f}秒の音声から{stem}を取り出す）…")
    with torch.no_grad():
        out = apply_model(model, ((wav - mean) / std)[None], device=device, split=True, overlap=0.25,
                          progress=False)[0]
    part = out[model.sources.index(stem)] * std + mean
    sf.write(out_path, part.cpu().numpy().T, model.samplerate)


def transcribe_pitch(audio_path: str, midi_path: str, inst: str, start: float | None = None,
                     end: float | None = None, log: Log = _echo) -> int:
    """Basic Pitch でギター・ベースの音声を MIDI にする。戻り値: 音符の数"""
    import logging

    import soundfile as sf

    spec = INSTRUMENTS[inst]
    # basic_pitch は使わない実行環境(TensorFlow など)が無いと警告を出すので黙らせる
    logging.getLogger().setLevel(logging.ERROR)
    from basic_pitch import FilenameSuffix, build_icassp_2022_model_path
    from basic_pitch.inference import predict

    if start is not None or end is not None:
        clip = os.path.splitext(midi_path)[0] + ".clip.wav"
        sf.write(clip, _load_clip(audio_path, 22050, True, start, end), 22050)
        audio_path = clip
    log(f"採譜中（Basic Pitch、{spec['label']}）…")
    try:
        import onnxruntime  # noqa: F401  Windows・新しい Python では ONNX 版のモデルを使う
        model = build_icassp_2022_model_path(FilenameSuffix.onnx)
    except ImportError:
        from basic_pitch import ICASSP_2022_MODEL_PATH as model
    with contextlib.redirect_stdout(_LineWriter(log)):
        _, midi, notes = predict(
            audio_path, model,
            # 既定値(0.5 / 0.3)より厳しめにして、余韻や倍音から出る余分な音を減らす
            onset_threshold=0.6, frame_threshold=0.4,
            minimum_note_length=90 if inst == "guitar" else 110,  # ms
            minimum_frequency=spec["fmin"], maximum_frequency=spec["fmax"],
            multiple_pitch_bends=False, melodia_trick=True)
    if audio_path.endswith(".clip.wav"):
        os.remove(audio_path)
    for ins in midi.instruments:
        ins.program = spec["program"]
        ins.pitch_bends = []
        if inst == "bass":
            # ベースは単音: 重なった音は後の音の頭で切る
            ns = sorted(ins.notes, key=lambda n: (n.start, -n.velocity))
            kept = []
            for n in ns:
                if kept and n.start - kept[-1].start < 0.03:
                    continue  # ほぼ同時の音(倍音の誤検出)は強いほうだけ
                if kept and kept[-1].end > n.start:
                    kept[-1].end = n.start
                kept.append(n)
            ins.notes = kept
    midi.write(midi_path)
    return sum(len(i.notes) for i in midi.instruments)


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
            catalog: bool = True, instrument: str = "piano", split: bool = True,
            log: Log = _echo) -> tuple[str, str]:
    """URL か音声ファイルを MIDI にする。戻り値: (MIDIのパス, 曲名)

    instrument: piano / guitar / bass。split: ギター・ベースで、先に音源分離するか
    （その楽器だけの録音なら不要）
    """
    if instrument not in INSTRUMENTS:
        raise ValueError(f"楽器は {', '.join(INSTRUMENTS)} のどれかです: {instrument}")
    if is_url(source):
        audio_path, src_title = download_audio(source, log)
    else:
        if not os.path.exists(source):
            raise FileNotFoundError(f"ファイルが見つかりません: {source}")
        audio_path = source
        src_title = os.path.splitext(os.path.basename(source))[0]

    spec = INSTRUMENTS[instrument]
    title = title or src_title
    stem = slug(name or src_title) + ("" if name else spec.get("suffix", ""))
    if instrument != "piano" and not name:
        title += f"（{spec['label']}）"
    out_dir = os.path.join(PIECES_DIR, subdir)
    os.makedirs(out_dir, exist_ok=True)
    midi_path = os.path.join(out_dir, stem + ".mid")

    t0, t1 = seconds(start), seconds(end)
    if instrument == "piano":
        transcribe_audio(audio_path, midi_path, t0, t1, log)
    else:
        if split:
            os.makedirs(CACHE_DIR, exist_ok=True)
            part = os.path.join(CACHE_DIR, f"{stem}.{spec['stem']}.wav")
            separate(audio_path, part, spec["model"], spec["stem"], t0, t1, log)
            audio_path, t0, t1 = part, None, None
        count = transcribe_pitch(audio_path, midi_path, instrument, t0, t1, log)
        if count == 0:
            raise RuntimeError(f"{spec['label']}の音が見つかりませんでした")
        log(f"{count}個の音符を採譜しました")
    log(f"保存しました: {os.path.relpath(midi_path, ROOT)}")

    if catalog:
        rel_stem = f"{subdir}/{stem}".replace("\\", "/")
        if add_to_catalog(rel_stem, composer, title):
            log(f"catalog.toml に登録しました: {title}")
        else:
            log("catalog.toml には登録済みです")
    return midi_path, title

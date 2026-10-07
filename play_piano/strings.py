"""ギター・ベースの音を合成する(撥弦楽器)。

弦をはじいた瞬間の形(はじく位置で決まる倍音の強さ)と、倍音ごとの減衰
(高い倍音ほど早く消える)を正弦波の重ね合わせで作り、
ギターは胴の共鳴、エレキベースはピックアップとアンプの特性を掛ける。

合成ピアノ(synth.py)と同じく「弱くはじいた音(low)」と「強くはじいたときに加わる差分(high)」の
2層を作るので、エンジン側はピアノと同じ発音体(engine._Voice)で鳴らせる。
レンダリング結果は cache/ にディスクキャッシュする。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np

from .synth import CACHE_DIR, SR, midi_to_freq

CACHE_VERSION = 2


@dataclass(frozen=True)
class StringSpec:
    name: str
    lo: int             # 出せる最低音(開放弦の最低音)
    hi: int             # 出せる最高音(最高フレット)
    pluck: float        # はじく位置(弦長に対する駒からの割合)
    soft_fc: float      # 弱くはじいたときの明るさ(倍音の低域通過の目安[Hz])
    hard_fc: float      # 強くはじいたときの明るさ
    tau_low: float      # 最低音の基音の減衰時間[s](振幅が1/eになるまで)
    tau_oct: float      # 1オクターブ上がるごとに減衰時間が何倍になるか
    loss1: float        # 周波数に比例する損失(1/τ に足す、1/s/Hz)
    loss2: float        # 周波数の2乗に比例する損失(高い倍音ほど急に消える)
    B: float            # 弦の硬さによる非調和性
    attack: float       # 立ち上がり時間[s]
    body: tuple         # 胴の共鳴 (周波数, Q, 強さ)。空ならピックアップで拾う楽器
    pickup: float       # ピックアップの位置(駒からの割合)。0 なら無し
    amp_fc: float       # アンプ・スピーカーの高域の上限[Hz]。0 なら無し
    click: float        # ピック・爪が弦に当たる音の大きさ
    max_seconds: float
    gain: float = 1.0   # 音量(単音で弾くベースは少し大きく)


SPECS: dict[str, StringSpec] = {
    "nylon": StringSpec(
        "クラシックギター（ナイロン弦）", lo=40, hi=83, pluck=0.16, soft_fc=900, hard_fc=3200,
        tau_low=2.4, tau_oct=0.62, loss1=9e-4, loss2=4e-7, B=1.5e-5, attack=0.0025,
        body=((98, 9, 1.0), (205, 14, 0.9), (395, 10, 0.55), (610, 7, 0.35), (1050, 5, 0.25)),
        pickup=0.0, amp_fc=0.0, click=0.25, max_seconds=7.0),
    "steel": StringSpec(
        "アコースティックギター（スチール弦）", lo=40, hi=86, pluck=0.12, soft_fc=1500, hard_fc=6500,
        tau_low=3.6, tau_oct=0.6, loss1=5e-4, loss2=1.2e-7, B=4e-5, attack=0.0008,
        body=((105, 8, 1.0), (220, 12, 0.85), (420, 9, 0.5), (700, 6, 0.4), (1800, 4, 0.3)),
        pickup=0.0, amp_fc=0.0, click=0.6, max_seconds=8.0),
    "bass": StringSpec(
        "エレキベース", lo=28, hi=67, pluck=0.1, soft_fc=500, hard_fc=2200,
        tau_low=3.2, tau_oct=0.6, loss1=1.4e-3, loss2=6e-7, B=1.2e-4, attack=0.0018,
        body=(), pickup=0.22, amp_fc=4500, click=0.15, max_seconds=8.0, gain=1.6),
}

# ギター＋ベースの合奏: キー → (ギターの種類, 表示名)
BANDS = {
    "band_steel": ("steel", "ギター＋ベース（アコースティック）"),
    "band_nylon": ("nylon", "ギター＋ベース（クラシック）"),
}

# 鳴らす強さの目安: 強くはじいた音の最初の0.5秒の実効値(合成ピアノと同じくらいの音量にそろえる)
_TARGET_RMS = 0.42


def _body_gain(spec: StringSpec, f: np.ndarray) -> np.ndarray:
    """胴(またはピックアップ・アンプ)を通ったときの、周波数ごとの振幅の倍率。"""
    g = np.ones_like(f)
    if spec.body:
        g = 0.45 + sum(s / np.sqrt(1 + q * q * (f / fb - fb / f) ** 2) for fb, q, s in spec.body)
        g /= 1 + (f / 5000.0) ** 2  # 胴は高い音をあまり放射しない
    if spec.amp_fc:
        g = g / np.sqrt(1 + (f / spec.amp_fc) ** 4)
    return g


def _thump(spec: StringSpec, rng, n: int) -> np.ndarray:
    """はじいた瞬間の音: 胴が叩かれて鳴る短い響きと、ピック(爪)が弦をこする音。"""
    t = np.arange(n) / SR
    out = np.zeros(n)
    for fb, q, s in spec.body:
        tau = q / (np.pi * fb)
        out += s * np.exp(-t / tau) * np.sin(2 * np.pi * fb * t + rng.uniform(0, 6.28))
    # ピックの当たる音: 数ミリ秒の高域の雑音
    m = min(n, int(SR * 0.012))
    noise = rng.standard_normal(m)
    noise = np.diff(noise, prepend=0.0)  # 高域を強調
    out[:m] += spec.click * noise * np.exp(-np.arange(m) / (SR * 0.0025))
    return out


def _render(kind: str, m: int) -> tuple[np.ndarray, np.ndarray]:
    spec = SPECS[kind]
    rng = np.random.default_rng(2000 + 97 * list(SPECS).index(kind) + m)
    f0 = midi_to_freq(m)
    nyq = min(SR * 0.45, 14000.0)
    ks = np.arange(1, 160)
    fk = ks * f0 * np.sqrt(1.0 + spec.B * ks * ks)
    keep = fk < nyq
    ks, fk = ks[keep], fk[keep]

    # はじいた形: 三角形の変位 → 駒にかかる力は sin(πkβ)/k
    shape = np.abs(np.sin(np.pi * ks * spec.pluck)) / ks
    if spec.pickup:
        shape *= np.abs(np.sin(np.pi * ks * spec.pickup)) * 2
    shape *= _body_gain(spec, fk)
    soft = shape / (1 + (fk / spec.soft_fc) ** 2)
    hard = shape / (1 + (fk / spec.hard_fc) ** 1.5)

    # 倍音ごとの減衰時間
    tau1 = spec.tau_low * spec.tau_oct ** ((m - spec.lo) / 12)
    tau = 1.0 / (1.0 / tau1 + spec.loss1 * fk + spec.loss2 * fk * fk)

    top = max(hard.max(), 1e-9)
    # 長さ: 一番長く残る倍音が -70dB まで小さくなるまで
    life = tau * np.log(np.maximum(hard / top, 1e-9) / 10 ** (-70 / 20))
    length = int(SR * np.clip(life.max() if len(life) else 1.0, 1.0, spec.max_seconds))
    t = np.arange(length) / SR

    low = np.zeros(length)
    high = np.zeros(length)
    for a_s, a_h, f, tk, lf in zip(soft, hard, fk, tau, life):
        if lf <= 0:
            continue
        n = min(length, int(SR * lf) + 1)
        tt = t[:n]
        # 弦の振動は縦横2方向あり、減衰の速さとわずかな音程が違う(余韻のうなり)
        d = rng.uniform(-0.25, 0.25)
        ph = rng.uniform(-0.2, 0.2)
        wave = (0.75 * np.exp(-tt / tk) * np.sin(2 * np.pi * f * tt + ph)
                + 0.25 * np.exp(-tt / (tk * 2.2)) * np.sin(2 * np.pi * f * 2 ** (d / 1200) * tt + ph))
        low[:n] += a_s * wave
        high[:n] += (a_h - a_s) * wave

    attack = 1.0 - np.exp(-t / spec.attack)
    low *= attack
    high *= attack

    # はじいた瞬間の音(強くはじくほど大きく、明るい)
    nt = min(length, int(SR * 0.4))
    th = _thump(spec, rng, nt) * top * 0.6
    low[:nt] += th * 0.35
    high[:nt] += th * 0.65

    # 音量をそろえる
    full = low.copy()
    full[: len(high)] += high
    rms = np.sqrt(np.mean(full[: int(SR * 0.5)] ** 2))
    g = _TARGET_RMS * spec.gain / max(rms, 1e-9)
    low *= g
    high *= g

    fade = int(SR * 0.3)
    if length > fade:
        ramp = np.linspace(1, 0, fade)
        low[-fade:] *= ramp
        high[-fade:] *= ramp
    # 強打の差分が十分小さくなったところで high を打ち切る(メモリを節約)
    ref = np.abs(full).max() * g
    k = np.nonzero(np.abs(high) > ref * 1e-3)[0]
    hn = (k[-1] + 1) if len(k) else 1
    high = high[:hn]
    f2 = min(hn, int(SR * 0.05))
    high[hn - f2:] *= np.linspace(1, 0, f2)
    return low.astype(np.float32), high.astype(np.float32)


def fold(pitch: int, lo: int, hi: int) -> int:
    """音域外の音をオクターブ移動して lo〜hi に収める。"""
    while pitch < lo:
        pitch += 12
    while pitch > hi:
        pitch -= 12
    return pitch


class StringBank:
    """ギター・ベースの合成済み波形を保持する(synth.NoteBank と同じ使い方)。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.spec = SPECS[kind]
        self.name = self.spec.name
        self._notes: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._lock = threading.Lock()

    def has(self, m: int) -> bool:
        return m in self._notes

    def get(self, m: int) -> tuple[np.ndarray, np.ndarray]:
        m = fold(m, self.spec.lo, self.spec.hi)
        note = self._notes.get(m)
        if note is None:
            note = self.load(m)
        return note

    def load(self, m: int) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if m in self._notes:
                return self._notes[m]
        path = os.path.join(CACHE_DIR, f"{self.kind}_v{CACHE_VERSION}_{m}.npz")
        note = None
        if os.path.exists(path):
            try:
                with np.load(path) as z:
                    note = (z["low"], z["high"])
            except Exception:
                note = None
        if note is None:
            note = _render(self.kind, m)
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp.npz"
            np.savez(tmp, low=note[0], high=note[1])
            os.replace(tmp, path)
        with self._lock:
            self._notes[m] = note
        return note

    def prepare(self, pitches, progress=None) -> None:
        todo = sorted(set(fold(p, self.spec.lo, self.spec.hi) for p in pitches) - set(self._notes))
        if not todo:
            return
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, _ in enumerate(as_completed([pool.submit(self.load, m) for m in todo])):
                if progress:
                    progress(i + 1, len(todo))

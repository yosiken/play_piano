"""空間と共鳴: 合成したインパルス応答と、リアルタイム用の分割畳み込み。"""
from __future__ import annotations

import numpy as np

from .synth import NO_DAMPER_FROM, SR, midi_to_freq

PART = 1024  # 畳み込みの分割長(このぶんの遅延がプリディレイを兼ねる)

# 名前: (残響時間RT60[s], 初期反射の広がり[s], 残響の量)
ROOMS = {
    "ドライ（残響なし）": (0.0, 0.0, 0.0),
    "サロン": (1.1, 0.025, 0.22),
    "コンサートホール": (2.2, 0.06, 0.30),
    "大聖堂": (4.5, 0.10, 0.34),
}


def _band_noise(rng, n, lo, hi):
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    w = 1 / (1 + (lo / np.maximum(f, 1)) ** 4) / (1 + (f / hi) ** 4)
    return np.fft.irfft(spec * w, n)


def hall_ir(rt60: float, er_spread: float, seed: int = 3) -> np.ndarray:
    """ホールのインパルス応答(ステレオ)。低音ほど長く、高音ほど早く減衰する。"""
    rng = np.random.default_rng(seed)
    n = int(SR * rt60 * 1.15)
    t = np.arange(n) / SR
    ir = np.zeros((n, 2))
    bands = [(20, 180, 1.35), (180, 700, 1.15), (700, 2000, 1.0), (2000, 5000, 0.75), (5000, 16000, 0.45)]
    for ch in range(2):
        for lo, hi, k in bands:
            ir[:, ch] += _band_noise(rng, n, lo, hi) * np.exp(-6.91 * t / (rt60 * k))
        # 残響は少し遅れて立ち上がる(拡散音場が育つ)
        ir[:, ch] *= 1 - np.exp(-t / (er_spread * 0.8 + 1e-3))
        # 初期反射: 壁・天井からの反射音
        for _ in range(14):
            d = rng.uniform(0.004, er_spread * 1.4 + 0.006)
            i = int(d * SR)
            g = rng.uniform(0.4, 1.0) * (0.012 / (d + 0.012))
            ir[i:i + 24, ch] += g * np.hanning(24) * rng.choice((-1, 1))
    ir /= np.sqrt((ir ** 2).sum() / 2)
    return ir


def resonance_irs(seed: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """弦の共鳴: (ペダル時に全弦が共鳴する分, ダンパーの無い高音弦が常に共鳴する分)。"""
    rng = np.random.default_rng(seed)
    n = int(SR * 3.0)
    t = np.arange(n) / SR
    pedal = np.zeros((n, 2))
    free = np.zeros((n, 2))
    for m in range(21, 109):
        f0 = midi_to_freq(m)
        B = 1.0e-4 * 2.0 ** ((m - 60) / 18.0)
        tau = 0.6 * 10.0 * 2.0 ** (-(m - 21) / 20.0)
        dst = free if m >= NO_DAMPER_FROM else pedal
        for k in range(1, 7):
            fk = k * f0 * np.sqrt(1 + B * k * k)
            if fk > 9000:
                break
            nk = min(n, int(tau / k ** 0.5 * 7 * SR))
            env = np.exp(-t[:nk] * k ** 0.5 / tau) / k
            for ch in range(2):
                dst[:nk, ch] += env * np.sin(2 * np.pi * fk * t[:nk] + rng.uniform(0, 6.28))
    pedal /= np.sqrt((pedal ** 2).sum() / 2)
    free /= np.sqrt((free ** 2).sum() / 2)
    return pedal, free


class Convolver:
    """モノラル入力 → 複数のステレオIRを一様分割の重畳加算(overlap-save)で畳み込む。

    出力は PART サンプル遅れる。
    """

    def __init__(self, irs: list[np.ndarray]):
        K = max(1, max(-(-len(ir) // PART) for ir in irs))
        self.K = K
        self.H = []
        for ir in irs:
            pad = np.zeros((K * PART, 2))
            pad[: len(ir)] = ir
            parts = pad.reshape(K, PART, 2).transpose(0, 2, 1)       # (K, 2, PART)
            Hk = np.fft.rfft(parts, 2 * PART, axis=2)                 # (K, 2, PART+1)
            Hrev = Hk[::-1]
            self.H.append(np.concatenate([Hrev, Hrev], axis=0))       # 窓のずらし用に2周分
        self.X = np.zeros((K, PART + 1), dtype=complex)
        self.idx = 0
        self.prev = np.zeros(PART)
        self.inbuf = np.zeros(0)
        self.outbuf = [np.zeros((PART, 2)) for _ in self.H]

    def _block(self, x):
        buf = np.concatenate([self.prev, x])
        self.prev = x
        self.X[self.idx] = np.fft.rfft(buf)
        s = (self.K - 1 - self.idx) % self.K
        outs = []
        for H in self.H:
            Y = np.einsum("kf,kcf->cf", self.X, H[s:s + self.K])
            outs.append(np.fft.irfft(Y, 2 * PART, axis=1)[:, PART:].T)
        self.idx = (self.idx + 1) % self.K
        return outs

    def process(self, x: np.ndarray) -> list[np.ndarray]:
        """x(モノラル)を入れ、同じ長さの出力(IRごとのステレオ)を返す。"""
        self.inbuf = np.concatenate([self.inbuf, x])
        while len(self.inbuf) >= PART:
            outs = self._block(self.inbuf[:PART])
            self.inbuf = self.inbuf[PART:]
            self.outbuf = [np.concatenate([b, o]) for b, o in zip(self.outbuf, outs)]
        n = len(x)
        res = [b[:n] for b in self.outbuf]
        self.outbuf = [b[n:] for b in self.outbuf]
        return res

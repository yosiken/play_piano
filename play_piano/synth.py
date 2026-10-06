"""外部音源を使わずにピアノ音を合成する。

録音ピアノを分析した表(piano_model.npz、tools/build_piano_model.py で作成)をもとに、
倍音ごとの周波数(非調和性)と音量の時間変化を正弦波の重ね合わせで再現し、
ハンマーが当たったときの響板・ケースの打撃音を、測ったスペクトルどおりに整形した雑音で加える。
実行時に WAV は読まない。

1音ごとに「弱打の音(low)」と「強打で加わる差分(high)」の2層を作っておき、
再生時にベロシティに応じて混ぜることで、強く弾くほど明るい音色になる。
レンダリング結果は cache/ にディスクキャッシュする。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

SR = 44100
CACHE_VERSION = 6
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "piano_model.npz")
# 倍音の音量を測る時刻[s]
MODEL_TIMES = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
# 打撃音のスペクトルを測る 1/3 オクターブ帯の中心周波数[Hz]
KNOCK_BANDS = 31.5 * 2.0 ** (np.arange(28) / 3)

# これより上の鍵盤には実物のピアノと同様ダンパーが無い
NO_DAMPER_FROM = 89

# 表にある弱打・強打の録音が鳴らされたベロシティ
SOFT_VEL, HARD_VEL = 30, 127
# サンプル音源の再生音量(engine.SAMPLE_GAIN、SFZ の amp_veltrack=73)とパンの補正。
# 合成音を録音ピアノと同じ音量にそろえるために使う
_SAMPLE_GAIN = 1.6
_SAMPLE_VELTRACK = 0.73
_PAN_COMP = np.sqrt(2.0)
MAX_SECONDS = 10.0


def midi_to_freq(m: float) -> float:
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


def _vel_x(vel: int) -> float:
    return float(np.clip((vel - SOFT_VEL) / (HARD_VEL - SOFT_VEL), 0.0, 1.0))


def vel_amp(vel: int) -> float:
    """ベロシティ → 音量。"""
    x = _vel_x(vel)
    # 強打層を2乗で混ぜるぶん、中くらいの強さで音量が下がるのを補う(録音の中間の層に合わせた)
    return 0.55 * (vel / 127.0) ** 1.7 * 10 ** (9.0 * np.sqrt(x) * (1 - x) / 20)


def vel_bright(vel: int) -> float:
    """ベロシティ → 強打層(high)を混ぜる量。弱打の録音の強さ以下では 0。

    高い倍音はハンマーの速さに対して急に増えるので、2乗で混ぜると録音の中間の層に近くなる。
    """
    return _vel_x(vel) ** 2


def _sample_level(vel: int) -> float:
    return _SAMPLE_GAIN * (vel / 127.0) ** (2 * _SAMPLE_VELTRACK) * _PAN_COMP


_MODEL = None
_MODEL_LOCK = threading.Lock()


def _model() -> dict:
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            if not os.path.exists(MODEL_PATH):
                raise FileNotFoundError(f"合成ピアノの倍音表がありません: {MODEL_PATH}\n"
                                        "python tools/build_piano_model.py で作成してください")
            with np.load(MODEL_PATH) as z:
                _MODEL = {k: z[k] for k in z.files}
                _MODEL["db"] = _MODEL["db"].astype(np.float64)
    return _MODEL


def _db_at(db: np.ndarray, times: np.ndarray, t: np.ndarray) -> np.ndarray:
    """測定点の dB を時刻 t に補間する。測定範囲の前後は端の傾きで延長する。"""
    out = np.empty((len(db), len(t)))
    s0 = np.clip((db[:, 1] - db[:, 0]) / (times[1] - times[0]), -80.0, 0.0)
    s1 = np.clip((db[:, -1] - db[:, -2]) / (times[-1] - times[-2]), -80.0, -0.5)
    for k in range(len(db)):
        out[k] = np.interp(t, times, db[k])
    early, late = t < times[0], t > times[-1]
    out[:, early] = db[:, :1] + np.minimum(s0[:, None] * (t[early] - times[0]), 12.0)
    out[:, late] = db[:, -1:] + s1[:, None] * (t[late] - times[-1])
    return out


def _note_params(m: int):
    """録音のある両隣の鍵盤から、この鍵盤の f0・B・倍音の dB 表を補間する。"""
    z = _model()
    notes = z["notes"]
    i = int(np.clip(np.searchsorted(notes, m, side="right") - 1, 0, len(notes) - 2))
    w = float(np.clip((m - notes[i]) / (notes[i + 1] - notes[i]), 0.0, 1.0))

    cents = 1200 * np.log2(z["f0"][[i, i + 1]].mean(1) / [midi_to_freq(notes[i]), midi_to_freq(notes[i + 1])])
    f0 = midi_to_freq(m) * 2 ** (np.clip((1 - w) * cents[0] + w * cents[1], -30, 30) / 1200)
    logB = np.log(np.maximum(z["B"][[i, i + 1]], 1e-7)).mean(1)
    B = float(np.exp((1 - w) * logB[0] + w * logB[1]))

    t = np.array(MODEL_TIMES)
    a, b = z["db"][i], z["db"][i + 1]  # [層, 倍音, 時刻]
    # 片方にしか無い倍音はもう片方の値をそのまま使う
    a = np.where(a <= -150, b, a)
    b = np.where(b <= -150, a, b)
    db = (1 - w) * a + w * b
    return f0, B, db, t


def _knock(m: int, rng, n: int) -> tuple[np.ndarray, np.ndarray]:
    """打撃音(弱打, 強打)。測ったパワー密度になるよう、白色雑音を短時間フーリエ変換で整形する。"""
    z = _model()
    notes = z["notes"]
    i = int(np.clip(np.searchsorted(notes, m, side="right") - 1, 0, len(notes) - 2))
    w = float(np.clip((m - notes[i]) / (notes[i + 1] - notes[i]), 0.0, 1.0))
    gain = (1 - w) * z["knock_gain"][i] + w * z["knock_gain"][i + 1]
    tilt = (1 - w) * z["knock_tilt"][i] + w * z["knock_tilt"][i + 1]
    octs = np.log2(KNOCK_BANDS / 1000.0)
    times = z["knock_times"]

    N, hop = 1024, 512
    frames = n // hop + 2
    noise = rng.standard_normal(frames * hop + N)
    win = np.sqrt(np.hanning(N + 1)[:N])  # 分析・合成の両方に掛けて、重ねると 1 になる
    fr = np.fft.rfftfreq(N, 1 / SR)
    lf = np.log(np.maximum(fr, 10.0))
    tf = (np.arange(frames) * hop - N / 2) / SR
    spec = np.fft.rfft(np.lib.stride_tricks.sliding_window_view(noise, N)[::hop][:frames] * win, axis=1)
    out = []
    for layer, vel in enumerate((SOFT_VEL, HARD_VEL)):
        db = (z["knock_shape"][layer] + (gain[layer] + tilt[layer] * octs)[:, None]
              + 20 * np.log10(_sample_level(vel) / vel_amp(vel)))
        # 帯 × 時刻 → 各フレームの各周波数
        slope = np.minimum((db[:, -1] - db[:, -2]) / (times[-1] - times[-2]), -6.0)
        dbt = np.array([np.interp(tf, times, row) for row in db])
        late = tf > times[-1]
        dbt[:, late] = db[:, -1:] + slope[:, None] * (tf[late] - times[-1])
        g = np.array([np.interp(lf, np.log(KNOCK_BANDS), col) for col in dbt.T])
        g = np.sqrt(10 ** (g / 10) * SR / 2)  # 分散1の白色雑音のパワー密度は 2/SR
        y = np.fft.irfft(spec * g, N, axis=1) * win
        sig = np.zeros(frames * hop + N)
        for f in range(frames):
            sig[f * hop:f * hop + N] += y[f]
        out.append(sig[N // 2:N // 2 + n])
    return out[0], out[1]


def _render(m: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(1000 + m)
    f0, B, db, times = _note_params(m)

    nyq = min(SR * 0.45, 18000.0)
    ks = np.arange(1, db.shape[1] + 1)
    fk = ks * f0 * np.sqrt(1.0 + B * ks * ks)
    K = int(np.sum(fk < nyq))
    fk, db = fk[:K], db[:, :K]

    # 弱打・強打の録音と同じ音量になる、エンジン側の low / high の振幅
    soft = db[0] + 20 * np.log10(_sample_level(SOFT_VEL) / vel_amp(SOFT_VEL))
    hard = db[1] + 20 * np.log10(_sample_level(HARD_VEL) / vel_amp(HARD_VEL))

    # 長さ: 一番大きい倍音が -80dB まで減衰するまで
    hop = 256
    tc = np.arange(0, MAX_SECONDS, hop / SR)
    env_s = _db_at(soft, times, tc)
    env_h = _db_at(hard, times, tc)
    top = max(env_s.max(), env_h.max())
    alive = np.maximum(env_s, env_h).max(0) > top - 80
    length = min(int(SR * MAX_SECONDS), max(int(SR * 1.5), (int(np.argmin(alive)) or len(tc)) * hop))
    t = np.arange(length) / SR

    # 1音あたりの弦の本数。弦ごとにわずかに音程と非調和性が違うので、倍音ごとに違ううなりが出る
    n_strings = 1 if m < 31 else (2 if m < 43 else 3)
    detune = rng.uniform(-0.6, 0.6, n_strings) if n_strings > 1 else np.zeros(1)
    detune -= detune.mean()
    b_var = 1 + rng.uniform(-0.03, 0.03, n_strings)

    low = np.zeros(length)
    high = np.zeros(length)
    for k in range(K):
        es = 10 ** (env_s[k] / 20)
        eh = 10 ** (env_h[k] / 20)
        live = np.nonzero(np.maximum(es, eh) > 10 ** ((top - 90) / 20))[0]
        if len(live) == 0:
            continue
        n = min(length, (live[-1] + 1) * hop)
        tt = t[:n]
        wave = np.zeros(n)
        for d, bv in zip(detune, b_var):
            f = (k + 1) * f0 * 2 ** (d / 1200) * np.sqrt(1.0 + B * bv * (k + 1) ** 2)
            # ハンマーが全部の弦・倍音を同時に叩くので、位相はそろえて始める
            wave += np.sin(2 * np.pi * f * tt + rng.uniform(-0.3, 0.3))
        wave /= n_strings
        ls = np.interp(tt, tc, es)
        lh = np.interp(tt, tc, eh)
        low[:n] += ls * wave
        high[:n] += (lh - ls) * wave

    # 打鍵の立ち上がり(ハンマーが弦に触れている時間): 高音ほど短い
    tau = 0.0006 + 0.0014 * np.clip((96 - m) / 75, 0, 1)
    attack = 1.0 - np.exp(-t / tau)
    low *= attack
    high *= attack

    # 響板・ケースの打撃音
    ks, kh = _knock(m, rng, min(length, int(SR * 2.0)))
    low[: len(ks)] += ks
    high[: len(kh)] += kh - ks
    ref = np.abs(low + high).max()

    # 途中で打ち切る音は最後をなめらかに消す
    if length >= int(SR * MAX_SECONDS):
        fade = int(SR * 0.5)
        ramp = np.linspace(1, 0, fade)
        low[-fade:] *= ramp
        high[-fade:] *= ramp
    # 強打の差分が -60dB まで小さくなったところで high を打ち切る(メモリを節約)
    keep = np.nonzero(np.abs(high) > ref * 1e-3)[0]
    hn = (keep[-1] + 1) if len(keep) else 1
    fade = min(hn, int(SR * 0.05))
    high = high[:hn]
    high[hn - fade:] *= np.linspace(1, 0, fade)
    return low.astype(np.float32), high.astype(np.float32)


class NoteBank:
    """鍵盤ごとの合成済み波形を保持する。"""

    def __init__(self) -> None:
        self._notes: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._lock = threading.Lock()

    def has(self, m: int) -> bool:
        return m in self._notes

    def get(self, m: int) -> tuple[np.ndarray, np.ndarray]:
        note = self._notes.get(m)
        if note is None:
            note = self.load(m)
        return note

    def load(self, m: int) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if m in self._notes:
                return self._notes[m]
        path = os.path.join(CACHE_DIR, f"v{CACHE_VERSION}_{m}.npz")
        note = None
        if os.path.exists(path):
            try:
                with np.load(path) as z:
                    note = (z["low"], z["high"])
            except Exception:
                note = None
        if note is None:
            note = _render(m)
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp.npz"
            np.savez(tmp, low=note[0], high=note[1])
            os.replace(tmp, path)
        with self._lock:
            self._notes[m] = note
        return note

    def prepare(self, pitches, progress=None) -> None:
        """必要な音をまとめて用意する(未キャッシュなら合成)。"""
        todo = sorted(set(p for p in pitches if 21 <= p <= 108 and not self.has(p)))
        if not todo:
            return
        workers = max(1, min(8, (os.cpu_count() or 2) - 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, _ in enumerate(as_completed([pool.submit(self.load, m) for m in todo])):
                if progress:
                    progress(i + 1, len(todo))

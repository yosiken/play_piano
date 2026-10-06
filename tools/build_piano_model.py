"""録音ピアノを分析して、合成ピアノ用の倍音パラメータを作る。

  python tools/build_piano_model.py

sounds/ の Salamander 音源(30鍵 × 弱打・強打)から、各倍音の周波数(非調和性)と
時間ごとの音量(dB)、倍音以外の打撃音(ハンマーが当たったときの響板・ケースの「コツン」)の
スペクトルを測って play_piano/piano_model.npz に保存する。
合成ピアノはこの表だけを使い、実行時に WAV は読まない。
"""
from __future__ import annotations

import os
import sys
import warnings

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from play_piano.sampler import _open_wav, find_sfz  # noqa: E402
from play_piano.synth import KNOCK_BANDS, MODEL_PATH, MODEL_TIMES, SR, midi_to_freq  # noqa: E402

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
NOTES = list(range(21, 109, 3))  # A0, C1, D#1, ... C8(録音のある鍵盤)
LAYERS = (2, 16)                 # 弱打(ベロシティ27〜34)・強打(121〜127)
MAX_PARTIALS = 256
MAX_FREQ = 18000.0
# 打撃音を測る窓(打鍵からの開始時刻[s], 長さ[サンプル])
KNOCK_WINDOWS = ((0.0, 2048), (0.02, 2048), (0.06, 4096), (0.15, 8192), (0.4, 16384))


def _bh(n: int) -> np.ndarray:
    """Blackman-Harris 窓(サイドローブ -92dB)。強い倍音のもれを抑える。"""
    x = 2 * np.pi * np.arange(n) / (n - 1)
    return 0.35875 - 0.48829 * np.cos(x) + 0.14128 * np.cos(2 * x) - 0.01168 * np.cos(3 * x)


def _spectrum(x: np.ndarray, a: int, n: int):
    """x[a:a+n] の振幅スペクトル(正弦波の振幅そのものになるよう正規化)。"""
    seg = x[a:a + n]
    if len(seg) < n:
        seg = np.pad(seg, (0, n - len(seg)))
    w = _bh(n)
    nfft = 1 << (4 * n - 1).bit_length()
    return np.abs(np.fft.rfft(seg * w, nfft)) * 2 / w.sum(), SR / nfft


def _peak(mag, df, f, half):
    lo, hi = max(1, int((f - half) / df)), int((f + half) / df) + 1
    if hi <= lo:
        return 0.0, f
    i = lo + int(np.argmax(mag[lo:hi]))
    # 放物線補間で周波数を細かく
    if 0 < i < len(mag) - 1:
        a, b, c = np.log(mag[i - 1:i + 2] + 1e-20)
        d = 0.5 * (a - c) / (a - 2 * b + c) if a - 2 * b + c < 0 else 0.0
    else:
        d = 0.0
    return mag[i], (i + d) * df


def fit_partials(x: np.ndarray, onset: int, m: int):
    """f0 と非調和係数 B を推定する(fk = k·f0·√(1+B·k²))。

    音の高さを決める下の方の倍音(30本まで)だけで当てはめる。
    戻り値の n は当てはめに使えた倍音の数(少ないと B は信用できない)。
    """
    f0 = midi_to_freq(m)
    n = min(len(x) - onset, int(SR * 1.5))
    mag, df = _spectrum(x, onset + int(SR * 0.03), n)
    B = 1e-4 * 2.0 ** ((m - 60) / 18.0)
    ks, fs = [], []
    floor = np.median(mag) + 1e-12
    first = None
    for k in range(1, 31):
        guess = k * f0 * np.sqrt(1 + B * k * k)
        if guess > 8000.0:
            break
        a, f = _peak(mag, df, guess, 0.2 * midi_to_freq(m))
        if k == 1:
            first = f
        if a > 30 * floor:
            ks.append(k); fs.append(f)
        if len(ks) >= 5:
            K, F = np.array(ks, float), np.array(fs)
            # fk² = f0²k² + f0²B k⁴ を最小二乗で解く(各式を k² で割って重みをそろえる)
            A = np.stack([np.ones_like(K), K ** 2], 1)
            c, *_ = np.linalg.lstsq(A, (F / K) ** 2, rcond=None)
            if c[0] > 0 and c[1] >= 0:
                f0, B = float(np.sqrt(c[0])), float(c[1] / c[0])
    if len(ks) < 5 and first is not None:
        f0 = first  # 倍音が少ない高音は基音の周波数をそのまま使う
    return f0, B, len(ks)


def load(path: str):
    d = np.asarray(_open_wav(path), dtype=np.float64) / 32768.0
    x = d.mean(1) if d.ndim == 2 else d
    onset = int(np.argmax(np.abs(x) > 0.1 * np.abs(x).max()))
    return x, max(0, onset - int(SR * 0.002))


def measure(x: np.ndarray, onset: int, f0: float, B: float):
    """各倍音の音量(dB)を MODEL_TIMES の時刻ごとに測る。"""
    fk = partial_freqs(f0, B)
    K = len(fk)
    db = np.full((K, len(MODEL_TIMES)), np.nan)
    centers = np.zeros(len(MODEL_TIMES))
    end = len(x)
    for j, t in enumerate(MODEL_TIMES):
        L = int(SR * min(2.0, max(12.0 / f0, 0.02 + 0.3 * t)))
        # 窓は打鍵より前にはみ出さないようにする(低音は窓が長いので、測る時刻が少し後ろにずれる)
        c = max(t, L / 2 / SR, centers[j - 1] + 0.05 if j else 0.0)
        centers[j] = c
        a = onset + int(SR * c) - L // 2
        if a + L > end:
            continue
        mag, df = _spectrum(x, a, L)
        amp = np.array([_peak(mag, df, f, 0.12 * f0)[0] for f in fk])
        # 倍音と倍音の間の音量 = 雑音の床
        mid = np.array([_peak(mag, df, f + 0.5 * f0, 0.1 * f0)[0] for f in fk])
        floor = np.array([np.median(mid[max(0, k - 3):k + 4]) for k in range(K)])
        ok = amp > 4 * floor
        db[ok, j] = 20 * np.log10(amp[ok])
    return _regrid(_fill(db, centers), centers)


def partial_freqs(f0: float, B: float) -> np.ndarray:
    ks = np.arange(1, MAX_PARTIALS + 1)
    fk = ks * f0 * np.sqrt(1 + B * ks * ks)
    return fk[fk < MAX_FREQ]


def band_psd(x: np.ndarray, a: int, L: int, fk: np.ndarray, f0: float) -> np.ndarray:
    """倍音の周波数を避けて、1/3オクターブ帯ごとのパワー密度(dB)を測る。測れない帯は NaN。"""
    seg = x[a:a + L]
    if len(seg) < L:
        seg = np.pad(seg, (0, L - len(seg)))
    w = _bh(L)
    P = 2 * np.abs(np.fft.rfft(seg * w)) ** 2 / (SR * (w ** 2).sum())
    f = np.fft.rfftfreq(L, 1 / SR)
    i = np.clip(np.searchsorted(fk, f), 1, len(fk) - 1)
    dist = np.minimum(np.abs(f - fk[i - 1]), np.abs(fk[i] - f))
    dist = np.where(f < fk[0], fk[0] - f, dist)
    free = dist > 4 * SR / L + 0.03 * f0  # 窓のメインローブ + 余裕
    out = np.full(len(KNOCK_BANDS), np.nan)
    for b, c in enumerate(KNOCK_BANDS):
        sel = free & (f >= c * 2 ** (-1 / 6)) & (f < c * 2 ** (1 / 6))
        if sel.sum() >= 2:
            out[b] = 10 * np.log10(P[sel].mean() + 1e-30)
    return out


def measure_knock(x: np.ndarray, onset: int, f0: float, B: float, floor: np.ndarray) -> np.ndarray:
    """打撃音のパワー密度 [帯, 窓]。録音の背景雑音(floor)を差し引き、埋もれた所は NaN。"""
    fk = partial_freqs(f0, B)
    out = np.full((len(KNOCK_BANDS), len(KNOCK_WINDOWS)), np.nan)
    for j, (t, L) in enumerate(KNOCK_WINDOWS):
        p = 10 ** (band_psd(x, onset + int(SR * t), L, fk, f0) / 10)
        q = p - 10 ** (floor / 10)
        ok = q > 0.5 * p  # 背景雑音より 3dB 以上大きい所だけ
        out[ok, j] = 10 * np.log10(q[ok])
    return out


def _regrid(db: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """実際に測った時刻から MODEL_TIMES の時刻へ。打鍵直後は最初の傾きで延長する。"""
    T = np.array(MODEL_TIMES)
    out = np.empty_like(db)
    for k in range(len(db)):
        out[k] = np.interp(T, centers, db[k])
        slope = np.clip((db[k, 1] - db[k, 0]) / (centers[1] - centers[0]), -80.0, 0.0)
        early = T < centers[0]
        out[k, early] = db[k, 0] + np.minimum(slope * (T[early] - centers[0]), 12.0)
    return out


def _fill(db: np.ndarray, T: np.ndarray) -> np.ndarray:
    """雑音に埋もれた所は、直前の減衰の傾きで延長する。"""
    out = db.copy()
    for k in range(len(db)):
        row = out[k]
        valid = np.nonzero(~np.isnan(row))[0]
        if len(valid) == 0:
            out[k] = -160.0
            continue
        # 打鍵直後はハンマーの雑音に埋もれる弱い倍音(低音の基音など): 最初に測れた値でうめる
        row[: valid[0]] = row[valid[0]]
        slope = -20.0
        for j in range(valid[0] + 1, len(T)):
            if np.isnan(row[j]):
                row[j] = row[j - 1] + slope * (T[j] - T[j - 1])
            else:
                slope = min(-1.0, (row[j] - row[j - 1]) / (T[j] - T[j - 1]))
                row[j] = min(row[j], row[j - 1] + 3.0)  # うなりによる一時的な盛り上がりはならす
    return out


def main() -> None:
    sfz = find_sfz()
    if not sfz:
        raise SystemExit("sounds/ に Salamander 音源が見つかりません")
    folder = os.path.join(os.path.dirname(sfz), "44.1khz16bit")
    shape = (len(NOTES), len(LAYERS))
    f0s, Bs, used = np.zeros(shape), np.zeros(shape), np.zeros(shape, int)
    waves = {}
    for i, m in enumerate(NOTES):
        name = f"{NAMES[m % 12]}{m // 12 - 1}"
        for j, v in enumerate(LAYERS):
            x, onset = load(os.path.join(folder, f"{name}v{v}.wav"))
            waves[i, j] = (x, onset)
            f0s[i, j], Bs[i, j], used[i, j] = fit_partials(x, onset, m)

    # 非調和係数は鍵盤に沿ってなめらかに変わる。倍音が少なく測れない高音は、測れた鍵盤の傾向から決める
    notes = np.repeat(np.array(NOTES)[:, None], len(LAYERS), 1)
    et = np.vectorize(midi_to_freq)(notes)
    cents = 1200 * np.log2(f0s / et)
    ok = (used >= 6) & (np.abs(cents) < 40) & (Bs > 1e-5) & (Bs < 5e-3)
    Bs = np.where(ok, Bs, np.exp(np.polyval(np.polyfit(notes[ok], np.log(Bs[ok]), 3), notes)))
    # 調律(低音を低め・高音を高めにするストレッチ)もなめらかな曲線にする
    stretch = np.clip(np.polyval(np.polyfit(notes[ok], cents[ok], 3), notes), -35, 35)
    f0s = et * 2 ** (stretch / 1200)

    dbs = np.full(shape + (MAX_PARTIALS, len(MODEL_TIMES)), -160.0)
    for i, m in enumerate(NOTES):
        name = f"{NAMES[m % 12]}{m // 12 - 1}"
        for j, v in enumerate(LAYERS):
            db = measure(*waves[i, j], f0s[i, j], Bs[i, j])
            dbs[i, j, :len(db)] = db
            cents = 1200 * np.log2(f0s[i, j] / midi_to_freq(m))
            print(f"{name:4s} v{v:<2d} 倍音{len(db):3d}本  B={Bs[i, j]:.2e}{'' if ok[i, j] else '(推定)'}  "
                  f"{cents:+5.1f}セント  基音 {db[0, 0]:6.1f}dB → 1秒後 {db[0, 4]:6.1f}dB")

    # 録音の背景雑音: 高音の録音の最後の方(弦の音はほぼ消えている)
    tails = []
    for i, m in enumerate(NOTES):
        if m >= 96:
            x, _ = waves[i, 0]
            tails.append(band_psd(x, len(x) - 16384, 16384, partial_freqs(f0s[i, 0], Bs[i, 0]), f0s[i, 0]))
    floor = np.nanmedian(tails, axis=0)
    floor = np.where(np.isnan(floor), np.nanmax(floor), floor)

    # 打撃音: スペクトルの形は倍音がまばらで測りやすい高音から、鍵盤ごとの音量はそれぞれの録音から求める
    knock = np.array([[measure_knock(*waves[i, j], f0s[i, j], Bs[i, j], floor) for j in range(len(LAYERS))]
                      for i in range(len(NOTES))])  # [鍵盤, 層, 帯, 窓]
    high = np.array(NOTES) >= 84
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # 全部 NaN の帯は後で補間する
        shape_ = np.nanmedian(knock[high], axis=0)  # [層, 帯, 窓]
    for j in range(len(LAYERS)):
        for w in range(len(KNOCK_WINDOWS)):
            col = shape_[j, :, w]
            ok_ = ~np.isnan(col)
            shape_[j, :, w] = np.interp(np.arange(len(col)), np.nonzero(ok_)[0], col[ok_])
    # 鍵盤ごとの音量と傾き(低音のハンマーは大きく柔らかいので、打撃音もこもる)を当てはめる
    octs = np.log2(KNOCK_BANDS / 1000.0)
    gain = np.full((len(NOTES), len(LAYERS)), np.nan)
    tilt = np.full((len(NOTES), len(LAYERS)), np.nan)
    for i in range(len(NOTES)):
        for j in range(len(LAYERS)):
            d = (knock[i, j] - shape_[j])[:, :3]  # 打撃音そのもの(打鍵直後)の窓だけで当てはめる
            ok_ = ~np.isnan(d)
            o = np.broadcast_to(octs[:, None], d.shape)[ok_]
            # 倍音が密な低音は打鍵直後の打撃音を測れないので、測れた鍵盤の値を使う
            if ok_.sum() >= 12 and ok_.any(0).sum() >= 2 and np.ptp(o) >= 2.0:
                A = np.stack([np.ones_like(o), o], 1)
                (gain[i, j], tilt[i, j]), *_ = np.linalg.lstsq(A, d[ok_], rcond=None)
    tilt = np.clip(tilt, -6.0, 3.0)
    # 弱打・強打の両方を測れた鍵盤だけを使う(片方しか測れない低音は、当てはめが不安定)
    measured = ~np.isnan(gain).any(1)
    gain[~measured] = np.nan
    tilt[~measured] = np.nan
    for arr in (gain, tilt):
        for j in range(len(LAYERS)):
            ok_ = ~np.isnan(arr[:, j])
            arr[:, j] = np.interp(NOTES, np.array(NOTES)[ok_], arr[ok_, j])
    for i, m in enumerate(NOTES):
        print(f"打撃音 {NAMES[m % 12]}{m // 12 - 1:<2d}{'' if measured[i] else '(推定)'} 弱 {gain[i, 0]:+5.1f}dB {tilt[i, 0]:+4.1f}dB/oct  "
              f"強 {gain[i, 1]:+5.1f}dB {tilt[i, 1]:+4.1f}dB/oct")
    centers = np.array([t + L / 2 / SR for t, L in KNOCK_WINDOWS])

    np.savez_compressed(MODEL_PATH, notes=np.array(NOTES), layers=np.array(LAYERS),
                        f0=f0s, B=Bs, db=dbs.astype(np.float16), times=np.array(MODEL_TIMES),
                        knock_shape=shape_, knock_gain=gain, knock_tilt=tilt, knock_times=centers)
    print("保存しました:", MODEL_PATH)


if __name__ == "__main__":
    main()

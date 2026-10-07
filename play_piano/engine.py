"""リアルタイム発音エンジン。

イベント(ノートオン/オフ・ペダル)をサンプル時刻付きで受け取り、
sounddevice のコールバック(またはオフライン書き出し)で波形を合成する。

信号の流れ:
  各音(響板・音色込み) → ステレオ配置(マイク間の時間差)
    → + 弦の共鳴(ペダル時は全弦、高音弦は常時)
    → + ホール残響
    → + 機械ノイズ(ダンパー・ペダル)
    → リミッター
"""
from __future__ import annotations

import heapq
import itertools
import threading
from collections import deque

import numpy as np

from . import acoustics
from .acoustics import ROOMS, Convolver
from .sampler import SampleVoice, SFZBank
from .strings import StringBank
from .synth import CACHE_DIR, NO_DAMPER_FROM, SR, NoteBank, vel_amp, vel_bright

BLOCK = 1024
SAMPLE_GAIN = 1.6  # サンプル音源の音量を合成音とそろえる係数


class _Voice:
    __slots__ = ("note", "low", "high", "pos", "amp", "bright", "gains", "delays",
                 "key_down", "level", "fast_damp", "vel", "inst")

    def __init__(self, note, low, high, offset, vel, inst=""):
        self.note = note
        self.inst = inst
        self.low = low
        self.high = high
        self.pos = -offset  # ブロック先頭での音の内部位置(負 = まだ鳴っていない)
        self.vel = vel
        self.amp = vel_amp(vel)
        self.bright = vel_bright(vel)
        x = (note - 64.5) / 43.5  # -1(低音) 〜 +1(高音)
        pan = 0.5 + 0.28 * x
        self.gains = (float(np.cos(pan * np.pi / 2)), float(np.sin(pan * np.pi / 2)))
        # 奏者側のマイク配置: 低音は左マイクに先に届く(最大0.35ms)
        d = int(round(abs(x) * 0.00035 * SR))
        self.delays = (d, 0) if x > 0 else (0, d)
        self.key_down = True
        self.level = 1.0
        self.fast_damp = False

    def read(self, start, n):
        """内部位置 start から n サンプル(範囲外は0)。"""
        out = np.zeros(n)
        a, b = max(0, start), min(len(self.low), start + n)
        if b > a:
            out[a - start:b - start] = self.low[a:b]
            hb = min(b, len(self.high))
            if hb > a:
                out[a - start:hb - start] += self.bright * self.high[a:hb]
        return out


def _noise_samples():
    rng = np.random.default_rng(11)

    def burst(dur, tau, lp, hp=0):
        n = int(SR * dur)
        x = rng.standard_normal(n)
        spec = np.fft.rfft(x)
        f = np.fft.rfftfreq(n, 1 / SR)
        spec *= 1 / (1 + (f / lp) ** 2) * (1 / (1 + (hp / np.maximum(f, 1)) ** 2) if hp else 1)
        y = np.fft.irfft(spec, n) * np.exp(-np.arange(n) / (SR * tau))
        return y / np.abs(y).max()

    return {
        "damper": burst(0.06, 0.012, 900, 80) * 0.010,   # ダンパーのフェルトが弦に触れる音
        "pedal_down": burst(0.15, 0.03, 400, 40) * 0.020,  # ペダルを踏む音
        "pedal_up": burst(0.25, 0.06, 1800, 150) * 0.012,  # ダンパーが一斉に戻る音
    }


class AudioEngine:
    def __init__(self, bank: NoteBank | StringBank | None = None, max_voices: int = 96,
                 room: str = "コンサートホール", sampler: SFZBank | None = None):
        self.bank = bank or NoteBank()  # 合成音: ピアノ(NoteBank) またはギター・ベース(StringBank)
        self.sampler = sampler  # None なら合成音
        # 合奏で使う、ほかの楽器の合成音(楽器のキー → StringBank)
        self.extra_banks: dict[str, StringBank] = {}
        self.shots: list[SampleVoice] = []  # 録音されたリリース音・ペダル音
        self._rand = np.random.default_rng(0)
        self.max_voices = max_voices
        self.clock = 0  # 次に出力するサンプル番号
        self.voices: list[_Voice] = []
        self.oneshots: list[list] = []  # [波形, 位置, ゲイン]
        self.pedal_down = False
        self.pedal_depth = 1.0
        self.master = 0.6
        self.resonance = 1.0  # 弦の共鳴の量(0〜2)
        self.noise = 1.0      # 機械ノイズの量(0〜2)
        self._events = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._noises = _noise_samples()
        self._pedal_gain = 0.0
        self._lim_gain = 1.0
        self._res_conv = Convolver(list(_load_resonance()))
        self.room = ""
        self.wet = 0.0
        self._rev_conv = None
        self.set_room(room)
        # GUI表示用 (note, vel) / ('pedal', bool)
        self.visual: deque = deque(maxlen=4096)
        self._stream = None

    @property
    def plucked(self) -> bool:
        """ギター・ベースを鳴らしているか(ピアノのペダル音や弦の共鳴は鳴らさない)。"""
        return self.sampler is None and isinstance(self.bank, StringBank)

    def _bank_for(self, inst: str):
        if not inst or inst == getattr(self.bank, "kind", None):
            return self.bank
        if inst not in self.extra_banks:
            self.extra_banks[inst] = StringBank(inst)
        return self.extra_banks[inst]

    def prepare_notes(self, notes, progress=None) -> None:
        """曲の音符(合奏なら楽器ごと)に必要な音を準備する。"""
        by_inst: dict[str, list[int]] = {}
        for n in notes:
            by_inst.setdefault(n.inst, []).append(n.pitch)
        for inst, pitches in by_inst.items():
            bank = self._bank_for(inst)
            if bank is self.bank:
                self.prepare(pitches, progress)
            else:
                bank.prepare(pitches, progress)

    def prepare(self, pitches, progress=None) -> None:
        """曲で使う音を準備する(合成音なら合成、サンプルなら先読み)。"""
        if self.sampler is not None:
            self.sampler.prepare(pitches, progress)
        else:
            self.bank.prepare(pitches, progress)

    def set_room(self, name: str) -> None:
        rt60, spread, wet = ROOMS[name]
        conv = Convolver([acoustics.hall_ir(rt60, spread)]) if rt60 > 0 else None
        self._rev_conv, self.wet, self.room = conv, wet, name

    # ---- スケジューリング(どのスレッドからでも可) ----
    def schedule(self, sample_time: int, kind: str, *args) -> None:
        with self._lock:
            heapq.heappush(self._events, (int(sample_time), next(self._seq), kind, args))

    def clear_scheduled(self) -> None:
        with self._lock:
            self._events.clear()
            heapq.heappush(self._events, (0, next(self._seq), "all_off", ()))

    # ---- イベント処理 ----
    def _oneshot(self, name, gain, offset=0):
        if self.noise > 0:
            self.oneshots.append([self._noises[name], -offset, gain * self.noise])

    def _note_on(self, note, vel, offset, inst=""):
        if not 21 <= note <= 108 or vel <= 0:
            return
        for v in self.voices:
            if v.note == note and getattr(v, "inst", "") == inst:
                v.fast_damp = True  # 同じ鍵盤の打ち直し
        if len(self.voices) >= self.max_voices:
            self.voices.sort(key=lambda v: v.level * v.amp)
            del self.voices[: len(self.voices) - self.max_voices + 1]
        if self.sampler is not None and not inst:
            voice = SampleVoice(note, self.sampler.find(note, vel), offset, vel, SAMPLE_GAIN)
            voice.t_on = self.clock + offset
        else:
            low, high = self._bank_for(inst).get(note)
            voice = _Voice(note, low, high, offset, vel, inst)
        self.voices.append(voice)
        self.visual.append((note, vel))

    def _damper_sound(self, v, offset):
        """ダンパーが下りる音。サンプル音源では録音された弦の余韻とハンマー音を使う。"""
        if self.sampler is None:
            # ギター・ベースでは指で弦を押さえて止める、小さな音
            self._oneshot("damper", (0.4 + v.vel / 127) * (0.5 if self.plucked else 1.0), offset)
            return
        held = max(0.0, (self.clock + offset - v.t_on) / SR)
        for z in self.sampler.find_release(v.note, v.vel):
            mech = z.keytrack == 0  # ハンマー・ダンパーの機械音
            gain = SAMPLE_GAIN * 10 ** (-z.rt_decay * held / 20) * (self.noise if mech else 1.0)
            if gain > 1e-3:
                self.shots.append(SampleVoice(v.note, z, offset, v.vel, gain))

    def _pedal_sound(self, down, offset):
        if self.plucked:
            return
        zones = (self.sampler.pedal_down if down else self.sampler.pedal_up) if self.sampler else []
        if zones:
            z = zones[int(self._rand.integers(len(zones)))]
            self.shots.append(SampleVoice(z.center, z, offset, 100, SAMPLE_GAIN * self.noise))
        else:
            self._oneshot("pedal_down" if down else "pedal_up", 1.0, offset)

    def _note_off(self, note, offset, inst=""):
        for v in self.voices:
            if v.note == note and v.key_down and getattr(v, "inst", "") == inst:
                v.key_down = False
                if not self.pedal_down and note < NO_DAMPER_FROM:
                    self._damper_sound(v, offset)
        self.visual.append((note, 0))

    def _apply(self, kind, args, offset):
        if kind == "on":
            self._note_on(args[0], args[1], offset, *args[2:])
        elif kind == "off":
            self._note_off(args[0], offset, *args[1:])
        elif kind == "pedal":
            down = bool(args[0])
            if down != self.pedal_down:
                self._pedal_sound(down, offset)
                if not down:
                    # ペダルを離すと、指を離していた音のダンパーが一斉に下りる
                    held = [v for v in self.voices if not v.key_down and not v.fast_damp and v.note < NO_DAMPER_FROM]
                    held.sort(key=lambda v: -v.level * v.amp)
                    for v in held[:12]:
                        self._damper_sound(v, offset)
            self.pedal_down = down
            self.visual.append(("pedal", down))
        elif kind == "pedal_depth":
            self.pedal_depth = float(args[0])
        elif kind == "all_off":
            for v in self.voices:
                v.key_down = False
                v.fast_damp = True
            self.pedal_down = False
            self.shots.clear()
            self.visual.append(("all_off", None))

    # ---- 合成 ----
    def render(self, frames: int) -> np.ndarray:
        end = self.clock + frames
        with self._lock:
            due = []
            while self._events and self._events[0][0] < end:
                due.append(heapq.heappop(self._events))
        for t, _, kind, args in due:
            self._apply(kind, args, max(0, t - self.clock))

        dry = np.zeros((frames, 2))
        ramp = np.arange(frames)
        alive = []
        for v in self.voices:
            start = max(0, int(np.ceil(-v.pos)))
            if start >= frames:
                v.pos += frames
                alive.append(v)
                continue
            n = frames - start
            sampled = isinstance(v, SampleVoice)

            # 減衰: ダンパー / ハーフペダル / 打ち直し
            tau = None
            if v.fast_damp:
                tau = 0.03
            elif not v.key_down and v.note < NO_DAMPER_FROM:
                if self.pedal_down:
                    if self.pedal_depth < 0.99:
                        tau = 0.25 + 12.0 * self.pedal_depth ** 2
                else:
                    tau = 0.09 if v.note > 50 else 0.18
            if tau is not None:
                env = v.level * np.exp(-ramp[:n] / (tau * SR))
                v.level = float(env[-1]) * np.exp(-1.0 / (tau * SR))
            else:
                env = v.level

            if sampled:
                v.pos = max(0.0, v.pos)
                dry[start:] += v.read(n) * (np.asarray(env) * v.amp).reshape(-1, 1)
                v.pos += n * v.step
                if v.level > 1e-3 and v.pos < v.length:  # -60dBで打ち切り
                    alive.append(v)
                continue
            q = max(0, v.pos)
            for ch in range(2):
                d = v.delays[ch]
                dry[start:, ch] += v.read(q - d, n) * (env * v.amp * v.gains[ch])
            v.pos = q + n
            if v.level > 1e-4 and v.pos - max(v.delays) < len(v.low):
                alive.append(v)
        self.voices = alive

        # 録音されたリリース音・ペダル音
        keep = []
        for v in self.shots:
            start = max(0, int(np.ceil(-v.pos)))
            if start >= frames:
                v.pos += frames
                keep.append(v)
                continue
            n = frames - start
            v.pos = max(0.0, v.pos)
            dry[start:] += v.read(n) * v.amp
            v.pos += n * v.step
            if v.pos < v.length:
                keep.append(v)
        self.shots = keep[-48:]

        mono = dry.mean(axis=1)

        # 弦の共鳴: ペダルの踏み込みに応じてなめらかに増減
        res = 0.0 if self.plucked else self.resonance
        target = (self.pedal_depth if self.pedal_down else 0.0) * res
        pg = self._pedal_gain + (target - self._pedal_gain) * np.minimum(1.0, (ramp + 1) / (0.05 * SR))
        self._pedal_gain = float(pg[-1])
        res_pedal, res_free = self._res_conv.process(mono)
        out = dry + res_pedal * (0.05 * pg)[:, None] + res_free * (0.02 * res)

        if self._rev_conv is not None:
            out += self._rev_conv.process(mono)[0] * self.wet

        # 機械ノイズ
        keep = []
        for shot in self.oneshots:
            wave, pos, gain = shot
            s = max(0, -pos)
            if s < frames:
                q = max(0, pos)
                seg = wave[q:q + frames - s] * gain
                out[s:s + len(seg)] += seg[:, None]
                shot[1] = q + len(seg)
                if shot[1] < len(wave):
                    keep.append(shot)
            else:
                shot[1] += frames
                keep.append(shot)
        self.oneshots = keep

        self.clock = end
        return self._limit(out * self.master)

    def _limit(self, x):
        """ピークを0.95以下に保つ(アタックは速く、リリースはゆっくり)。"""
        peak = np.abs(x).max()
        target = min(1.0, 0.95 / peak) if peak > 0 else 1.0
        g0 = self._lim_gain
        if target < g0:
            g = np.full(len(x), target)  # 超えそうなら即座に下げる
            g1 = target
        else:
            g1 = min(1.0, g0 + (1.0 - g0) * 0.02)
            g = np.linspace(g0, g1, len(x))
        x = x * g[:, None]
        self._lim_gain = g1
        return np.clip(x, -1.0, 1.0)

    # ---- リアルタイム出力 ----
    def start(self) -> None:
        import sounddevice as sd

        if self._stream is not None:
            return

        def cb(outdata, frames, time_info, status):
            outdata[:] = self.render(frames).astype(np.float32)

        self._stream = sd.OutputStream(samplerate=SR, channels=2, blocksize=BLOCK,
                                       dtype="float32", latency=0.25, callback=cb)  # 先読み再生なので遅延より安定を優先
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def _load_resonance():
    import os

    path = os.path.join(CACHE_DIR, "resonance_v1.npz")
    if os.path.exists(path):
        try:
            with np.load(path) as z:
                return z["pedal"], z["free"]
        except Exception:
            pass
    pedal, free = acoustics.resonance_irs()
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.savez(path, pedal=pedal.astype(np.float32), free=free.astype(np.float32))
    return pedal, free

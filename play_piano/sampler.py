"""SFZ形式のサンプル音源(Salamander Grand Piano など)を鳴らす。

WAVはメモリマップで開き、必要な部分だけを読む。録音されていない鍵盤は
近くのサンプルを再生速度を変えて(線形補間で)音程を合わせる。
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field

import numpy as np

from .synth import SR

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sounds")

_NOTE = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}


def _key(v: str) -> int:
    v = v.strip().lower()
    if v.lstrip("-").isdigit():
        return int(v)
    m = re.fullmatch(r"([a-g])([#b]?)(-?\d+)", v)
    if not m:
        raise ValueError(v)
    n = _NOTE[m.group(1)] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)
    return n + 12 * (int(m.group(3)) + 1)


def _open_wav(path: str) -> np.ndarray:
    """16bitステレオ/モノラルWAVのデータ部を (n, 2) の int16 メモリマップで返す。"""
    with open(path, "rb") as f:
        riff, _, wave = struct.unpack("<4sI4s", f.read(12))
        if riff != b"RIFF" or wave != b"WAVE":
            raise ValueError(f"WAVではありません: {path}")
        ch = bits = rate = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError(f"dataチャンクがありません: {path}")
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                fmt = f.read(size)
                _, ch, rate, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
            elif cid == b"data":
                offset = f.tell()
                break
            else:
                f.seek(size + (size & 1), 1)
    if bits != 16:
        raise ValueError(f"16bit以外は未対応です: {path}")
    if rate != SR:
        raise ValueError(f"{SR}Hz以外は未対応です: {path} ({rate}Hz)")
    mm = np.memmap(path, dtype="<i2", mode="r", offset=offset, shape=(size // (2 * ch), ch))
    return mm if ch == 2 else np.repeat(mm, 2, axis=1)


@dataclass
class Zone:
    path: str
    lokey: int
    hikey: int
    center: int
    lovel: int = 0
    hivel: int = 127
    volume: float = 0.0  # dB
    tune: float = 0.0    # セント
    veltrack: float = 100.0
    keytrack: float = 100.0  # 0 なら音程を変えずに再生
    rt_decay: float = 0.0    # リリース音: 押していた時間1秒あたりの減衰(dB)
    _data: np.ndarray | None = field(default=None, repr=False)

    @property
    def data(self) -> np.ndarray:
        if self._data is None:
            self._data = _open_wav(self.path)
        return self._data


class SFZBank:
    def __init__(self, sfz_path: str):
        self.path = sfz_path
        self.name = os.path.splitext(os.path.basename(sfz_path))[0]
        self.zones: list[Zone] = []
        self.release: list[Zone] = []
        self.pedal_down: list[Zone] = []
        self.pedal_up: list[Zone] = []
        self._parse(sfz_path)
        if not self.zones:
            raise ValueError("SFZにサンプルがありません")
        self._cache: dict[tuple[int, int], Zone | None] = {}

    def _parse(self, path):
        base = os.path.dirname(path)
        text = open(path, encoding="utf-8", errors="replace").read()
        text = re.sub(r"//[^\n]*", "", text)
        default_path = ""
        defines: dict[str, str] = {}
        levels = {"control": {}, "global": {}, "master": {}, "group": {}}
        tokens = re.split(r"(<\w+>)", text)
        header = None
        for tok in tokens:
            m = re.fullmatch(r"<(\w+)>", tok.strip())
            if m:
                header = m.group(1)
                if header in levels:
                    # 上位の見出しが来たら下位の設定をリセット
                    order = ["control", "global", "master", "group"]
                    for h in order[order.index(header):]:
                        levels[h] = {}
                if header == "region":
                    pass
                continue
            if header is None:
                continue
            for k, v in re.findall(r"#define\s+(\$\w+)\s+(\S+)", tok):
                defines[k] = v
            ops = self._opcodes(tok, defines)
            if header in levels:
                levels[header].update(ops)
                if header == "control" and "default_path" in ops:
                    default_path = ops["default_path"]
            elif header == "region":
                merged = {}
                for h in ("global", "master", "group"):
                    merged.update(levels[h])
                merged.update(ops)
                self._add_region(merged, base, default_path)

    @staticmethod
    def _opcodes(tok, defines):
        for k, v in defines.items():
            tok = tok.replace(k, v)
        ops = {}
        # sample= はスペースを含むことがあるので次の opcode= まで取る
        for m in re.finditer(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", tok.strip(), re.S):
            ops[m.group(1)] = m.group(2).strip()
        return ops

    def _add_region(self, o, base, default_path):
        if "sample" not in o:
            return
        path = os.path.normpath(os.path.join(base, default_path, o["sample"].replace("\\", "/")))
        if not os.path.exists(path):
            return
        if "key" in o:
            lo = hi = center = _key(o["key"])
        else:
            lo = _key(o.get("lokey", "0"))
            hi = _key(o.get("hikey", "127"))
            center = _key(o.get("pitch_keycenter", "60"))  # SFZの既定値は60
        z = Zone(path, lo, hi, center,
                 int(o.get("lovel", 0)), int(o.get("hivel", 127)),
                 float(o.get("volume", 0)), float(o.get("tune", 0)),
                 float(o.get("amp_veltrack", 100)), float(o.get("pitch_keytrack", 100)),
                 float(o.get("rt_decay", 0)))
        if "on_locc64" in o:  # ペダル操作の音
            (self.pedal_down if int(o["on_locc64"]) >= 64 else self.pedal_up).append(z)
        elif hi < 0:
            return
        elif o.get("trigger", "attack") == "release":
            self.release.append(z)
        elif o.get("trigger", "attack") == "attack":
            self.zones.append(z)

    def find(self, note: int, vel: int) -> Zone:
        key = (note, vel)
        z = self._cache.get(key)
        if z is None:
            for c in self.zones:
                if c.lokey <= note <= c.hikey and c.lovel <= vel <= c.hivel:
                    z = c
                    break
            if z is None:
                # 鍵域外: 最も近いサンプルで代用
                cand = [c for c in self.zones if c.lovel <= vel <= c.hivel] or self.zones
                z = min(cand, key=lambda c: abs(c.center - note))
            self._cache[key] = z
        return z

    def find_release(self, note: int, vel: int) -> list[Zone]:
        """鍵盤を離したときに鳴らす音(弦の余韻・ダンパー音)。該当するものすべて。"""
        return [z for z in self.release if z.lokey <= note <= z.hikey and z.lovel <= vel <= z.hivel]

    def prepare(self, pitches, progress=None):
        """曲で使う鍵盤のサンプルを先読みして、再生中にディスク待ちが起きないようにする。"""
        pitches = sorted(set(pitches))
        zones = {id(z): z for z in self.zones + self.release
                 if any(z.lokey <= p <= z.hikey for p in pitches)}
        zl = list(zones.values()) + self.pedal_down + self.pedal_up
        for i, z in enumerate(zl):
            d = z.data
            # 4KBおきに触れてOSのファイルキャッシュに載せる
            _ = int(d[:: 1024].sum())
            if progress and (i % 8 == 0 or i == len(zl) - 1):
                progress(i + 1, len(zl))


def find_sfz() -> str | None:
    """sounds/ 以下で最初に見つかった .sfz を返す。"""
    if not os.path.isdir(SOUNDS_DIR):
        return None
    for root, _, files in os.walk(SOUNDS_DIR):
        for f in sorted(files):
            if f.lower().endswith(".sfz"):
                return os.path.join(root, f)
    return None


class SampleVoice:
    """エンジンから使う発音体(合成音の _Voice と同じ属性を持つ)。"""

    __slots__ = ("note", "zone", "data", "pos", "step", "amp", "key_down", "level", "fast_damp", "vel", "length", "t_on")

    def __init__(self, note, zone: Zone, offset, vel, gain=1.0):
        self.note = note
        self.zone = zone
        self.data = zone.data
        self.length = len(self.data)
        self.pos = -float(offset)
        self.step = 2.0 ** ((note - zone.center) * zone.keytrack / 100 / 12 + zone.tune / 1200)
        self.vel = vel
        # SFZのamp_veltrack: 層の中でもベロシティで音量を変える
        db = 40 * np.log10(max(vel, 1) / 127) * zone.veltrack / 100
        self.amp = 10 ** ((db + zone.volume) / 20) / 32768 * gain
        self.key_down = True
        self.level = 1.0
        self.fast_damp = False
        self.t_on = 0

    def read(self, n):
        """現在位置から n サンプル(ステレオ)。位置は進めない。"""
        out = np.zeros((n, 2), dtype=np.float32)
        p = self.pos
        if self.step == 1.0 and p == int(p):
            a, b = int(p), min(self.length, int(p) + n)
            if b > a:
                out[: b - a] = self.data[a:b]
            return out
        lo = int(p)
        if lo >= self.length - 1:
            return out
        # 必要な範囲だけを読み、np.interp で再生速度を変える
        hi = min(self.length, int(p + self.step * n) + 2)
        seg = np.asarray(self.data[lo:hi], dtype=np.float32)
        x = (p - lo) + self.step * np.arange(n, dtype=np.float64)
        xp = np.arange(hi - lo, dtype=np.float64)
        out[:, 0] = np.interp(x, xp, seg[:, 0], right=0.0)
        out[:, 1] = np.interp(x, xp, seg[:, 1], right=0.0)
        return out

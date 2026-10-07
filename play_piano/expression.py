"""表現エンジン: 楽譜 + 個性パラメータ → 演奏イベント。

パラメータは再生中に書き換えてよい。先読み(約0.25秒)より先の音から反映される。
"""
from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import asdict, dataclass

from .engine import AudioEngine
from .score import Score
from .synth import SR


@dataclass
class Params:
    tempo: float = 1.0       # テンポ倍率
    rubato: float = 0.3      # フレーズに沿ったテンポの揺れ(0〜1)
    drift: float = 0.1       # 気まぐれなテンポの揺らぎ(0〜1)
    dyn_level: float = 0.0   # 全体の音量(ベロシティ加算)
    dyn_range: float = 20.0  # フレーズ内の強弱の幅
    melody: float = 8.0      # メロディを浮き立たせる量
    bass: float = 4.0        # バスを響かせる量
    accent: float = 4.0      # 小節頭のアクセント
    surprise: float = 0.0    # 突然のスフォルツァンド/スービト・ピアノ(0〜1)
    lead_ms: float = 0.0     # メロディを先に出す時間(ms)
    roll_ms: float = 0.0     # 和音をばらす時間(ms)
    jitter: float = 4.0      # タイミングと強さの人間的なばらつき(ms)
    legato: float = 0.95     # 音の長さの倍率(<1 ノンレガート, >1 重ねる)
    pedal: float = 0.8       # ペダルの深さ(0 = 使わない)
    final_rit: float = 0.5   # 曲の終わりのリタルダンド(0〜1)
    upstroke: float = 0.0    # 裏拍の和音を高い音から鳴らす割合(ギターのアップストローク、0〜1)


PARAM_INFO = [
    # (名前, ラベル, 最小, 最大)
    ("tempo", "テンポ", 0.5, 1.5),
    ("rubato", "ルバート(フレーズの揺れ)", 0.0, 1.0),
    ("drift", "テンポの気まぐれ", 0.0, 1.0),
    ("dyn_level", "全体の音量", -25.0, 25.0),
    ("dyn_range", "強弱の幅", 0.0, 50.0),
    ("melody", "メロディの強調", 0.0, 25.0),
    ("bass", "バスの強調", -5.0, 20.0),
    ("accent", "拍頭のアクセント", 0.0, 20.0),
    ("surprise", "意外性(突然の強弱)", 0.0, 1.0),
    ("lead_ms", "メロディ先行(ms)", 0.0, 50.0),
    ("roll_ms", "和音のばらし(ms)", 0.0, 80.0),
    ("jitter", "人間らしいばらつき", 0.0, 25.0),
    ("legato", "レガート", 0.4, 1.3),
    ("pedal", "ペダル", 0.0, 1.0),
    ("final_rit", "終わりのリタルダンド", 0.0, 1.0),
    ("upstroke", "アップストローク（裏拍）", 0.0, 1.0),
]

# ギター・ベースで意味が変わるつまみの表示名
PLUCKED_LABELS = {
    "pedal": "響かせる（レットリング）",
    "roll_ms": "ストローク・アルペジオ(ms)",
}

# 演奏スタイルの特徴を誇張した「〜風」のプリセット
PRESETS: dict[str, tuple[str, Params]] = {
    "機械的（表情なし）": (
        "楽譜どおり、一定のテンポと音量。比較用。",
        Params(1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.95, 0, 0),
    ),
    "グールド風（構築的・ノンペダル）": (
        "速めで揺れの少ないテンポ。ペダルを使わず、一音一音を切り離して声部をくっきり。",
        Params(1.18, 0.08, 0.05, -2, 12, 6, 6, 7, 0.0, 0, 0, 3, 0.55, 0.0, 0.15),
    ),
    "ホロヴィッツ風（劇的・鮮烈）": (
        "極端な強弱の幅と、雷のような低音。突然のアクセントで聴き手を驚かせる。",
        Params(1.05, 0.55, 0.3, 2, 45, 12, 16, 8, 0.7, 8, 0, 6, 0.9, 0.55, 0.6),
    ),
    "ルービンシュタイン風（気品ある歌）": (
        "自然で品のあるルバート。メロディを美しく歌わせ、内声は控えめに。",
        Params(0.95, 0.5, 0.12, 0, 24, 15, 5, 3, 0.05, 18, 0, 4, 1.05, 0.8, 0.6),
    ),
    "アルゲリッチ風（情熱・推進力）": (
        "前へ前へと進む速いテンポと気まぐれな揺れ。鋭いアクセントと大きな起伏。",
        Params(1.25, 0.4, 0.55, 4, 34, 10, 10, 10, 0.4, 6, 0, 7, 0.85, 0.5, 0.35),
    ),
    "コルトー風（ロマン派の詩情）": (
        "大きなルバート、メロディを先に出し、和音をハープのようにばらす19世紀的な歌い方。",
        Params(0.88, 0.9, 0.25, -2, 28, 14, 6, 2, 0.1, 35, 45, 8, 1.15, 0.9, 0.9),
    ),
    # ---- 恩田陸『蜜蜂と遠雷』の登場人物(作中の描写からの解釈) ----
    "風間塵（蜜蜂と遠雷）": (
        "型にとらわれない自由奔放な音。気まぐれに揺れるテンポと予測できない強弱で、"
        "ピアノを野山へ連れ出すように鳴らす。",
        #      tempo rub  drift lvl rng mel bas acc sur  lead roll jit leg  ped  rit
        Params(1.06, 0.6, 0.65, 2, 40, 10, 8, 5, 0.6, 8, 10, 10, 0.92, 0.6, 0.35),
    ),
    "栄伝亜夜（蜜蜂と遠雷）": (
        "かつての天才少女が取り戻した、深く大きな音楽。ゆったりとした呼吸で"
        "メロディを歌い、豊かなペダルで響きを広げる。",
        Params(0.96, 0.6, 0.15, 1, 36, 14, 9, 3, 0.15, 15, 5, 4, 1.08, 0.85, 0.75),
    ),
    "マサル（蜜蜂と遠雷）": (
        "ジュリアードの王子。完璧な技術と構成力による、気品ある王道の演奏。"
        "テンポは安定し、輪郭のはっきりした輝かしい音。",
        Params(1.08, 0.3, 0.06, 3, 32, 11, 10, 7, 0.1, 6, 0, 2, 0.95, 0.7, 0.5),
    ),
    "高島明石（蜜蜂と遠雷）": (
        "楽器店で働く「生活者の音楽」。派手さより誠実さ。温かい音で、"
        "一つひとつの旋律を丁寧に歌う。",
        Params(0.94, 0.35, 0.1, -2, 22, 12, 4, 3, 0.0, 10, 3, 5, 1.05, 0.75, 0.6),
    ),
    # ---- 二ノ宮知子『のだめカンタービレ』の登場人物(作中の描写からの解釈) ----
    "のだめ（のだめカンタービレ）": (
        "楽譜より耳と感性で弾く天真爛漫な音楽。テンポも強弱も気分しだいで大きく揺れ、"
        "突然の爆発と、はっとするほど美しい歌が同居する。",
        #      tempo rub   drift lvl rng mel bas acc sur  lead roll jit leg  ped  rit
        Params(1.05, 0.75, 0.8, 3, 44, 12, 8, 6, 0.75, 10, 12, 14, 1.0, 0.85, 0.5),
    ),
    "千秋真一（のだめカンタービレ）": (
        "指揮者を目指す完璧主義者。曲全体を見通した構成と、声部の明快なバランス。"
        "拍節感のしっかりした端正で知的な演奏。",
        Params(1.02, 0.25, 0.04, 1, 30, 13, 11, 8, 0.05, 8, 0, 2, 0.96, 0.65, 0.55),
    ),
}


# ---- ギター・ベースの奏者(演奏スタイルの特徴を誇張したもの) ----
GUITAR_PRESETS: dict[str, tuple[str, Params]] = {
    "機械的（表情なし）": (
        "楽譜どおり、一定のテンポと音量。比較用。",
        Params(1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.95, 0, 0, 0),
    ),
    "クラシックギター風（歌うアルペジオ）": (
        "和音を下から一音ずつつま弾き、メロディを歌わせる。弦をよく響かせ、フレーズの終わりでゆったり。",
        #      tempo rub  drift lvl rng mel bas acc sur  lead roll jit leg  ped  rit  up
        Params(0.95, 0.5, 0.12, 0, 24, 14, 6, 3, 0.05, 12, 30, 5, 1.1, 0.85, 0.6, 0.0),
    ),
    "弾き語り風（ストローク）": (
        "和音をジャカジャカとかき鳴らす。表拍は下から、裏拍は上から。テンポは一定で拍頭が強い。",
        Params(1.0, 0.1, 0.06, 3, 16, 6, 6, 10, 0.05, 0, 38, 6, 0.9, 0.9, 0.3, 1.0),
    ),
    "フラメンコ風（情熱・ラスゲアード）": (
        "速く鋭いかき鳴らしと強烈なアクセント。突然の強弱で畳みかける。",
        Params(1.15, 0.4, 0.4, 5, 40, 10, 8, 14, 0.5, 4, 18, 8, 0.85, 0.6, 0.4, 0.7),
    ),
    "ジャズ・ギター風（柔らかく端正）": (
        "控えめな音量で、和音はさっと揃えて柔らかく。音を伸ばしすぎず、落ち着いた語り口。",
        Params(0.92, 0.3, 0.1, -4, 16, 10, 5, 2, 0.0, 6, 12, 4, 1.0, 0.4, 0.5, 0.0),
    ),
    "ロック・ギター風（歪みでかき鳴らす）": (
        "一定のテンポで拍頭を強く、短めに切って刻む。ディストーションのパワーコードに合う。",
        #      tempo rub   drift lvl rng mel bas acc sur  lead roll jit leg  ped  rit  up
        Params(1.04, 0.05, 0.03, 4, 14, 8, 8, 12, 0.1, 0, 10, 3, 0.85, 0.2, 0.25, 0.5),
    ),
    "ブルース風（泣きのリード）": (
        "メロディを強く歌わせ、たっぷり伸ばす。フレーズの終わりでぐっと溜める。",
        Params(0.92, 0.55, 0.15, 2, 28, 16, 5, 4, 0.1, 15, 15, 6, 1.12, 0.7, 0.7, 0.2),
    ),
    "ボサノヴァ風（ささやくように）": (
        "小さな音で淡々と。和音は短く切り、ベースを親指で軽く弾く。",
        Params(0.9, 0.2, 0.05, -6, 12, 8, 8, 4, 0.0, 10, 8, 3, 0.8, 0.5, 0.4, 0.0),
    ),
}

BASS_PRESETS: dict[str, tuple[str, Params]] = {
    "機械的（表情なし）": (
        "楽譜どおり、一定のテンポと音量。比較用。",
        Params(1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.95, 0, 0, 0),
    ),
    "フィンガー風（落ち着いた土台）": (
        "指弾きの丸い音で、安定したテンポと音量。曲を下から支える。",
        #      tempo rub  drift lvl rng mel bas acc sur  lead roll jit leg  ped  rit  up
        Params(1.0, 0.2, 0.05, 0, 16, 6, 4, 5, 0.0, 0, 0, 4, 0.95, 0.5, 0.5, 0.0),
    ),
    "ロック風（タイトに刻む）": (
        "前のめりのテンポで、音を短めに切ってタイトに。拍頭を強く。",
        Params(1.08, 0.05, 0.03, 4, 14, 4, 6, 10, 0.05, 0, 0, 3, 0.8, 0.0, 0.2, 0.0),
    ),
    "ファンク風（跳ねるスタッカート）": (
        "短く切った音と鋭いアクセント。ときどき不意に強く弾いてグルーヴを出す。",
        Params(1.05, 0.1, 0.1, 3, 30, 6, 8, 14, 0.4, 0, 0, 6, 0.5, 0.0, 0.2, 0.0),
    ),
    "バラード風（たっぷり伸ばす）": (
        "ゆったりしたテンポで、一音一音を長く響かせる。",
        Params(0.9, 0.5, 0.1, -3, 22, 10, 6, 2, 0.0, 0, 0, 4, 1.1, 0.9, 0.7, 0.0),
    ),
}


def presets_for(instrument: str) -> dict[str, tuple[str, Params]]:
    """楽器(piano / nylon / steel / electric / drive / bass / band_*)に合う奏者のプリセット。"""
    if instrument == "bass":
        return BASS_PRESETS
    if instrument in ("nylon", "steel", "electric", "drive") or instrument.startswith("band_"):
        return GUITAR_PRESETS
    return PRESETS


def default_preset(instrument: str) -> str:
    """楽器を選んだときに最初に選ぶ奏者。"""
    guitar = instrument.removeprefix("band_")
    if guitar == "drive":
        return "ロック・ギター風（歪みでかき鳴らす）"
    if guitar == "electric":
        return "ジャズ・ギター風（柔らかく端正）"
    if instrument == "piano":
        return "ルービンシュタイン風（気品ある歌）"
    return list(presets_for(instrument))[1]  # 「機械的」の次


def _smoothstep(a, b, x):
    x = min(1.0, max(0.0, (x - a) / (b - a)))
    return x * x * (3 - 2 * x)


class Performer:
    LOOKAHEAD = 0.25  # 秒

    def __init__(self, engine: AudioEngine, score: Score, params: Params, seed: int | None = None):
        self.engine = engine
        self.score = score
        self.params = params
        self.rng = random.Random(seed)
        self._drift_phase = (self.rng.uniform(0, 6.3), self.rng.uniform(0, 6.3))
        self._mel_mean = score.melody_mean()
        self._surprise_bar = {}
        self._thread = None
        self._stop = threading.Event()
        self.position = 0.0  # 現在の拍位置(表示用)
        self.finished = False
        self._sched: list[tuple[int, float]] = []  # (サンプル時刻, 拍)

        groups: dict[float, list] = {}
        for n in score.notes:
            groups.setdefault(round(n.start, 4), []).append(n)
        self._timeline = sorted(
            [(b, 0, "pedal", kind) for b, kind in score.pedal_points]
            + [(b, 1, "notes", g) for b, g in groups.items()],
            key=lambda x: (x[0], x[1]),
        )
        self._phr = sorted(score.phrases, key=lambda ph: ph.start)

    # ---- 表現の計算 ----
    def _phrase_at(self, beat):
        for ph in self._phr:
            if ph.start <= beat < ph.end:
                return ph, (beat - ph.start) / (ph.end - ph.start)
        ph = self._phr[-1]
        return ph, 0.999

    def bpm_at(self, beat: float) -> float:
        P = self.params
        _, x = self._phrase_at(beat)
        f = 1.0 + P.rubato * (0.10 * math.sin(math.pi * x) - 0.38 * _smoothstep(0.72, 1.0, x))
        p1, p2 = self._drift_phase
        f += P.drift * 0.09 * (math.sin(2 * math.pi * beat / 11.0 + p1) + math.sin(2 * math.pi * beat / 6.7 + p2)) / 2
        g = beat / self.score.length
        f -= P.final_rit * 0.55 * _smoothstep(0.92, 1.0, g)
        return self.score.tempo_at(beat) * P.tempo * max(0.3, f)

    def _velocity(self, note, beat):
        P = self.params
        ph, x = self._phrase_at(beat)
        arch = math.sin(math.pi * min(1.0, x) ** 1.36)
        v = 58 + P.dyn_level + ph.level * (0.4 + P.dyn_range / 40) + P.dyn_range * (arch - 0.4)
        v += (note.vel_hint - 64) * 0.6
        if note.role == "melody":
            v += P.melody + P.dyn_range * 0.12 * (note.pitch - self._mel_mean) / 12
        elif note.role == "bass":
            v += P.bass
        else:
            v -= P.melody * 0.35
        bp = self.score.bar_pos(beat)
        if abs(bp) < 1e-3:
            v += P.accent
        elif abs(bp - self.score.bar_len(beat) / 2) < 1e-3 and self.score.bar_len(beat) >= 2:
            v += P.accent * 0.4
        bar = self.score.bar_index(beat)
        v += self._surprise_bar.get(bar, 0.0) * (1.0 if abs(bp) < 1e-3 or self._surprise_bar.get(bar, 0) < 0 else 0.3)
        v += self.rng.gauss(0, P.jitter * 0.25)
        return int(max(6, min(127, round(v))))

    def _roll_surprise(self, beat):
        P = self.params
        bar = self.score.bar_index(beat)
        if bar not in self._surprise_bar:
            r = self.rng.random()
            if r < P.surprise * 0.10:
                self._surprise_bar[bar] = 22.0   # スフォルツァンド
            elif r < P.surprise * 0.16:
                self._surprise_bar[bar] = -18.0  # スービト・ピアノ
            else:
                self._surprise_bar[bar] = 0.0

    # ---- スケジューリング ----
    def _schedule_point(self, item, t_sec, start_sample):
        beat, _, kind, data = item
        P = self.params
        eng = self.engine
        at = lambda s: start_sample + int(max(0.0, s) * SR)
        if kind == "pedal":
            if P.pedal <= 0.01:
                return
            eng.schedule(at(t_sec - 0.005), "pedal", False)
            if data == "change":
                eng.schedule(at(t_sec - 0.004), "pedal_depth", P.pedal)
                eng.schedule(at(t_sec + 0.07), "pedal", True)
            return

        self._roll_surprise(beat)
        sec_per_beat = 60.0 / self.bpm_at(beat)
        notes = sorted(data, key=lambda n: n.pitch)
        roll = P.roll_ms / 1000 if len(notes) >= 3 else 0.0
        # アップストローク: 裏拍の和音は高い弦から鳴らす
        if roll and P.upstroke > 0 and abs(beat - round(beat)) > 1e-3 and self.rng.random() < P.upstroke:
            notes.reverse()
        for i, n in enumerate(notes):
            off = self.rng.gauss(0, P.jitter / 1000)
            if roll:
                off += roll * i / (len(notes) - 1)
            if n.role != "melody":
                off += P.lead_ms / 1000
            vel = self._velocity(n, beat)
            dur = max(0.04, n.dur * sec_per_beat * P.legato)
            eng.schedule(at(t_sec + off), "on", n.pitch, vel, n.inst)
            eng.schedule(at(t_sec + off + dur), "off", n.pitch, n.inst)

    def _iter_times(self):
        """(timelineの要素, 演奏開始からの秒) を順に返す。テンポは都度計算する。"""
        t = 0.0
        prev = 0.0
        for item in self._timeline:
            beat = item[0]
            # 拍間を細かく区切ってテンポを積分する
            while prev < beat - 1e-9:
                step = min(0.25, beat - prev)
                t += step * 60.0 / self.bpm_at(prev)
                prev += step
            yield item, t

    def render_offline(self, tail: float = 3.0):
        """WAV書き出し用: 全イベントを予約して波形を返す。"""
        import numpy as np

        self.engine.prepare_notes(self.score.notes)
        start = self.engine.clock + int(0.05 * SR)
        t = 0.0
        for item, t in self._iter_times():
            self._schedule_point(item, t, start)
        total = int((t + tail + 2.0) * SR)
        out = [self.engine.render(1024) for _ in range(total // 1024 + 1)]
        return np.concatenate(out)

    # ---- リアルタイム再生 ----
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        eng = self.engine
        start = eng.clock + int(0.15 * SR)
        last_t = 0.0
        for item, t in self._iter_times():
            if self._stop.is_set():
                return
            while (start + t * SR) - eng.clock > self.LOOKAHEAD * SR:
                if self._stop.is_set():
                    return
                self.position = self._beat_now()
                time.sleep(0.01)
            self._schedule_point(item, t, start)
            self._sched.append((start + int(t * SR), item[0]))
            last_t = t
        end = start + int((last_t + 2.5) * SR)
        while eng.clock < end and not self._stop.is_set():
            self.position = self._beat_now()
            time.sleep(0.02)
        self.finished = True

    def _beat_now(self):
        # 予約済みの (サンプル, 拍) から現在の拍を求める
        clk = self.engine.clock
        beat = 0.0
        for s, b in reversed(self._sched[-64:]):
            if s <= clk:
                beat = b
                break
        return beat

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self.engine.clear_scheduled()


def params_dict(p: Params) -> dict:
    return asdict(p)

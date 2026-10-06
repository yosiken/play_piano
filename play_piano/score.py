"""楽譜データ。拍(4分音符=1.0)単位で音符を持つ。"""
from __future__ import annotations

import bisect
import os
from dataclasses import dataclass, field

NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def p(name: str) -> int:
    """'C#4' や 'Bb3' をMIDIノート番号にする。"""
    base = NOTE_NAMES[name[0]]
    i = 1
    while i < len(name) and name[i] in "#b":
        base += 1 if name[i] == "#" else -1
        i += 1
    return base + 12 * (int(name[i:]) + 1)


@dataclass
class Note:
    start: float
    dur: float
    pitch: int
    role: str = "inner"  # melody / bass / inner
    vel_hint: int = 64
    track: int = 0


@dataclass
class Phrase:
    start: float
    end: float
    level: float = 0.0  # 曲全体の起伏(-10〜+10)


@dataclass
class Score:
    title: str
    bpm: float
    beats_per_bar: float
    notes: list[Note]
    phrases: list[Phrase]
    # (拍, "change" | "up") ペダルを踏み替える/離す位置
    pedal_points: list[tuple[float, str]] = field(default_factory=list)
    pickup: float = 0.0  # 弱起の長さ(拍)
    # 途中でテンポ・拍子が変わる曲用: [(拍, bpm)] / 小節頭の拍のリスト
    tempo_map: list[tuple[float, float]] = field(default_factory=list)
    bar_starts: list[float] = field(default_factory=list)

    def __post_init__(self):
        self.length = max(n.start + n.dur for n in self.notes)
        self._tm_beats = [b for b, _ in self.tempo_map]

    def tempo_at(self, beat: float) -> float:
        """楽譜に書かれたテンポ(bpm)。"""
        if not self.tempo_map:
            return self.bpm
        i = bisect.bisect_right(self._tm_beats, beat) - 1
        return self.tempo_map[max(0, i)][1]

    def bar_index(self, beat: float) -> int:
        if self.bar_starts:
            return bisect.bisect_right(self.bar_starts, beat + 1e-6) - 1
        return int((beat - self.pickup) // self.beats_per_bar)

    def bar_pos(self, beat: float) -> float:
        """小節内の位置(拍)。"""
        if self.bar_starts:
            return beat - self.bar_starts[max(0, self.bar_index(beat))]
        return (beat - self.pickup) % self.beats_per_bar

    def bar_len(self, beat: float) -> float:
        if self.bar_starts:
            i = max(0, self.bar_index(beat))
            if i + 1 < len(self.bar_starts):
                return self.bar_starts[i + 1] - self.bar_starts[i]
        return self.beats_per_bar

    def melody_mean(self) -> float:
        mel = [n.pitch for n in self.notes if n.role == "melody"] or [n.pitch for n in self.notes]
        return sum(mel) / len(mel)


# ---------------------------------------------------------------------------
# J.S.バッハ 平均律クラヴィーア曲集 第1巻 第1番 前奏曲 ハ長調 BWV 846
# ---------------------------------------------------------------------------
_BWV846 = [
    "C4 E4 G4 C5 E5", "C4 D4 A4 D5 F5", "B3 D4 G4 D5 F5", "C4 E4 G4 C5 E5",
    "C4 E4 A4 E5 A5", "C4 D4 F#4 A4 D5", "B3 D4 G4 D5 G5", "B3 C4 E4 G4 C5",
    "A3 C4 E4 G4 C5", "D3 A3 D4 F#4 C5", "G3 B3 D4 G4 B4", "G3 Bb3 E4 G4 C#5",
    "F3 A3 D4 A4 D5", "F3 Ab3 D4 F4 B4", "E3 G3 C4 G4 C5", "E3 F3 A3 C4 F4",
    "D3 F3 A3 C4 F4", "G2 D3 G3 B3 F4", "C3 E3 G3 C4 E4", "C3 G3 Bb3 C4 E4",
    "F2 F3 A3 C4 E4", "F#2 C3 A3 C4 Eb4", "Ab2 F3 B3 C4 D4", "G2 F3 G3 B3 D4",
    "G2 E3 G3 C4 E4", "G2 D3 G3 C4 F4", "G2 D3 G3 B3 F4", "G2 Eb3 A3 C4 F#4",
    "G2 E3 G3 C4 G4", "G2 D3 G3 C4 F4", "G2 D3 G3 B3 F4", "C2 C3 G3 Bb3 E4",
]


def bach_prelude() -> Score:
    notes: list[Note] = []
    for bar, chord in enumerate(_BWV846):
        a, b, c, d, e = (p(x) for x in chord.split())
        for half in (0, 2):
            t = bar * 4 + half
            notes.append(Note(t, 2.0, a, "bass"))
            notes.append(Note(t + 0.25, 1.75, b, "inner"))
            for i, (x, role) in enumerate(((c, "inner"), (d, "inner"), (e, "melody")) * 2):
                notes.append(Note(t + 0.5 + i * 0.25, 0.25, x, role))
    t = 32 * 4
    for bar_notes, second in (
        ("F3 A3 C4 F4 C4 A3 C4 A3 F3 A3 F3 D3 F3 D3", "C3"),
        ("G4 B4 D5 F5 D5 B4 D5 B4 G4 B4 D4 F4 E4 D4", "B2"),
    ):
        notes.append(Note(t, 4.0, p("C2"), "bass"))
        notes.append(Note(t + 0.25, 3.75, p(second), "bass"))
        seq = [p(x) for x in bar_notes.split()]
        top = max(seq)
        for i, x in enumerate(seq):
            notes.append(Note(t + 0.5 + i * 0.25, 0.25, x, "melody" if x == top else "inner"))
        t += 4
    for i, x in enumerate(("C2", "C3", "E4", "G4", "C5")):
        notes.append(Note(t, 4.0, p(x), "bass" if i < 2 else ("melody" if x == "C5" else "inner")))

    levels = [-6, -3, 0, 2, -2, 3, 6, 4]
    phrases = [Phrase(i * 16, i * 16 + 16, lv) for i, lv in enumerate(levels)]
    phrases.append(Phrase(128, 140, -2))
    pedal = [(bar * 4.0, "change") for bar in range(35)]
    return Score("バッハ：平均律 第1番 前奏曲 BWV846", 66, 4, notes, phrases, pedal)


# ---------------------------------------------------------------------------
# L.v.ベートーヴェン エリーゼのために WoO 59 (Aセクション)
# 3/8拍子。16分音符 = 0.25拍
# ---------------------------------------------------------------------------
def fur_elise() -> Score:
    notes: list[Note] = []
    S = 0.25
    bar_len = 1.5

    def rh(t, names, role="melody", dur=S):
        for i, x in enumerate(names.split()):
            if x != "-":
                notes.append(Note(t + i * S, dur, p(x), role))

    def lh(t, names):
        for i, x in enumerate(names.split()):
            notes.append(Note(t + i * S, (3 - i) * S + S * 3, p(x), "bass" if i == 0 else "inner"))

    def section(t0, first_time):
        # t0 = 1小節目の頭
        b = [t0 + i * bar_len for i in range(8)]
        rh(b[0], "E5 D#5 E5 B4 D5 C5")
        rh(b[1], "A4", dur=2 * S); lh(b[1], "A2 E3 A3"); rh(b[1] + 3 * S, "C4 E4 A4", "inner")
        rh(b[2], "B4", dur=2 * S); lh(b[2], "E2 E3 G#3"); rh(b[2] + 3 * S, "E4 G#4 B4", "inner")
        rh(b[3], "C5", dur=2 * S); lh(b[3], "A2 E3 A3"); rh(b[3] + 3 * S, "E4", "inner"); rh(b[3] + 4 * S, "E5 D#5")
        rh(b[4], "E5 D#5 E5 B4 D5 C5")
        rh(b[5], "A4", dur=2 * S); lh(b[5], "A2 E3 A3"); rh(b[5] + 3 * S, "C4 E4 A4", "inner")
        rh(b[6], "B4", dur=2 * S); lh(b[6], "E2 E3 G#3"); rh(b[6] + 3 * S, "E4", "inner"); rh(b[6] + 4 * S, "C5 B4")
        if first_time:
            rh(b[7], "A4", dur=2 * S); lh(b[7], "A2 E3 A3"); rh(b[7] + 4 * S, "E5 D#5")
        else:
            rh(b[7], "A4", dur=6 * S)
            for i, x in enumerate(("A2", "E3", "A3", "C4", "E4")):
                notes.append(Note(b[7] + i * S, 6 * S - i * S + 1.0, p(x), "bass" if i == 0 else "inner"))
        return t0 + 8 * bar_len

    rh(0, "E5 D#5")  # 弱起
    pickup = 2 * S
    t = section(pickup, True)
    end = section(t, False)

    phrases = [
        Phrase(0, pickup + 4 * bar_len, -3),
        Phrase(pickup + 4 * bar_len, t, -1),
        Phrase(t, t + 4 * bar_len, -2),
        Phrase(t + 4 * bar_len, end + 1.0, -4),
    ]
    pedal = []
    for s0 in (pickup, t):
        for i in range(8):
            bt = s0 + i * bar_len
            # 右手だけの小節(1,5小節目)ではペダルを上げる
            pedal.append((bt, "up" if i in (0, 4) else "change"))
    return Score("ベートーヴェン：エリーゼのために（冒頭の主題）", 72, 1.5, notes, phrases, pedal, pickup)


# ---------------------------------------------------------------------------
# ロシア民謡 コロブチカ(行商人) — テトリスのタイプAとして知られる旋律
# 構成: A(主題) → A'(オクターブで厚く) → B(中間部) → A''(終止)
# ---------------------------------------------------------------------------
_KORO_A = [  # (音, 長さ[拍]) "r" は休符
    ("E5", 1), ("B4", .5), ("C5", .5), ("D5", 1), ("C5", .5), ("B4", .5),
    ("A4", 1), ("A4", .5), ("C5", .5), ("E5", 1), ("D5", .5), ("C5", .5),
    ("B4", 1.5), ("C5", .5), ("D5", 1), ("E5", 1),
    ("C5", 1), ("A4", 1), ("A4", 1), ("r", 1),
    ("D5", 1.5), ("F5", .5), ("A5", 1), ("G5", .5), ("F5", .5),
    ("E5", 1.5), ("C5", .5), ("E5", 1), ("D5", .5), ("C5", .5),
    ("B4", 1), ("B4", .5), ("C5", .5), ("D5", 1), ("E5", 1),
    ("C5", 1), ("A4", 1), ("A4", 1), ("r", 1),
]
# 左手: 小節ごとに (根音, 4拍ぶんの8分音符の並び)
_KORO_A_BASS = [
    "E2 E3 E2 E3 E2 E3 E2 E3", "A2 A3 A2 A3 A2 A3 A2 A3",
    "G#2 G#3 G#2 G#3 E2 E3 E2 E3", "A2 A3 A2 A3 A2 A3 B2 C3",
    "D2 D3 D2 D3 D2 D3 D2 D3", "C2 C3 C2 C3 C2 C3 C2 C3",
    "B1 B2 B1 B2 E2 E3 E2 E3", "A2 A3 A2 A3 A2 A3 A2 r",
]
_KORO_B = [
    ("E5", 2), ("C5", 2), ("D5", 2), ("B4", 2), ("C5", 2), ("A4", 2), ("G#4", 2), ("B4", 2),
    ("E5", 2), ("C5", 2), ("D5", 2), ("B4", 2), ("C5", 1), ("E5", 1), ("A5", 2), ("G#5", 4),
]
_KORO_B_BASS = [
    "A2 E3 A3 E3 A2 E3 A3 E3", "E2 B2 E3 B2 E2 B2 E3 B2",
    "A2 E3 A3 E3 A2 E3 A3 E3", "E2 B2 E3 B2 E2 B2 E3 B2",
    "A2 E3 A3 E3 A2 E3 A3 E3", "E2 B2 E3 B2 E2 B2 E3 B2",
    "A2 E3 A3 E3 A2 E3 A3 E3", "E2 B2 E3 G#3 B3 G#3 E3 B2",
]


def korobeiniki() -> Score:
    notes: list[Note] = []

    def melody(t, seq, octave_double=False):
        for name, d in seq:
            if name != "r":
                notes.append(Note(t, d, p(name), "melody"))
                if octave_double:
                    notes.append(Note(t, d, p(name) - 12, "inner"))
            t += d
        return t

    def bass(t, bars, staccato=0.42):
        for bar in bars:
            for i, name in enumerate(bar.split()):
                if name != "r":
                    notes.append(Note(t + i * 0.5, staccato, p(name), "bass" if i % 2 == 0 else "inner"))
            t += 4
        return t

    phrases = []
    pedal = []
    t = 0.0
    for section, (mel, bas, dbl, levels) in enumerate((
        (_KORO_A, _KORO_A_BASS, False, (-3, -1)),
        (_KORO_A, _KORO_A_BASS, True, (1, 3)),
        (_KORO_B, _KORO_B_BASS, False, (-4, 2)),
        (_KORO_A, _KORO_A_BASS, True, (4, 2)),
    )):
        start = t
        melody(start, mel, dbl)
        t = bass(start, bas)
        phrases += [Phrase(start, start + 16, levels[0]), Phrase(start + 16, start + 32, levels[1])]
        # 中間部(長い音)はペダルを使い、主題は軽く(半小節ごとに踏み替え)
        step = 4.0 if section == 2 else 2.0
        pedal += [(start + i * step, "change") for i in range(int(32 / step))]

    # 終止: イ短調の和音
    for name in ("A1", "A2", "E3", "A3", "C4", "E4", "A4"):
        notes.append(Note(t, 4.0, p(name), "bass" if name == "A1" else "inner"))
    notes.append(Note(t, 4.0, p("A5"), "melody"))
    phrases.append(Phrase(t, t + 4, 4))
    pedal.append((t, "change"))
    return Score("ロシア民謡：コロブチカ（テトリス タイプA）", 144, 4, notes, phrases, pedal)


BUILTIN = {
    "bach": bach_prelude,
    "elise": fur_elise,
    "korobeiniki": korobeiniki,
}


# ---------------------------------------------------------------------------
# MIDIファイル読み込み
# ---------------------------------------------------------------------------
PIECES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pieces")

CATALOG_PATH = os.path.join(PIECES_DIR, "catalog.toml")
CATALOG_ERROR = ""  # 読み込みに失敗したときの理由(GUIで表示する)


def _load_catalog():
    """pieces/catalog.toml から曲名・組曲・テンポ補正を読む。

    戻り値: (KNOWN_PIECES, PIECE_SETS, TEMPO_OVERRIDE)
      KNOWN_PIECES  : ファイル名(拡張子なし) → (作曲者, 表示名)
      PIECE_SETS    : キー → (作曲者, 表示名, [ファイル名(拡張子なし)], 曲間の休み[拍])
      TEMPO_OVERRIDE: ファイル名(拡張子なし) → テンポ(♩/分)
    """
    global CATALOG_ERROR
    import tomllib

    try:
        with open(CATALOG_PATH, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}, {}, {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        CATALOG_ERROR = f"{os.path.basename(CATALOG_PATH)} を読み込めませんでした: {e}"
        return {}, {}, {}

    known, sets, tempo = {}, {}, {}
    problems = []
    for key, v in data.get("pieces", {}).items():
        if isinstance(v, dict) and "title" in v:
            known[key] = (str(v.get("composer", "")), str(v["title"]))
        else:
            problems.append(f"[pieces] {key}")
    for key, v in data.get("sets", {}).items():
        if isinstance(v, dict) and "title" in v and v.get("files"):
            sets[key] = (str(v.get("composer", "")), str(v["title"]),
                         [str(x) for x in v["files"]], float(v.get("gap", 2.0)))
        else:
            problems.append(f"[sets.{key}]")
    for key, v in data.get("tempo", {}).items():
        if isinstance(v, (int, float)) and v > 0:
            tempo[key] = float(v)
        else:
            problems.append(f"[tempo] {key}")
    if problems:
        CATALOG_ERROR = "catalog.toml の次の項目を読み飛ばしました（title や files が無い、または値が不正）: " + \
            "、".join(problems)
    return known, sets, tempo


KNOWN_PIECES, PIECE_SETS, TEMPO_OVERRIDE = _load_catalog()


def _display_name(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    comp, title = KNOWN_PIECES.get(stem, ("", stem))
    return f"{comp}：{title}" if comp else title


def list_piece_files() -> list[tuple[str, str | list[str]]]:
    """pieces/ の曲を (表示名, パス または パスのリスト) で返す。"""
    if not os.path.isdir(PIECES_DIR):
        return []
    res: list = []
    in_sets = set()
    for comp, title, stems, _ in PIECE_SETS.values():
        paths = [os.path.join(PIECES_DIR, s + ".mid") for s in stems]
        in_sets.update(stems)
        if all(os.path.exists(p) for p in paths):
            res.append((f"{comp}：{title}", paths))
    known, unknown = [], []
    for f in os.listdir(PIECES_DIR):
        s = os.path.splitext(f)[0]
        if f.lower().endswith((".mid", ".midi")) and s not in in_sets:
            (known if s in KNOWN_PIECES else unknown).append(f)
    order = list(KNOWN_PIECES)
    known.sort(key=lambda f: order.index(os.path.splitext(f)[0]))
    singles = lambda fs: [(_display_name(f), os.path.join(PIECES_DIR, f)) for f in fs]
    # 既知の単曲 → 組曲・前奏曲とフーガ → その他のMIDI
    return singles(known) + res + singles(sorted(unknown))


def find_piece(name: str):
    """曲のキー(PIECE_SETS)・ファイル名・パスから読み込み対象を返す。"""
    if name in PIECE_SETS:
        return [os.path.join(PIECES_DIR, s + ".mid") for s in PIECE_SETS[name][2]]
    if os.path.exists(name):
        return name
    p = os.path.join(PIECES_DIR, name if name.endswith(".mid") else name + ".mid")
    return p


def concat_scores(scores: list[Score], title: str, gap: float) -> Score:
    """楽章をつなげて1曲にする。gap は曲間の休み(拍)。"""
    notes, phrases, pedal, tempo_map, bars = [], [], [], [], []
    off = 0.0
    for sc in scores:
        for n in sc.notes:
            notes.append(Note(n.start + off, n.dur, n.pitch, n.role, n.vel_hint, n.track))
        phrases += [Phrase(ph.start + off, ph.end + off, ph.level) for ph in sc.phrases]
        pedal += [(b + off, k) for b, k in sc.pedal_points]
        if off > 0:
            pedal.append((off - gap, "up"))  # 曲間ではペダルを上げる
        tm = sc.tempo_map or [(0.0, sc.bpm)]
        tempo_map += [(b + off, bpm) for b, bpm in tm]
        bars += [b + off for b in (sc.bar_starts or [0.0])]
        off += sc.length + gap
    first = scores[0]
    return Score(title, first.bpm, first.beats_per_bar, notes, phrases, sorted(pedal),
                 tempo_map=tempo_map, bar_starts=bars)
def load_piece(arg) -> Score:
    """パス、またはパスのリスト(続けて演奏する楽章)から Score を作る。"""
    if isinstance(arg, (list, tuple)):
        for comp, title, stems, gap in PIECE_SETS.values():
            if [os.path.join(PIECES_DIR, x + ".mid") for x in stems] == list(arg):
                break
        else:
            comp, title, gap = "", os.path.basename(arg[0]), 2.0
        scores = [load_midi(p) for p in arg]
        if len(scores) == 1:
            scores[0].title = f"{comp}：{title}"
            return scores[0]
        return concat_scores(scores, f"{comp}：{title}" if comp else title, gap)
    return load_midi(arg)


def load_midi(path: str) -> Score:
    """MIDIを読み込む。テンポ・拍子の変化、トラック(右手/左手)、
    音量(CC7)とベロシティによる強弱、ペダル(CC64)を楽譜情報として使う。"""
    import mido

    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat
    tempos: list[tuple[int, float]] = []
    sigs: list[tuple[int, int, int]] = []
    raw = []       # (start, end, pitch, 強さ, トラック番号)
    pedal_ev = []  # (tick, down)
    for ti, track in enumerate(mid.tracks):
        tick = 0
        vol: dict[int, int] = {}
        active: dict[tuple[int, int], tuple[int, float]] = {}
        for msg in track:
            tick += msg.time
            if msg.type == "set_tempo":
                tempos.append((tick, mido.tempo2bpm(msg.tempo)))
            elif msg.type == "time_signature":
                sigs.append((tick, msg.numerator, msg.denominator))
            elif msg.type == "control_change" and msg.control == 7:
                vol[msg.channel] = msg.value
            elif msg.type == "control_change" and msg.control == 64:
                pedal_ev.append((tick, msg.value >= 64))
            elif msg.type in ("note_on", "note_off") and msg.channel != 9:
                key = (msg.channel, msg.note)
                if key in active:
                    s0, loud = active.pop(key)
                    raw.append((s0, tick, msg.note, loud, ti))
                if msg.type == "note_on" and msg.velocity > 0:
                    active[key] = (tick, msg.velocity * vol.get(msg.channel, 100) / 100)
    if not raw:
        raise ValueError("ノートが見つかりませんでした")

    first = min(r[0] for r in raw)

    def beat(tk):
        return max(0.0, (tk - first) / tpb)

    # 強弱: 曲中の中央値を64として相対化する
    louds = sorted(r[3] for r in raw)
    med = louds[len(louds) // 2]
    spread = louds[int(len(louds) * 0.9)] - louds[int(len(louds) * 0.1)]
    notes = []
    for s0, e, n, loud, ti in raw:
        hint = 64 + (loud - med) / spread * 30 if spread > 1 else 64
        notes.append(Note(beat(s0), max(0.05, (e - s0) / tpb), n, "inner", int(max(20, min(110, hint))), ti))
    notes.sort(key=lambda n: (n.start, n.pitch))

    # 役割: 上のトラック(右手)の最高音 = メロディ、下のトラック(左手)の最低音 = バス
    tracks = sorted({n.track for n in notes})
    # 右手 = 音数が全体の5%以上あるトラックのうち平均音高が最も高いもの
    # (トラックの順番は楽譜によって違い、高音の小さな追加パートもある)
    count = {t: sum(1 for n in notes if n.track == t) for t in tracks}
    mean_pitch = {t: sum(n.pitch for n in notes if n.track == t) / count[t] for t in tracks}
    major = [t for t in tracks if count[t] >= 0.05 * len(notes)] or tracks
    upper = max(major, key=lambda t: mean_pitch[t])
    rh_tracks = {upper} | {t for t in tracks if t not in major and mean_pitch[t] >= 60}
    two_hands = len(tracks) >= 2
    groups: dict[float, list[Note]] = {}
    for n in notes:
        groups.setdefault(round(n.start, 3), []).append(n)
    sounding: list[Note] = []  # 右手で鳴り続けている音(上声の判定用)
    for t in sorted(groups):
        g = groups[t]
        rh = [n for n in g if n.track in rh_tracks] if two_hands else g
        lh = [n for n in g if n.track not in rh_tracks] if two_hands else g
        if rh:
            hi = max(rh, key=lambda n: n.pitch)
            # 上で長い音が鳴り続けているなら、その下の音は伴奏(例: 月光の3連符)
            sounding = [n for n in sounding if n.start + n.dur > t + 1e-6]
            covered = any(n.pitch > hi.pitch for n in sounding)
            if (two_hands or hi.pitch >= 55) and not covered:
                hi.role = "melody"
            sounding += rh
        if lh and not two_hands:
            lo = min(lh, key=lambda n: n.pitch)
            if lo.role != "melody" and lo.pitch < 55:
                lo.role = "bass"
    if two_hands:
        # 左手は拍ごとに最低音だけをバスとする(アルペジオ全体を強調しないため)
        by_beat: dict[int, list[Note]] = {}
        for n in notes:
            if n.track not in rh_tracks:
                by_beat.setdefault(int(n.start + 1e-6), []).append(n)
        for g in by_beat.values():
            lo = min(n.pitch for n in g)
            for n in g:
                if n.pitch == lo and n.role != "melody":
                    n.role = "bass"

    length = max(n.start + n.dur for n in notes)

    # テンポ表
    tempo_map: list[tuple[float, float]] = []
    for tk, bpm in sorted(tempos):
        b = beat(tk)
        if tempo_map and abs(tempo_map[-1][0] - b) < 1e-6:
            tempo_map[-1] = (b, bpm)
        else:
            tempo_map.append((b, bpm))
    if not tempo_map:
        tempo_map = [(0.0, 100.0)]
    # テンポ補正は pieces/ 内の曲だけ(別ドライブのファイルでは relpath が失敗する)
    try:
        rel = os.path.splitext(os.path.relpath(path, PIECES_DIR))[0].replace("\\", "/")
    except ValueError:
        rel = ""
    if rel in TEMPO_OVERRIDE and len(tempo_map) == 1:
        tempo_map = [(0.0, float(TEMPO_OVERRIDE[rel]))]

    # 小節の頭(拍子の変化を反映)
    sigs.sort()
    if not sigs or sigs[0][0] > first:
        sigs.insert(0, (first, 4, 4))
    bar_starts = []
    for i, (tk, num, den) in enumerate(sigs):
        bl = num * 4 / den
        b = beat(tk)
        stop = beat(sigs[i + 1][0]) if i + 1 < len(sigs) else length + bl
        while b < stop - 1e-6:
            bar_starts.append(b)
            b += bl
    bpb = sigs[0][1] * 4 / sigs[0][2]

    # フレーズ: 4小節ずつ
    phrases = []
    for i in range(0, len(bar_starts), 4):
        en = bar_starts[i + 4] if i + 4 < len(bar_starts) else length + 0.01
        phrases.append(Phrase(bar_starts[i], en, 0))

    # ペダル: MIDIに指定があれば使い、無ければ小節頭とバスが変わる拍で踏み替える
    pedal: list[tuple[float, str]] = []
    if sum(1 for _, d in pedal_ev if d) >= 4:
        state = False
        for tk, down in sorted(pedal_ev):
            if down:
                pedal.append((beat(tk), "change"))
            elif state:
                pedal.append((beat(tk), "up"))
            state = down
    else:
        points = set(bar_starts)
        last_bass = None
        last_pt = -1.0
        for b in sorted(groups):
            bass = [n for n in groups[b] if n.role == "bass"]
            if not bass:
                continue
            pc = bass[0].pitch % 12
            on_beat = abs(b - round(b)) < 1e-3
            if last_bass is not None and pc != last_bass and on_beat and b - last_pt >= 1.0:
                points.add(float(b))
            last_bass = pc
            if b in points:
                last_pt = b
        pedal = [(b, "change") for b in sorted(points)]

    return Score(_display_name(path), tempo_map[0][1], bpb, notes, phrases, pedal,
                 tempo_map=tempo_map, bar_starts=bar_starts)

"""ピアノの楽譜をギター・ベースで弾ける形に編曲する。

ギター: 音域(6弦の開放 E2 〜 最高フレット)に収め、同時に鳴る音を弦の数(6本)までにする。
        同じ高さの音を重ねて鳴らすことはできない(同じ弦を弾き直す)。
ベース: 単音楽器なので、バスの声部(または旋律)を1本の線として取り出す。
ギター＋ベース: ベースがバスの声部を、ギターが残りの声部を受け持つ。
ペダルの位置はそのまま残し、ギターでは「響かせる(レットリング)」、ベースでは音を伸ばす目安に使う。
"""
from __future__ import annotations

from .score import Note, Score
from .strings import BANDS, SPECS, fold

GUITAR_STRINGS = 6

# 楽器 → 選べるパート (キー, 表示名)
PARTS = {
    "guitar": [("full", "メロディ＋伴奏"), ("melody", "メロディだけ")],
    "bass": [("bass", "ベースライン"), ("bass8", "ベースライン（8分で刻む）"), ("melody", "メロディ")],
    "band": [("full", "メロディ＋伴奏＋ベース"), ("full8", "メロディ＋伴奏＋ベース（8分で刻む）"),
             ("backing", "伴奏＋ベース（メロディなし）")],
}


def part_family(instrument: str) -> str:
    """楽器のキー → PARTS のキー"""
    if instrument in BANDS:
        return "band"
    return "bass" if instrument == "bass" else "guitar"


def instrument_name(instrument: str) -> str:
    return BANDS[instrument][1] if instrument in BANDS else SPECS[instrument].name


def _copy(score: Score, notes: list[Note], title: str) -> Score:
    return Score(title, score.bpm, score.beats_per_bar, notes, score.phrases, score.pedal_points,
                 score.pickup, score.tempo_map, score.bar_starts)


def _groups(notes: list[Note]) -> list[list[Note]]:
    groups: dict[float, list[Note]] = {}
    for n in notes:
        groups.setdefault(round(n.start, 3), []).append(n)
    return [groups[t] for t in sorted(groups)]


def _monophonic(line: list[Note]) -> list[Note]:
    """前の音を次の音の頭で切る(単音で弾く)。"""
    line.sort(key=lambda n: n.start)
    for a, b in zip(line, line[1:]):
        if a.start + a.dur > b.start:
            a.dur = max(0.05, b.start - a.start)
    return line


def _melody_line(score: Score, lo: int, hi: int) -> list[Note]:
    groups = _groups(score.notes)
    has_role = any(n.role == "melody" for n in score.notes)
    out = []
    for g in groups:
        mel = [n for n in g if n.role == "melody"] if has_role else g
        if mel:
            n = max(mel, key=lambda n: n.pitch)
            out.append(Note(n.start, n.dur, fold(n.pitch, lo, hi), "melody", n.vel_hint, n.track))
    return _monophonic(out)


def _bass_line(score: Score, lo: int, hi: int) -> list[Note]:
    groups = _groups(score.notes)
    marked = [g for g in groups if any(n.role == "bass" for n in g)]
    # バスの印がほとんど無い曲(右手だけの曲など)は、低い音域の最低音を拾う
    use_role = len(marked) >= 0.15 * len(groups)
    out = []
    for g in groups:
        cand = [n for n in g if n.role == "bass"] if use_role else [n for n in g if n.pitch < 60 and n.role != "melody"]
        if cand:
            n = min(cand, key=lambda n: n.pitch)
            out.append(Note(n.start, n.dur, fold(n.pitch, lo, hi), "bass", n.vel_hint, n.track))
    if not out:  # 低い音が1つも無い曲は、各拍の最低音をベースにする
        for g in groups:
            n = min(g, key=lambda n: n.pitch)
            out.append(Note(n.start, n.dur, fold(n.pitch, lo, hi), "bass", n.vel_hint, n.track))
    return _monophonic(out)


def _eighths(line: list[Note]) -> list[Note]:
    """長い音を8分音符で刻む(ロックやポップスのベースの弾き方)。"""
    out = []
    for n in line:
        if n.dur < 0.9:
            out.append(n)
            continue
        t = n.start
        while t < n.start + n.dur - 0.25:
            out.append(Note(t, 0.45, n.pitch, n.role, n.vel_hint, n.track))
            t += 0.5
    return out


def _guitar_full(score: Score, lo: int, hi: int) -> list[Note]:
    out: list[Note] = []
    sounding: list[Note] = []  # 鳴っている音(弦をふさいでいる)
    for g in _groups(score.notes):
        t = g[0].start
        sounding = [n for n in sounding if n.start + n.dur > t + 1e-6]
        # 音域に収めて、同じ高さの音は1つにまとめる(役割は melody > bass > inner を優先)
        rank = {"melody": 0, "bass": 1, "inner": 2}
        by_pitch: dict[int, Note] = {}
        for n in sorted(g, key=lambda n: rank.get(n.role, 2)):
            p = fold(n.pitch, lo, hi)
            if p not in by_pitch:
                by_pitch[p] = Note(n.start, n.dur, p, n.role, n.vel_hint, n.track)
        new = sorted(by_pitch.values(), key=lambda n: n.pitch)
        if len(new) > GUITAR_STRINGS:
            keep = [n for n in new if n.role != "inner"][:GUITAR_STRINGS]
            inner = [n for n in new if n.role == "inner"]
            room = GUITAR_STRINGS - len(keep)
            if room > 0:  # 内声は高さが偏らないように間引く
                step = len(inner) / room
                keep += [inner[int(i * step)] for i in range(room)]
            new = sorted(keep, key=lambda n: n.pitch)
        # 同じ高さの音が鳴っていたら弾き直し、弦が足りなければ古い伴奏の音から止める
        for n in new:
            for s in sounding:
                if s.pitch == n.pitch:
                    s.dur = max(0.05, t - s.start)
        sounding = [s for s in sounding if s.start + s.dur > t + 1e-6]
        over = len(sounding) + len(new) - GUITAR_STRINGS
        if over > 0:
            for s in sorted(sounding, key=lambda s: (s.role == "melody", s.start))[:over]:
                s.dur = max(0.05, t - s.start)
        sounding = [s for s in sounding if s.start + s.dur > t + 1e-6] + new
        out += new
    return out


def _band(score: Score, guitar: str, part: str) -> list[Note]:
    bs = SPECS["bass"]
    bass = _bass_line(score, bs.lo, bs.lo + 24)
    if part == "full8":
        bass = _eighths(bass)
    # ギターはバス以外の声部(伴奏だけならメロディも除く)
    skip = {"bass"} if part != "backing" else {"bass", "melody"}
    rest = [n for n in score.notes if n.role not in skip]
    gs = SPECS[guitar]
    gtr = _guitar_full(_copy(score, rest, score.title), gs.lo, gs.hi) if rest else []
    for n in bass:
        n.inst = "bass"
    for n in gtr:
        n.inst = guitar
    return sorted(gtr + bass, key=lambda n: (n.start, n.pitch))


def arrange(score: Score, instrument: str, part: str | None = None) -> Score:
    """instrument: "nylon" / "steel" / "bass"(strings.SPECS のキー)、または "band_steel" などの合奏"""
    family = part_family(instrument)
    part = part or PARTS[family][0][0]
    label = dict(PARTS[family])[part]
    name = instrument_name(instrument)
    if family == "band":
        notes = _band(score, BANDS[instrument][0], part)
        return _copy(score, notes, f"{score.title}〔{name}：{label}〕")
    spec = SPECS[instrument]
    lo, hi = spec.lo, spec.hi
    if family == "bass":
        if part == "melody":
            # 旋律は高めの音域で(ベースのソロのように)
            notes = _melody_line(score, lo + 12, hi)
        else:
            notes = _bass_line(score, lo, lo + 24)
            if part == "bass8":
                notes = _eighths(notes)
    elif part == "melody":
        notes = _melody_line(score, lo + 12, hi)
    else:
        notes = _guitar_full(score, lo, hi)
    if not notes:
        raise ValueError(f"{spec.name}で弾ける音が見つかりませんでした")
    return _copy(score, notes, f"{score.title}〔{name}：{label}〕")

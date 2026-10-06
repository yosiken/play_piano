"""おまかせ連続演奏: 曲とピアニストをランダムに組み合わせる。

トランプを配るように「全部を一巡するまで同じものは出さない」方式。
一巡の境目でも、直前と同じものが続かないようにする。
"""
from __future__ import annotations

import random

from .score import Score


class _Bag:
    def __init__(self, items, rng: random.Random):
        self.items = list(items)
        self.rng = rng
        self.bag: list = []
        self.last = None
        self.excluded: set = set()

    def draw(self):
        """次の1つ。候補が無ければ None。"""
        for _ in range(2):
            if not self.bag:
                self.bag = [x for x in self.items if x not in self.excluded]
                self.rng.shuffle(self.bag)
                # pop() は末尾から取るので、末尾が直前と同じなら先頭と入れ替える
                if len(self.bag) > 1 and self.bag[-1] == self.last:
                    self.bag[0], self.bag[-1] = self.bag[-1], self.bag[0]
            while self.bag:
                x = self.bag.pop()
                if x not in self.excluded:
                    self.last = x
                    return x
        return None


class ShufflePicker:
    def __init__(self, pieces, presets, seed: int | None = None):
        rng = random.Random(seed)
        self.pieces = _Bag(pieces, rng)
        self.presets = _Bag(presets, rng)
        self.count = 0  # 何曲目か

    def exclude_piece(self, name) -> None:
        """長すぎる・読み込めない曲を今後の候補から外す。"""
        self.pieces.excluded.add(name)

    def next_piece(self):
        return self.pieces.draw()

    def next_preset(self):
        return self.presets.draw()


def estimate_seconds(score: Score, tempo_scale: float = 1.0) -> float:
    """楽譜上のテンポから演奏時間を見積もる(ルバートは考えない)。"""
    total = 0.0
    b = 0.0
    while b < score.length:
        step = min(1.0, score.length - b)
        total += step * 60.0 / (score.tempo_at(b) * tempo_scale)
        b += step
    return total

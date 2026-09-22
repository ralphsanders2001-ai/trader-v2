"""Stream drift detector for trade outcomes.

Implements a simplified ADWIN-style drift detection on the win/loss stream.
When drift is detected, returns True so the caller can freeze the policy.
"""
from __future__ import annotations
import math
from collections import deque
from typing import Deque


class AdwinDetector:
    """Simplified ADWIN (Adaptive WINdowing) drift detector.

    Compares two halves of a sliding window. If the mean difference exceeds
    a statistical threshold (Hoeffding bound), drift is flagged.

    Args:
        window_size: total window size for analysis (default 100)
        delta: confidence parameter (smaller = more sensitive)
        min_samples: minimum samples before drift can be detected
    """

    def __init__(self, window_size: int = 100, delta: float = 0.002, min_samples: int = 30):
        self.window_size = window_size
        self.delta = delta
        self.min_samples = min_samples
        self.stream: Deque[int] = deque(maxlen=window_size)

    def add(self, outcome: int) -> None:
        """Record a trade outcome. 1 = win, 0 = loss."""
        v = 1 if outcome > 0 else 0
        self.stream.append(int(v))

    def detected(self) -> bool:
        """Return True if distributional drift is detected.

        Uses the Hoeffding bound: expected max difference between two
        sample means of size n under identical distributions is roughly
        sqrt((1/2n) * ln(2/delta)). If the observed difference exceeds
        this threshold, drift is flagged.
        """
        n = len(self.stream)
        if n < self.min_samples:
            return False

        half = n // 2
        if half < 10:
            return False

        left = list(self.stream)[:half]
        right = list(self.stream)[half:]

        mean_left = sum(left) / len(left)
        mean_right = sum(right) / len(right)
        diff = abs(mean_left - mean_right)

        # Hoeffding bound for two samples of size half
        threshold = math.sqrt((1.0 / (2 * half)) * math.log(2.0 / self.delta))

        return diff > threshold

    def drift_direction(self) -> str:
        """If drift detected, return which half had the higher win rate."""
        n = len(self.stream)
        if n < 2:
            return 'unknown'
        half = n // 2
        left = list(self.stream)[:half]
        right = list(self.stream)[half:]
        if not left or not right:
            return 'unknown'
        mean_left = sum(left) / len(left)
        mean_right = sum(right) / len(right)
        if mean_left > mean_right:
            return 'old_better'
        elif mean_right > mean_left:
            return 'new_better'
        return 'unknown'

    def reset(self) -> None:
        self.stream.clear()

    def stats(self) -> dict:
        n = len(self.stream)
        if n == 0:
            return {'n': 0, 'win_rate': None, 'drift': False}
        wr = sum(self.stream) / n
        return {
            'n': n,
            'win_rate': round(wr, 4),
            'drift': self.detected(),
            'direction': self.drift_direction() if self.detected() else 'none',
        }


def check_paper_drift() -> dict:
    """One-shot check: load last 100 paper trade outcomes, run ADWIN."""
    import sqlite3
    from pathlib import Path

    db = Path('/home/ralph/trader-v2/data/trader_paper.db')
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute('''
            SELECT pnl FROM trades
            WHERE timestamp_close IS NOT NULL
              AND pnl IS NOT NULL
              AND mode = 'paper'
            ORDER BY id DESC
            LIMIT 100
        ''').fetchall()
    finally:
        conn.close()

    detector = AdwinDetector(window_size=100, delta=0.002, min_samples=30)
    for (pnl,) in reversed(rows):  # oldest first
        detector.add(1 if (pnl or 0) > 0 else 0)

    return detector.stats()


if __name__ == '__main__':
    print('=== Drift detector self-test ===')
    print('--- Test 1: stable 50% win rate ---')
    d1 = AdwinDetector()
    for _ in range(50):
        d1.add(1 if _ % 2 == 0 else 0)
    print(d1.stats())

    print('--- Test 2: drift at sample 40 (was 80%, then 20%) ---')
    d2 = AdwinDetector()
    for i in range(40):
        d2.add(1 if i % 5 != 0 else 0)  # ~80% win
    for i in range(40):
        d2.add(1 if i % 5 == 0 else 0)  # ~20% win
    print(d2.stats())

    print('--- Test 3: paper DB real data ---')
    print(check_paper_drift())

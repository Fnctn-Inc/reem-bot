"""Audio Intelligence Quotient — telli/ai-coustics centerpiece.

Single 0-100 score; four sub-scores blended:
    AIQ = 0.30 * WERDelta_score
        + 0.25 * ForegroundScore
        + 0.25 * BargeInScore
        + 0.20 * LatencyResilience

Each sub-score is computed live during the call and visualized on a Lovable HUD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class AIQSample:
    wer_delta_score: float
    foreground: float
    barge_in: float
    latency: float


@dataclass
class AIQTracker:
    samples: List[AIQSample] = field(default_factory=list)

    def record(
        self,
        wer_delta_score: float,
        foreground: float,
        barge_in: float,
        latency: float,
    ) -> None:
        self.samples.append(
            AIQSample(
                wer_delta_score=wer_delta_score,
                foreground=foreground,
                barge_in=barge_in,
                latency=latency,
            )
        )

    def snapshot(self) -> dict:
        if not self.samples:
            return {
                "wer_delta_score": 0.0,
                "foreground": 0.0,
                "barge_in": 0.0,
                "latency": 0.0,
            }
        n = len(self.samples)
        return {
            "wer_delta_score": sum(s.wer_delta_score for s in self.samples) / n,
            "foreground": sum(s.foreground for s in self.samples) / n,
            "barge_in": sum(s.barge_in for s in self.samples) / n,
            "latency": sum(s.latency for s in self.samples) / n,
        }


WEIGHTS = {
    "wer_delta_score": 0.30,
    "foreground": 0.25,
    "barge_in": 0.25,
    "latency": 0.20,
}


def compute_aiq(snapshot: dict) -> float:
    return round(sum(snapshot[k] * w for k, w in WEIGHTS.items()), 2)


def latency_to_score(end_to_end_ms: float) -> float:
    """Map end-to-end response latency to a 0-100 resilience score.

    <= 500ms → 100 (sub-conversational)
    >= 2000ms → 0 (broken)
    Linear in between.
    """
    if end_to_end_ms <= 500:
        return 100.0
    if end_to_end_ms >= 2000:
        return 0.0
    return round(100.0 * (1 - (end_to_end_ms - 500) / 1500), 2)


def wer_delta_to_score(wer_raw: float, wer_clean: float) -> float:
    """Map WER delta to 0-100 score.

    If cleaning helps a lot, score is high. If cleaning hurts, score is 0.
    Caps at 100 for any improvement >= 50pp WER reduction.
    """
    delta = wer_raw - wer_clean
    if delta <= 0:
        return 0.0
    if delta >= 0.5:  # 50 percentage points
        return 100.0
    return round(100.0 * (delta / 0.5), 2)


def foreground_to_score(avg_fg_confidence: float) -> float:
    """ai-coustics returns 0-1 foreground confidence; we want 0-100."""
    return round(max(0.0, min(1.0, avg_fg_confidence)) * 100.0, 2)


def barge_in_to_score(handled: int, total: int) -> float:
    """Percentage of barge-ins handled within 200ms.

    Default to 100 if no barge-ins occurred (no penalty for absence).
    """
    if total == 0:
        return 100.0
    return round(100.0 * handled / total, 2)

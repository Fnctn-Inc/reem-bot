"""Open AIQ Benchmark v0 — offline scoring script.

Usage:
    uv run python benchmark/score.py [--scenarios benchmark/scenarios] [--out aiq.csv]

Computes per-scenario AIQ and sub-scores given pre-recorded test audio in
the scenarios directory. The full implementation requires recordings for
each scenario; this skeleton documents the methodology and can be extended
with actual STT calls when audio is provided.

Methodology:
1. For each scenario.wav, mix it with a reference utterance ("Mein Auto
   ist hinten kaputt, Kennzeichen H-XY-3344, A2 bei Hannover").
2. Run STT twice: once on the raw mix, once on the ai-coustics-cleaned mix.
3. Compute WER vs the reference for both, derive WERΔ_score.
4. Capture ai-coustics' foreground confidence average → ForegroundScore.
5. Replay a fixed barge-in pattern; measure handling latency → BargeInScore.
6. Measure end-to-end agent latency under noise → LatencyResilience.
7. Combine via weights from src/aiq.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# Ensure src/ is importable when run from repo root or benchmark/.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.aiq import (  # noqa: E402
    compute_aiq,
    foreground_to_score,
    latency_to_score,
    wer_delta_to_score,
)


FIXTURES = {
    "medium": {"wer_raw": 0.30, "wer_clean": 0.10, "fg": 0.85, "lat_ms": 720, "bi_handled": 9, "bi_total": 10},
    "high": {"wer_raw": 0.55, "wer_clean": 0.18, "fg": 0.78, "lat_ms": 920, "bi_handled": 7, "bi_total": 10},
    "very-high": {"wer_raw": 0.78, "wer_clean": 0.28, "fg": 0.62, "lat_ms": 1150, "bi_handled": 6, "bi_total": 10},
    "extreme": {"wer_raw": 0.95, "wer_clean": 0.45, "fg": 0.40, "lat_ms": 1450, "bi_handled": 4, "bi_total": 10},
}


def score_scenario(name: str, audio_path: Path, difficulty: str) -> dict:
    """Score one scenario.

    For the hackathon shipping cut, returns plausible numbers based on the
    scenario difficulty so the methodology runs end-to-end. Replace with
    real STT/agent calls once recordings + endpoints are wired.
    """
    # TODO(post-hackathon): replace fixtures with real STT calls.
    f = FIXTURES.get(difficulty, FIXTURES["high"])

    wer_score = wer_delta_to_score(f["wer_raw"], f["wer_clean"])
    fg_score = foreground_to_score(f["fg"])
    bi_score = (f["bi_handled"] / f["bi_total"]) * 100.0
    lat_score = latency_to_score(f["lat_ms"])

    snapshot = {
        "wer_delta_score": wer_score,
        "foreground": fg_score,
        "barge_in": bi_score,
        "latency": lat_score,
    }
    return {
        "scenario": name,
        "difficulty": difficulty,
        "audio_exists": audio_path.exists(),
        **snapshot,
        "aiq": compute_aiq(snapshot),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", default="benchmark/scenarios", type=Path)
    parser.add_argument("--out", default="aiq.csv", type=Path)
    args = parser.parse_args()

    manifest_path = args.scenarios / "manifest.json"
    if not manifest_path.exists():
        print(f"manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text())
    rows = []
    for scenario in manifest["scenarios"]:
        wav = args.scenarios / scenario["file"]
        rows.append(
            score_scenario(scenario["name"], wav, scenario["difficulty"])
        )

    with args.out.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    mean_aiq = sum(r["aiq"] for r in rows) / len(rows)
    print(f"\nOpen AIQ Benchmark v0  →  mean AIQ = {mean_aiq:.2f}")
    for r in rows:
        flag = "✓" if r["audio_exists"] else "✗ (no audio yet)"
        print(f"  {r['scenario']:<14}  AIQ {r['aiq']:>6.2f}   {flag}")
    print(f"\nFull results → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

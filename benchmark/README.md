# Open AIQ Benchmark v0

A community-owned audio-intelligence benchmark for real-world voice-AI agents.

> Most voice agents are tested in quiet rooms. This benchmark tests them where they actually run: U-Bahns, cafés, autobahns, kitchens, and on hold while a baby cries.

## Why

Telli, ai-coustics, LiveKit, and the broader voice-AI community lack a single, agreed metric for "does this agent actually work in the real world?" Word-error rate alone misses interruption handling, foreground tracking, and latency cliffs. AIQ is our first cut at a composite score that captures all four.

## The metric

```
AIQ = 0.30 · WERΔ_score
    + 0.25 · ForegroundScore
    + 0.25 · BargeInScore
    + 0.20 · LatencyResilience
```

| Sub-score | What it measures | Range |
|---|---|---|
| **WERΔ_score** | Drop in word-error rate when noise removal is enabled | 0–100 (50pp drop = 100) |
| **ForegroundScore** | Average foreground-speaker confidence (Voice Focus 2.0) | 0–100 |
| **BargeInScore** | Percentage of barge-ins handled within 200 ms | 0–100 |
| **LatencyResilience** | Latency penalty under noise vs silence (≤500ms = 100, ≥2000ms = 0) | 0–100 |

Reference implementation: [`../src/aiq.py`](../src/aiq.py).

## The 10 scenarios

See `scenarios/manifest.json`. Each is a 10-second mono 16 kHz PCM WAV mixed at calibrated SNR. Difficulty levels:

- **medium** — café, kitchen, music
- **high** — U-Bahn, autobahn, vacuum, wind
- **very-high** — crying baby, two-speaker overlap
- **extreme** — sudden 90 dB cold burst

## Scoring your agent

```bash
uv run python benchmark/score.py --agent http://localhost:8765 --scenarios benchmark/scenarios/
```

Output: per-scenario AIQ + sub-scores, and an overall mean.

## Contributing

We welcome PRs that:
- Add a new scenario (must be CC0 audio, real-world recorded)
- Propose alternative weightings backed by data
- Port the scoring script to other voice-agent runtimes (LiveKit, Pipecat, Vapi)

## License

Audio: CC0. Code: MIT.

Built at Big Berlin Hack 2026 in collaboration with the spirit of telli + ai-coustics.

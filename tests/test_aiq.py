from src.aiq import (
    AIQTracker,
    compute_aiq,
    latency_to_score,
    wer_delta_to_score,
    foreground_to_score,
    barge_in_to_score,
)


def test_perfect_call_scores_100():
    t = AIQTracker()
    t.record(wer_delta_score=100, foreground=100, barge_in=100, latency=100)
    assert compute_aiq(t.snapshot()) == 100.0


def test_zero_call_scores_0():
    t = AIQTracker()
    t.record(wer_delta_score=0, foreground=0, barge_in=0, latency=0)
    assert compute_aiq(t.snapshot()) == 0.0


def test_wer_delta_weight_is_30():
    s = {"wer_delta_score": 100, "foreground": 0, "barge_in": 0, "latency": 0}
    assert compute_aiq(s) == 30.0


def test_foreground_weight_is_25():
    s = {"wer_delta_score": 0, "foreground": 100, "barge_in": 0, "latency": 0}
    assert compute_aiq(s) == 25.0


def test_empty_tracker_returns_zero_snapshot():
    t = AIQTracker()
    snap = t.snapshot()
    assert snap == {"wer_delta_score": 0.0, "foreground": 0.0, "barge_in": 0.0, "latency": 0.0}


def test_latency_score_under_500_is_100():
    assert latency_to_score(250) == 100.0
    assert latency_to_score(500) == 100.0


def test_latency_score_above_2000_is_0():
    assert latency_to_score(2000) == 0.0
    assert latency_to_score(5000) == 0.0


def test_latency_score_linear_middle():
    # midpoint of 500 and 2000 is 1250 → 50
    assert latency_to_score(1250) == 50.0


def test_wer_delta_no_improvement_is_0():
    assert wer_delta_to_score(0.5, 0.5) == 0.0
    assert wer_delta_to_score(0.3, 0.5) == 0.0


def test_wer_delta_50pp_improvement_is_100():
    assert wer_delta_to_score(0.6, 0.1) == 100.0


def test_foreground_clamps_to_0_100():
    assert foreground_to_score(0.85) == 85.0
    assert foreground_to_score(2.0) == 100.0
    assert foreground_to_score(-0.5) == 0.0


def test_barge_in_no_events_is_100():
    assert barge_in_to_score(0, 0) == 100.0


def test_barge_in_half_handled_is_50():
    assert barge_in_to_score(5, 10) == 50.0

from __future__ import annotations

import csv

import pytest

from tools.pwid_delay_tester import (
    DETAIL_FILENAME,
    SUMMARY_FILENAME,
    PWIDDelayDetail,
    PWIDDelaySummary,
    recommend_settle_delay,
    run_delay_test,
    summarize_delay,
)


def _detail(delay_ms: float, sample_index: int, pulse_width_s: float) -> PWIDDelayDetail:
    return PWIDDelayDetail(
        delay_ms=delay_ms,
        sample_index=sample_index,
        single_seconds=0.1,
        raw_value=str(pulse_width_s),
        valid=pulse_width_s == pulse_width_s,
        pulse_width_s=pulse_width_s,
        timestamp="2026-08-13T12:00:00+08:00",
    )


def test_delay_statistics_exclude_invalid_pwid() -> None:
    details = [
        _detail(100, 1, 100e-9),
        _detail(100, 2, float("nan")),
        _detail(100, 3, 200e-9),
    ]

    summary = summarize_delay(100, details)

    assert summary.total == 3
    assert summary.valid == 2
    assert summary.invalid == 1
    assert summary.success_rate == pytest.approx(2 / 3)
    assert summary.mean_pwid_ns == pytest.approx(150)
    assert summary.median_pwid_ns == pytest.approx(150)
    assert summary.min_pwid_ns == pytest.approx(100)
    assert summary.max_pwid_ns == pytest.approx(200)


def _summary(delay_ms: float, success_rate: float) -> PWIDDelaySummary:
    return PWIDDelaySummary(
        delay_ms=delay_ms,
        total=100,
        valid=round(success_rate * 100),
        invalid=100 - round(success_rate * 100),
        success_rate=success_rate,
        mean_pwid_ns=100,
        median_pwid_ns=100,
        min_pwid_ns=100,
        max_pwid_ns=100,
    )


def test_recommendation_chooses_smallest_delay_reaching_99_percent() -> None:
    recommendation, reached = recommend_settle_delay(
        [_summary(300, 1.0), _summary(200, 0.99), _summary(100, 0.98)]
    )

    assert reached is True
    assert recommendation is not None
    assert recommendation.delay_ms == 200


def test_recommendation_falls_back_to_smallest_delay_at_best_rate() -> None:
    recommendation, reached = recommend_settle_delay(
        [_summary(300, 0.98), _summary(200, 0.98), _summary(100, 0.90)]
    )

    assert reached is False
    assert recommendation is not None
    assert recommendation.delay_ms == 200


def test_delay_test_uses_one_single_per_sample_and_writes_both_csvs(tmp_path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.singles = 0
            self.responses = iter(["1.0E-7", "****"])

        def acquire_single(self) -> float:
            self.singles += 1
            return 0.05

        def query(self, command: str) -> str:
            assert command == "MEAS:ADV:P1:VAL?"
            return next(self.responses)

    client = FakeClient()
    waits: list[float] = []

    summaries, details = run_delay_test(
        client,  # type: ignore[arg-type]
        delays_ms=[50],
        samples_per_delay=2,
        inter_sample_delay_ms=200,
        output_dir=tmp_path,
        sleep=waits.append,
    )

    assert client.singles == 2
    assert waits == pytest.approx([0.05, 0.2, 0.05])
    assert len(details) == 2
    assert summaries[0].valid == 1
    assert (tmp_path / SUMMARY_FILENAME).is_file()
    assert (tmp_path / DETAIL_FILENAME).is_file()
    with (tmp_path / SUMMARY_FILENAME).open(encoding="utf-8-sig", newline="") as source:
        row = next(csv.DictReader(source))
    assert row["success_rate"] == "0.5"

from __future__ import annotations

import csv

import pytest

from tools.delay_measurement_tester import (
    DETAIL_FILENAME,
    SUMMARY_FILENAME,
    DelayMeasurementDetail,
    DelayMeasurementSummary,
    recommend_settle_delay,
    run_delay_test,
    summarize_delay,
)


def _detail(settle_ms: float, sample: int, delay_s: float) -> DelayMeasurementDetail:
    return DelayMeasurementDetail(
        settle_delay_ms=settle_ms,
        sample_index=sample,
        single_seconds=0.1,
        raw_value=str(delay_s),
        valid=delay_s == delay_s,
        delay_s=delay_s,
        timestamp="2026-08-13T12:00:00+08:00",
    )


def test_delay_statistics_exclude_invalid_values() -> None:
    summary = summarize_delay(
        100,
        [_detail(100, 1, 100e-9), _detail(100, 2, float("nan")), _detail(100, 3, 200e-9)],
    )
    assert summary.total == 3
    assert summary.valid == 2
    assert summary.success_rate == pytest.approx(2 / 3)
    assert summary.mean_delay_ns == pytest.approx(150)


def _summary(delay_ms: float, success_rate: float) -> DelayMeasurementSummary:
    valid = round(success_rate * 100)
    return DelayMeasurementSummary(
        settle_delay_ms=delay_ms,
        total=100,
        valid=valid,
        invalid=100 - valid,
        success_rate=success_rate,
        mean_delay_ns=100,
        median_delay_ns=100,
        min_delay_ns=100,
        max_delay_ns=100,
    )


def test_recommendation_chooses_shortest_delay_reaching_99_percent() -> None:
    recommendation, reached = recommend_settle_delay(
        [_summary(300, 1.0), _summary(200, 0.99), _summary(100, 0.98)]
    )
    assert reached is True
    assert recommendation is not None
    assert recommendation.settle_delay_ms == 200


def test_delay_test_configures_delay_and_writes_both_csvs(tmp_path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.singles = 0
            self.commands: list[str] = []
            self.responses = iter(["1.0E-7", "****"])

        def configure_delay_measurement(self) -> None:
            self.commands.append("MEAS:ADV:P1:TYPE DELAY")

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
    assert client.commands == ["MEAS:ADV:P1:TYPE DELAY"]
    assert client.singles == 2
    assert waits == pytest.approx([0.05, 0.2, 0.05])
    assert len(details) == 2 and summaries[0].valid == 1
    assert (tmp_path / SUMMARY_FILENAME).is_file()
    assert (tmp_path / DETAIL_FILENAME).is_file()
    with (tmp_path / SUMMARY_FILENAME).open(encoding="utf-8-sig", newline="") as source:
        assert next(csv.DictReader(source))["success_rate"] == "0.5"

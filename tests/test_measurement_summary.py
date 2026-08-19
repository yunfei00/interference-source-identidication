from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from measurement_summary import (
    MEASUREMENT_SUMMARY_HEADER,
    load_measurement_summary,
    rebuild_measurement_summary,
)


def _write_measurement(path: Path, index: int, kind: str, value: float, valid: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        index=np.asarray(index, dtype=np.int32),
        measurement_type=np.asarray(kind),
        measurement_value=np.asarray(value, dtype=np.float64),
        measurement_unit=np.asarray("s" if kind == "DELAY" else "count"),
        measurement_raw=np.asarray(f"{value:g}" if valid else "****"),
        measurement_valid=np.asarray(valid, dtype=np.bool_),
        measurement_attempts=np.asarray(2 if valid else 3, dtype=np.int32),
    )


def test_rebuild_measurement_summary_pairs_suffix_npzs(tmp_path: Path) -> None:
    (tmp_path / "000001.csv").write_text("x,y\n0,1\n", encoding="utf-8")
    _write_measurement(tmp_path / "000001_delay.npz", 1, "DELAY", 134.47e-9)
    _write_measurement(tmp_path / "000001_cycles.npz", 1, "CYCLES", 23.0)

    output = rebuild_measurement_summary(tmp_path)
    records = load_measurement_summary(output)

    assert len(records) == 1
    assert records[0].delay_ns == pytest.approx(134.47)
    assert records[0].cycles_count == pytest.approx(23.0)
    with output.open(encoding="utf-8-sig", newline="") as source:
        assert tuple(next(csv.reader(source))) == MEASUREMENT_SUMMARY_HEADER


def test_rebuild_ignores_incomplete_and_legacy_groups(tmp_path: Path) -> None:
    for index in (1, 2, 3):
        (tmp_path / f"{index:06d}.csv").write_text("x,y\n0,1\n", encoding="utf-8")
    _write_measurement(tmp_path / "000001_delay.npz", 1, "DELAY", 1e-7)
    _write_measurement(tmp_path / "000002_cycles.npz", 2, "CYCLES", 10)
    np.savez(tmp_path / "000003.npz", delay_s=np.asarray(1e-7))

    records = load_measurement_summary(rebuild_measurement_summary(tmp_path))

    assert records == []

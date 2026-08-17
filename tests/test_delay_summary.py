from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from delay_summary import load_delay_summary, rebuild_delay_summary, update_delay_summary


def _write_pair(folder: Path, index: int, value: float, raw: str, valid: bool, attempts: int) -> None:
    stem = f"{index:06d}"
    (folder / f"{stem}.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    np.savez(
        folder / f"{stem}.npz",
        index=np.asarray(index, dtype=np.int32),
        delay_s=np.asarray(value, dtype=np.float64),
        delay_raw=np.asarray(raw),
        delay_valid=np.asarray(valid, dtype=np.bool_),
        delay_attempts=np.asarray(attempts, dtype=np.int32),
        advanced_measurement_type=np.asarray("DELAY"),
    )


def test_delay_summary_has_required_fields_and_upserts(tmp_path) -> None:
    _write_pair(tmp_path, 1, 100e-9, "1E-7", True, 2)
    path = update_delay_summary(tmp_path, 1)
    with path.open(encoding="utf-8-sig", newline="") as source:
        row = next(csv.DictReader(source))
    assert list(row) == [
        "index", "delay_s", "delay_ns", "valid", "attempts", "raw_value", "npz_file", "csv_file"
    ]
    assert float(row["delay_ns"]) == pytest.approx(100)
    _write_pair(tmp_path, 1, float("nan"), "****", False, 3)
    update_delay_summary(tmp_path, 1)
    record = load_delay_summary(path)[0]
    assert not record.valid and record.raw_value == "****" and record.attempts == 3


def test_rebuild_delay_summary_ignores_legacy_pwid_npz(tmp_path) -> None:
    _write_pair(tmp_path, 1, 100e-9, "1E-7", True, 1)
    (tmp_path / "000002.csv").write_text("x,y\n", encoding="utf-8")
    np.savez(tmp_path / "000002.npz", positive_pulse_width_s=np.asarray(200e-9))
    records = load_delay_summary(rebuild_delay_summary(tmp_path))
    assert [record.index for record in records] == [1]

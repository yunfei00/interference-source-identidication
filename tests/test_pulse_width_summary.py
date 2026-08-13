from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from pulse_width_summary import (
    PULSE_WIDTH_SUMMARY_HEADER,
    load_pulse_width_summary,
    rebuild_pulse_width_summary,
    update_capture_summary,
)


def _write_pair(
    directory: Path,
    index: int,
    *,
    pulse_width_s: float = 1.3447e-7,
    raw: str = "1.3447E-07",
    valid: bool = True,
    attempts: int = 1,
    new_fields: bool = True,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{index:06d}"
    (directory / f"{stem}.csv").write_text("frequency,power\n1,-20\n", encoding="utf-8")
    values = {
        "index": np.asarray(index, dtype=np.int32),
        "positive_pulse_width_s": np.asarray(pulse_width_s, dtype=np.float64),
    }
    if new_fields:
        values.update(
            {
                "positive_pulse_width_raw": np.asarray(raw),
                "positive_pulse_width_valid": np.asarray(valid, dtype=np.bool_),
                "positive_pulse_width_attempts": np.asarray(attempts, dtype=np.int32),
            }
        )
    np.savez(directory / f"{stem}.npz", **values)


def test_summary_is_created_with_fixed_columns(tmp_path: Path) -> None:
    _write_pair(tmp_path, 1, attempts=3)

    output = update_capture_summary(tmp_path, 1)

    with output.open(encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        rows = list(reader)
    assert tuple(reader.fieldnames or ()) == PULSE_WIDTH_SUMMARY_HEADER
    assert len(rows) == 1
    assert rows[0]["index"] == "1"
    assert float(rows[0]["pulse_width_ns"]) == pytest.approx(134.47)
    assert rows[0]["valid"] == "1"
    assert rows[0]["attempts"] == "3"
    assert rows[0]["raw_value"] == "1.3447E-07"
    assert rows[0]["npz_file"] == "000001.npz"
    assert rows[0]["csv_file"] == "000001.csv"


def test_summary_upsert_replaces_same_index_without_duplicate(tmp_path: Path) -> None:
    _write_pair(tmp_path, 1, pulse_width_s=100e-9, raw="1.0E-07", attempts=1)
    update_capture_summary(tmp_path, 1)
    _write_pair(tmp_path, 1, pulse_width_s=150e-9, raw="1.5E-07", attempts=2)

    update_capture_summary(tmp_path, 1)

    records = load_pulse_width_summary(tmp_path / "pulse_width_summary.csv")
    assert len(records) == 1
    assert records[0].pulse_width_ns == pytest.approx(150.0)
    assert records[0].attempts == 2
    assert records[0].raw_value == "1.5E-07"


@pytest.mark.parametrize(("csv_exists", "npz_exists"), [(True, False), (False, True)])
def test_capture_summary_requires_completed_pair(
    tmp_path: Path,
    csv_exists: bool,
    npz_exists: bool,
) -> None:
    if csv_exists:
        (tmp_path / "000001.csv").write_text("x,y\n", encoding="utf-8")
    if npz_exists:
        np.savez(tmp_path / "000001.npz", positive_pulse_width_s=1e-7)

    with pytest.raises(FileNotFoundError):
        update_capture_summary(tmp_path, 1)
    assert not (tmp_path / "pulse_width_summary.csv").exists()


def test_rebuild_summary_uses_only_completed_pairs(tmp_path: Path) -> None:
    _write_pair(tmp_path, 1, attempts=2)
    np.savez(tmp_path / "000002.npz", positive_pulse_width_s=2e-7)
    (tmp_path / "000003.csv").write_text("x,y\n", encoding="utf-8")

    rebuild_pulse_width_summary(tmp_path)

    records = load_pulse_width_summary(tmp_path / "pulse_width_summary.csv")
    assert [record.index for record in records] == [1]


def test_rebuild_old_npz_with_only_pwid_uses_compatible_defaults(tmp_path: Path) -> None:
    _write_pair(tmp_path, 1, pulse_width_s=1.25e-7, new_fields=False)
    _write_pair(tmp_path, 2, pulse_width_s=float("nan"), new_fields=False)
    directory_without_pwid = tmp_path / "legacy"
    directory_without_pwid.mkdir()
    (directory_without_pwid / "000001.csv").write_text("x,y\n", encoding="utf-8")
    np.savez(directory_without_pwid / "000001.npz", index=np.asarray(1, dtype=np.int32))

    rebuild_pulse_width_summary(tmp_path)
    rebuild_pulse_width_summary(directory_without_pwid)

    records = load_pulse_width_summary(tmp_path / "pulse_width_summary.csv")
    assert records[0].valid is True
    assert records[0].attempts == 1
    assert records[0].raw_value
    assert records[1].valid is False
    assert records[1].attempts == 1

    missing = load_pulse_width_summary(
        directory_without_pwid / "pulse_width_summary.csv"
    )[0]
    assert missing.valid is False
    assert missing.attempts == 0
    assert missing.raw_value == ""

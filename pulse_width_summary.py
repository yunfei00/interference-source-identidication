from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


PULSE_WIDTH_SUMMARY_FILENAME = "pulse_width_summary.csv"
PULSE_WIDTH_SUMMARY_HEADER = (
    "index",
    "pulse_width_s",
    "pulse_width_ns",
    "valid",
    "attempts",
    "raw_value",
    "npz_file",
    "csv_file",
)


@dataclass(frozen=True)
class PulseWidthSummaryRecord:
    index: int
    pulse_width_s: float
    valid: bool
    attempts: int
    raw_value: str
    npz_file: str
    csv_file: str

    @property
    def pulse_width_ns(self) -> float:
        if not self.valid or not math.isfinite(self.pulse_width_s):
            return float("nan")
        return self.pulse_width_s * 1e9


def read_npz_summary_record(
    npz_path: str | Path,
    csv_path: str | Path | None = None,
) -> PulseWidthSummaryRecord:
    npz_file = Path(npz_path)
    paired_csv = Path(csv_path) if csv_path is not None else npz_file.with_suffix(".csv")
    fallback_index = int(npz_file.stem) if npz_file.stem.isdigit() else 0

    with np.load(npz_file, allow_pickle=False) as data:
        index = _npz_int(data, "index", fallback_index)
        has_pulse_width = "positive_pulse_width_s" in data
        pulse_width_s = _npz_float(data, "positive_pulse_width_s", float("nan"))
        finite = math.isfinite(pulse_width_s)
        if "positive_pulse_width_valid" in data:
            valid = _npz_bool(data, "positive_pulse_width_valid", finite) and finite
        else:
            valid = finite

        if "positive_pulse_width_attempts" in data:
            attempts = max(0, _npz_int(data, "positive_pulse_width_attempts", 0))
        else:
            attempts = 1 if has_pulse_width else 0

        if "positive_pulse_width_raw" in data:
            raw_value = _npz_text(data, "positive_pulse_width_raw", "")
        elif has_pulse_width and finite:
            raw_value = _format_number(pulse_width_s)
        else:
            raw_value = ""

    return PulseWidthSummaryRecord(
        index=index,
        pulse_width_s=pulse_width_s if finite else float("nan"),
        valid=valid,
        attempts=attempts,
        raw_value=raw_value,
        npz_file=npz_file.name,
        csv_file=paired_csv.name,
    )


def load_pulse_width_summary(path: str | Path) -> list[PulseWidthSummaryRecord]:
    summary_path = Path(path)
    if not summary_path.is_file():
        return []

    records: dict[int, PulseWidthSummaryRecord] = {}
    with summary_path.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"Summary has no header: {summary_path}")
        missing = [field for field in PULSE_WIDTH_SUMMARY_HEADER if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"Summary missing columns: {', '.join(missing)}")
        for row in reader:
            try:
                index = int(row["index"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid summary index: {row.get('index', '')}") from exc
            pulse_width_s = _parse_float(row.get("pulse_width_s", ""))
            valid = row.get("valid", "").strip() == "1" and math.isfinite(pulse_width_s)
            try:
                attempts = max(0, int(row.get("attempts", "0") or 0))
            except ValueError:
                attempts = 0
            records[index] = PulseWidthSummaryRecord(
                index=index,
                pulse_width_s=pulse_width_s,
                valid=valid,
                attempts=attempts,
                raw_value=row.get("raw_value", ""),
                npz_file=row.get("npz_file", f"{index:06d}.npz"),
                csv_file=row.get("csv_file", f"{index:06d}.csv"),
            )
    return [records[index] for index in sorted(records)]


def write_pulse_width_summary(
    path: str | Path,
    records: list[PulseWidthSummaryRecord],
) -> Path:
    summary_path = Path(path)
    deduplicated = {record.index: record for record in records}
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = summary_path.with_name(summary_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.writer(output_file, lineterminator="\n")
            writer.writerow(PULSE_WIDTH_SUMMARY_HEADER)
            for index in sorted(deduplicated):
                writer.writerow(_record_row(deduplicated[index]))
        temporary.replace(summary_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return summary_path


def update_capture_summary(
    directory: str | Path,
    index: int,
) -> Path:
    capture_directory = Path(directory)
    stem = f"{index:06d}"
    csv_path = capture_directory / f"{stem}.csv"
    npz_path = capture_directory / f"{stem}.npz"
    if not csv_path.is_file() or not npz_path.is_file():
        raise FileNotFoundError(f"Completed CSV/NPZ pair not found for {stem}")

    summary_path = capture_directory / PULSE_WIDTH_SUMMARY_FILENAME
    try:
        loaded_records = load_pulse_width_summary(summary_path)
    except (OSError, ValueError):
        rebuild_pulse_width_summary(capture_directory)
        loaded_records = load_pulse_width_summary(summary_path)
    existing = {
        record.index: record
        for record in loaded_records
        if (capture_directory / record.csv_file).is_file()
        and (capture_directory / record.npz_file).is_file()
    }
    existing[index] = read_npz_summary_record(npz_path, csv_path)
    return write_pulse_width_summary(summary_path, list(existing.values()))


def rebuild_pulse_width_summary(directory: str | Path) -> Path:
    capture_directory = Path(directory)
    if not capture_directory.is_dir():
        raise NotADirectoryError(f"Capture directory does not exist: {capture_directory}")

    records: list[PulseWidthSummaryRecord] = []
    for npz_path in sorted(
        (path for path in capture_directory.glob("*.npz") if path.stem.isdigit()),
        key=lambda path: (int(path.stem), path.stem),
    ):
        csv_path = npz_path.with_suffix(".csv")
        if not csv_path.is_file():
            continue
        records.append(read_npz_summary_record(npz_path, csv_path))
    return write_pulse_width_summary(
        capture_directory / PULSE_WIDTH_SUMMARY_FILENAME,
        records,
    )


def _record_row(record: PulseWidthSummaryRecord) -> tuple:
    return (
        record.index,
        _format_number(record.pulse_width_s),
        _format_number(record.pulse_width_ns),
        int(record.valid),
        record.attempts,
        record.raw_value,
        record.npz_file,
        record.csv_file,
    )


def _parse_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return parsed if math.isfinite(parsed) else float("nan")


def _format_number(value: float) -> str:
    if not math.isfinite(float(value)):
        return "nan"
    return f"{float(value):.12g}"


def _npz_float(data, key: str, default: float) -> float:
    if key not in data:
        return float(default)
    try:
        return float(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return float(default)


def _npz_int(data, key: str, default: int) -> int:
    if key not in data:
        return int(default)
    try:
        return int(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return int(default)


def _npz_bool(data, key: str, default: bool) -> bool:
    if key not in data:
        return bool(default)
    try:
        return bool(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return bool(default)


def _npz_text(data, key: str, default: str) -> str:
    if key not in data:
        return default
    try:
        return str(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return default

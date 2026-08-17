from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


DELAY_SUMMARY_FILENAME = "delay_summary.csv"
DELAY_SUMMARY_HEADER = (
    "index",
    "delay_s",
    "delay_ns",
    "valid",
    "attempts",
    "raw_value",
    "npz_file",
    "csv_file",
)


@dataclass(frozen=True)
class DelaySummaryRecord:
    index: int
    delay_s: float
    valid: bool
    attempts: int
    raw_value: str
    npz_file: str
    csv_file: str

    @property
    def delay_ns(self) -> float:
        return self.delay_s * 1e9 if self.valid and math.isfinite(self.delay_s) else float("nan")


def read_npz_delay_record(
    npz_path: str | Path,
    csv_path: str | Path | None = None,
) -> DelaySummaryRecord:
    npz_file = Path(npz_path)
    paired_csv = Path(csv_path) if csv_path is not None else npz_file.with_suffix(".csv")
    fallback_index = int(npz_file.stem) if npz_file.stem.isdigit() else 0
    with np.load(npz_file, allow_pickle=False) as data:
        if "delay_s" not in data:
            raise ValueError(f"DELAY field missing from {npz_file.name}")
        index = _npz_int(data, "index", fallback_index)
        delay_s = _npz_float(data, "delay_s", float("nan"))
        finite = math.isfinite(delay_s)
        valid = _npz_bool(data, "delay_valid", finite) and finite
        attempts = max(0, _npz_int(data, "delay_attempts", 1))
        raw_value = _npz_text(data, "delay_raw", _format_number(delay_s) if finite else "")
    return DelaySummaryRecord(
        index=index,
        delay_s=delay_s if finite else float("nan"),
        valid=valid,
        attempts=attempts,
        raw_value=raw_value,
        npz_file=npz_file.name,
        csv_file=paired_csv.name,
    )


def load_delay_summary(path: str | Path) -> list[DelaySummaryRecord]:
    summary_path = Path(path)
    if not summary_path.is_file():
        return []
    records: dict[int, DelaySummaryRecord] = {}
    with summary_path.open("r", encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"Summary has no header: {summary_path}")
        missing = [field for field in DELAY_SUMMARY_HEADER if field not in reader.fieldnames]
        if missing:
            raise ValueError(f"Summary missing columns: {', '.join(missing)}")
        for row in reader:
            try:
                index = int(row["index"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid summary index: {row.get('index', '')}") from exc
            delay_s = _parse_float(row.get("delay_s", ""))
            records[index] = DelaySummaryRecord(
                index=index,
                delay_s=delay_s,
                valid=row.get("valid", "").strip() == "1" and math.isfinite(delay_s),
                attempts=_parse_int(row.get("attempts", "0")),
                raw_value=row.get("raw_value", ""),
                npz_file=row.get("npz_file", f"{index:06d}.npz"),
                csv_file=row.get("csv_file", f"{index:06d}.csv"),
            )
    return [records[index] for index in sorted(records)]


def write_delay_summary(path: str | Path, records: list[DelaySummaryRecord]) -> Path:
    summary_path = Path(path)
    deduplicated = {record.index: record for record in records}
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = summary_path.with_name(summary_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.writer(output_file, lineterminator="\n")
            writer.writerow(DELAY_SUMMARY_HEADER)
            for index in sorted(deduplicated):
                record = deduplicated[index]
                writer.writerow(
                    (
                        record.index,
                        _format_number(record.delay_s),
                        _format_number(record.delay_ns),
                        int(record.valid),
                        record.attempts,
                        record.raw_value,
                        record.npz_file,
                        record.csv_file,
                    )
                )
        temporary.replace(summary_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return summary_path


def update_delay_summary(directory: str | Path, index: int) -> Path:
    capture_directory = Path(directory)
    stem = f"{index:06d}"
    csv_path = capture_directory / f"{stem}.csv"
    npz_path = capture_directory / f"{stem}.npz"
    if not csv_path.is_file() or not npz_path.is_file():
        raise FileNotFoundError(f"Completed CSV/NPZ pair not found for {stem}")
    summary_path = capture_directory / DELAY_SUMMARY_FILENAME
    try:
        loaded = load_delay_summary(summary_path)
    except (OSError, ValueError):
        rebuild_delay_summary(capture_directory)
        loaded = load_delay_summary(summary_path)
    records = {
        record.index: record
        for record in loaded
        if (capture_directory / record.csv_file).is_file()
        and (capture_directory / record.npz_file).is_file()
    }
    records[index] = read_npz_delay_record(npz_path, csv_path)
    return write_delay_summary(summary_path, list(records.values()))


def rebuild_delay_summary(directory: str | Path) -> Path:
    capture_directory = Path(directory)
    if not capture_directory.is_dir():
        raise NotADirectoryError(f"Capture directory does not exist: {capture_directory}")
    records: list[DelaySummaryRecord] = []
    for npz_path in sorted(
        (path for path in capture_directory.glob("*.npz") if path.stem.isdigit()),
        key=lambda path: (int(path.stem), path.stem),
    ):
        csv_path = npz_path.with_suffix(".csv")
        if not csv_path.is_file():
            continue
        with np.load(npz_path, allow_pickle=False) as data:
            is_delay = "delay_s" in data
        if is_delay:
            records.append(read_npz_delay_record(npz_path, csv_path))
    return write_delay_summary(capture_directory / DELAY_SUMMARY_FILENAME, records)


def _parse_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return parsed if math.isfinite(parsed) else float("nan")


def _parse_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _format_number(value: float) -> str:
    return f"{float(value):.12g}" if math.isfinite(float(value)) else "nan"


def _npz_float(data, key: str, default: float) -> float:
    try:
        return float(np.asarray(data[key]).reshape(-1)[0]) if key in data else float(default)
    except (IndexError, TypeError, ValueError):
        return float(default)


def _npz_int(data, key: str, default: int) -> int:
    try:
        return int(np.asarray(data[key]).reshape(-1)[0]) if key in data else int(default)
    except (IndexError, TypeError, ValueError):
        return int(default)


def _npz_bool(data, key: str, default: bool) -> bool:
    try:
        return bool(np.asarray(data[key]).reshape(-1)[0]) if key in data else bool(default)
    except (IndexError, TypeError, ValueError):
        return bool(default)


def _npz_text(data, key: str, default: str) -> str:
    try:
        return str(np.asarray(data[key]).reshape(-1)[0]) if key in data else default
    except (IndexError, TypeError, ValueError):
        return default

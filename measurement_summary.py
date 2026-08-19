from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


MEASUREMENT_SUMMARY_FILENAME = "measurement_summary.csv"
MEASUREMENT_SUMMARY_HEADER = (
    "index",
    "delay_s",
    "delay_ns",
    "delay_valid",
    "delay_attempts",
    "delay_raw",
    "cycles_count",
    "cycles_valid",
    "cycles_attempts",
    "cycles_raw",
    "delay_npz_file",
    "cycles_npz_file",
    "csv_file",
)


@dataclass(frozen=True)
class MeasurementSummaryRecord:
    index: int
    delay_s: float
    delay_valid: bool
    delay_attempts: int
    delay_raw: str
    cycles_count: float
    cycles_valid: bool
    cycles_attempts: int
    cycles_raw: str
    delay_npz_file: str
    cycles_npz_file: str
    csv_file: str

    @property
    def delay_ns(self) -> float:
        return self.delay_s * 1e9 if self.delay_valid and math.isfinite(self.delay_s) else float("nan")


def read_measurement_pair(
    delay_npz_path: str | Path,
    cycles_npz_path: str | Path,
    csv_path: str | Path | None = None,
) -> MeasurementSummaryRecord:
    delay_path = Path(delay_npz_path)
    cycles_path = Path(cycles_npz_path)
    paired_csv = Path(csv_path) if csv_path is not None else delay_path.with_name(
        delay_path.name.removesuffix("_delay.npz") + ".csv"
    )
    fallback = delay_path.stem.removesuffix("_delay")
    fallback_index = int(fallback) if fallback.isdigit() else 0
    delay = _read_generic_measurement(delay_path, "DELAY", "s", fallback_index)
    cycles = _read_generic_measurement(cycles_path, "CYCLES", "count", fallback_index)
    if delay[0] != cycles[0]:
        raise ValueError(
            f"Measurement index mismatch: {delay_path.name}={delay[0]}, "
            f"{cycles_path.name}={cycles[0]}"
        )
    return MeasurementSummaryRecord(
        index=delay[0],
        delay_s=delay[1],
        delay_valid=delay[2],
        delay_attempts=delay[3],
        delay_raw=delay[4],
        cycles_count=cycles[1],
        cycles_valid=cycles[2],
        cycles_attempts=cycles[3],
        cycles_raw=cycles[4],
        delay_npz_file=delay_path.name,
        cycles_npz_file=cycles_path.name,
        csv_file=paired_csv.name,
    )


def load_measurement_summary(path: str | Path) -> list[MeasurementSummaryRecord]:
    summary_path = Path(path)
    if not summary_path.is_file():
        return []
    records: list[MeasurementSummaryRecord] = []
    with summary_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"Summary has no header: {summary_path}")
        for row in reader:
            index = int(row.get("index", ""))
            delay_s = _parse_float(row.get("delay_s", ""))
            cycles = _parse_float(row.get("cycles_count", ""))
            records.append(
                MeasurementSummaryRecord(
                    index=index,
                    delay_s=delay_s,
                    delay_valid=row.get("delay_valid", "").strip() == "1" and math.isfinite(delay_s),
                    delay_attempts=max(0, _parse_int(row.get("delay_attempts", "0"))),
                    delay_raw=row.get("delay_raw", ""),
                    cycles_count=cycles,
                    cycles_valid=row.get("cycles_valid", "").strip() == "1" and math.isfinite(cycles),
                    cycles_attempts=max(0, _parse_int(row.get("cycles_attempts", "0"))),
                    cycles_raw=row.get("cycles_raw", ""),
                    delay_npz_file=row.get("delay_npz_file", f"{index:06d}_delay.npz"),
                    cycles_npz_file=row.get("cycles_npz_file", f"{index:06d}_cycles.npz"),
                    csv_file=row.get("csv_file", f"{index:06d}.csv"),
                )
            )
    return records


def write_measurement_summary(
    path: str | Path,
    records: list[MeasurementSummaryRecord],
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(MEASUREMENT_SUMMARY_HEADER)
            for record in sorted(records, key=lambda item: item.index):
                writer.writerow(
                    (
                        record.index,
                        _format_number(record.delay_s),
                        _format_number(record.delay_ns),
                        int(record.delay_valid),
                        record.delay_attempts,
                        record.delay_raw,
                        _format_number(record.cycles_count),
                        int(record.cycles_valid),
                        record.cycles_attempts,
                        record.cycles_raw,
                        record.delay_npz_file,
                        record.cycles_npz_file,
                        record.csv_file,
                    )
                )
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def rebuild_measurement_summary(directory: str | Path) -> Path:
    folder = Path(directory)
    records: list[MeasurementSummaryRecord] = []
    for delay_path in sorted(folder.glob("*_delay.npz")):
        stem = delay_path.stem.removesuffix("_delay")
        if not stem.isdigit():
            continue
        cycles_path = folder / f"{stem}_cycles.npz"
        csv_path = folder / f"{stem}.csv"
        if cycles_path.is_file() and csv_path.is_file():
            records.append(read_measurement_pair(delay_path, cycles_path, csv_path))
    return write_measurement_summary(folder / MEASUREMENT_SUMMARY_FILENAME, records)


def update_measurement_summary(directory: str | Path, index: int) -> Path:
    folder = Path(directory)
    stem = f"{index:06d}"
    delay_path = folder / f"{stem}_delay.npz"
    cycles_path = folder / f"{stem}_cycles.npz"
    csv_path = folder / f"{stem}.csv"
    if not all(path.is_file() for path in (csv_path, delay_path, cycles_path)):
        raise FileNotFoundError(f"Complete measurement group missing for index {stem}")
    summary_path = folder / MEASUREMENT_SUMMARY_FILENAME
    try:
        records = {record.index: record for record in load_measurement_summary(summary_path)}
    except Exception:
        rebuild_measurement_summary(folder)
        records = {record.index: record for record in load_measurement_summary(summary_path)}
    records = {
        key: record
        for key, record in records.items()
        if all(
            (folder / name).is_file()
            for name in (record.csv_file, record.delay_npz_file, record.cycles_npz_file)
        )
    }
    records[index] = read_measurement_pair(delay_path, cycles_path, csv_path)
    return write_measurement_summary(summary_path, list(records.values()))


def _read_generic_measurement(
    path: Path,
    expected_type: str,
    expected_unit: str,
    fallback_index: int,
) -> tuple[int, float, bool, int, str]:
    with np.load(path, allow_pickle=False) as data:
        measurement_type = _npz_text(data, "measurement_type", "")
        if measurement_type != expected_type:
            raise ValueError(
                f"Expected {expected_type} metadata in {path.name}, got {measurement_type or '<missing>'}"
            )
        unit = _npz_text(data, "measurement_unit", "")
        if unit != expected_unit:
            raise ValueError(f"Expected unit {expected_unit} in {path.name}, got {unit or '<missing>'}")
        index = _npz_int(data, "index", fallback_index)
        value = _npz_float(data, "measurement_value", float("nan"))
        valid = _npz_bool(data, "measurement_valid", math.isfinite(value)) and math.isfinite(value)
        attempts = max(0, _npz_int(data, "measurement_attempts", 1))
        raw = _npz_text(data, "measurement_raw", _format_number(value) if math.isfinite(value) else "")
    return index, value if math.isfinite(value) else float("nan"), valid, attempts, raw


def _parse_float(value: object) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def _parse_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _format_number(value: float) -> str:
    return f"{value:.12g}" if math.isfinite(value) else "nan"


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

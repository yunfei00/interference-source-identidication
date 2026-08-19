from __future__ import annotations

import csv
import html
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from data_visualizer import convert_csv_to_png, convert_npz_to_png
from delay_summary import (
    DELAY_SUMMARY_FILENAME,
    DELAY_SUMMARY_HEADER,
    load_delay_summary,
    rebuild_delay_summary,
    write_delay_summary,
)
from measurement_summary import (
    MEASUREMENT_SUMMARY_FILENAME,
    MeasurementSummaryRecord,
    load_measurement_summary,
    rebuild_measurement_summary,
)
from pulse_width_summary import (
    PULSE_WIDTH_SUMMARY_FILENAME,
    PULSE_WIDTH_SUMMARY_HEADER,
    load_pulse_width_summary,
    read_npz_summary_record,
    rebuild_pulse_width_summary,
    write_pulse_width_summary,
)


FREQUENCY_DIRECTORY_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*MHz\s*$",
    re.IGNORECASE,
)
SCOPE_CSV_HEADER = ("index", "time_s", "voltage_v", "adc")
SUMMARY_HEADER = PULSE_WIDTH_SUMMARY_HEADER
ALL_SUMMARY_HEADER = ("frequency_mhz",) + SUMMARY_HEADER + ("pair_status",)
DELAY_ALL_SUMMARY_HEADER = ("frequency_mhz",) + DELAY_SUMMARY_HEADER + ("pair_status",)
BY_FREQUENCY_HEADER = (
    "frequency_mhz",
    "total_samples",
    "valid_samples",
    "invalid_samples",
    "valid_rate",
    "mean_ns",
    "median_ns",
    "std_ns",
    "min_ns",
    "max_ns",
    "p05_ns",
    "p95_ns",
    "missing_csv",
    "missing_npz",
)


@dataclass(frozen=True)
class ExportOptions:
    scope_csv: bool = False
    n9020a_png: bool = False
    scope_png: bool = False
    pulse_width_summary: bool = False
    measurement_summary: bool = False
    html_report: bool = False
    overwrite: bool = False
    all_frequencies: bool = True

    def has_output(self) -> bool:
        return any(
            (
                self.scope_csv,
                self.n9020a_png,
                self.scope_png,
                self.pulse_width_summary,
                self.measurement_summary,
                self.html_report,
            )
        )


@dataclass(frozen=True)
class FrequencyDataset:
    path: Path
    directory_name: str
    frequency_mhz: float | None


@dataclass(frozen=True)
class SampleFiles:
    dataset: FrequencyDataset
    index: int
    stem: str
    csv_path: Path
    npz_path: Path
    delay_npz_path: Path
    cycles_npz_path: Path

    @property
    def csv_exists(self) -> bool:
        return self.csv_path.is_file()

    @property
    def npz_exists(self) -> bool:
        return self.npz_path.is_file()

    @property
    def delay_npz_exists(self) -> bool:
        return self.delay_npz_path.is_file()

    @property
    def cycles_npz_exists(self) -> bool:
        return self.cycles_npz_path.is_file()

    @property
    def is_dual_measurement(self) -> bool:
        return self.delay_npz_exists or self.cycles_npz_exists


@dataclass(frozen=True)
class MeasurementRecord:
    frequency_mhz: float | None
    frequency_name: str
    index: int
    measurement_s: float
    measurement_valid: bool
    attempts: int
    raw_value: str
    npz_file: str
    csv_file: str
    pair_status: str
    measurement_type: str = "PWID"

    @property
    def valid(self) -> bool:
        return self.measurement_valid and math.isfinite(self.measurement_s)

    @property
    def pulse_width_ns(self) -> float:
        """Legacy compatibility; never exposes DELAY as pulse width."""
        return self.measurement_ns if self.measurement_type == "PWID" else float("nan")

    @property
    def pulse_width_s(self) -> float:
        return self.measurement_s if self.measurement_type == "PWID" else float("nan")

    @property
    def pulse_width_valid(self) -> bool:
        return self.valid if self.measurement_type == "PWID" else False

    @property
    def measurement_ns(self) -> float:
        if not self.valid or self.measurement_type == "CYCLES":
            return float("nan")
        return self.measurement_s * 1e9

    @property
    def analysis_value(self) -> float:
        if not self.valid:
            return float("nan")
        return self.measurement_s if self.measurement_type == "CYCLES" else self.measurement_s * 1e9

    @property
    def measurement_label(self) -> str:
        if self.measurement_type in {"DELAY", "CYCLES"}:
            return self.measurement_type
        return "PWID (Legacy)"


# Public compatibility alias for integrations written before DELAY became authoritative.
PulseWidthRecord = MeasurementRecord


@dataclass(frozen=True)
class FrequencyStatistics:
    frequency_mhz: float | None
    frequency_name: str
    total_samples: int
    valid_samples: int
    invalid_samples: int
    valid_rate: float
    mean_ns: float
    median_ns: float
    std_ns: float
    min_ns: float
    max_ns: float
    p05_ns: float
    p95_ns: float
    missing_csv: int
    missing_npz: int


@dataclass
class ExportSummary:
    total: int
    success: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "success": self.success,
            "skipped": self.skipped,
            "failed": self.failed,
            "stopped": self.stopped,
            "errors": list(self.errors),
        }


def parse_frequency_directory(name: str) -> float | None:
    match = FREQUENCY_DIRECTORY_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def scan_frequency_datasets(
    data_root: str | Path,
    *,
    all_frequencies: bool = True,
) -> list[FrequencyDataset]:
    root = Path(data_root)
    if not root.is_dir():
        raise NotADirectoryError(f"Data directory does not exist: {root}")

    root_frequency = parse_frequency_directory(root.name)
    if not all_frequencies or root_frequency is not None:
        return [FrequencyDataset(root, root.name, root_frequency)]

    datasets = [
        FrequencyDataset(child, child.name, frequency)
        for child in root.iterdir()
        if child.is_dir()
        if (frequency := parse_frequency_directory(child.name)) is not None
    ]
    datasets.sort(key=lambda item: (float(item.frequency_mhz), item.directory_name.casefold()))
    if any(
        path.is_file()
        and path.suffix.casefold() in {".csv", ".npz"}
        and _sample_stem(path) is not None
        for path in root.iterdir()
    ):
        datasets.insert(0, FrequencyDataset(root, root.name, None))
    return datasets


def scan_samples(dataset: FrequencyDataset) -> list[SampleFiles]:
    stems: set[str] = set()
    for suffix in (".csv", ".npz"):
        for path in dataset.path.glob(f"*{suffix}"):
            stem = _sample_stem(path)
            if stem is not None:
                stems.add(stem)

    return [
        SampleFiles(
            dataset=dataset,
            index=int(stem),
            stem=stem,
            csv_path=dataset.path / f"{stem}.csv",
            npz_path=dataset.path / f"{stem}.npz",
            delay_npz_path=dataset.path / f"{stem}_delay.npz",
            cycles_npz_path=dataset.path / f"{stem}_cycles.npz",
        )
        for stem in sorted(stems, key=lambda item: (int(item), item))
    ]


def export_scope_npz_to_csv(
    npz_path: str | Path,
    output_path: str | Path,
    *,
    chunk_rows: int = 100_000,
) -> Path:
    source = Path(npz_path)
    output = Path(output_path)
    if chunk_rows <= 0:
        raise ValueError("chunk_rows must be positive")

    with np.load(source, allow_pickle=False) as data:
        missing = [key for key in ("time_s", "voltage_v", "adc") if key not in data]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")
        time_s = np.asarray(data["time_s"], dtype=np.float64).reshape(-1)
        voltage_v = np.asarray(data["voltage_v"], dtype=np.float32).reshape(-1)
        adc = np.asarray(data["adc"], dtype=np.int16).reshape(-1)
        if not (time_s.size == voltage_v.size == adc.size):
            raise ValueError("time_s, voltage_v and adc have different lengths")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output_file:
            output_file.write(",".join(SCOPE_CSV_HEADER) + "\n")
            for start in range(0, time_s.size, chunk_rows):
                end = min(start + chunk_rows, time_s.size)
                chunk = np.column_stack(
                    (
                        np.arange(start, end, dtype=np.int64),
                        time_s[start:end],
                        voltage_v[start:end],
                        adc[start:end],
                    )
                )
                np.savetxt(
                    output_file,
                    chunk,
                    delimiter=",",
                    fmt=("%d", "%.12e", "%.12e", "%d"),
                )
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output


def read_measurement_record(
    sample: SampleFiles,
    data_root: Path,
) -> tuple[PulseWidthRecord, str | None]:
    error: str | None = None
    pulse_width_s = float("nan")
    attempts = 0
    raw_value = ""
    pulse_width_valid = False
    if not sample.npz_exists:
        pair_status = "missing_npz"
    else:
        pair_status = "paired" if sample.csv_exists else "missing_csv"
        try:
            with np.load(sample.npz_path, allow_pickle=False) as data:
                is_delay = "delay_s" in data
                is_legacy_pwid = "positive_pulse_width_s" in data
            if is_delay:
                from delay_summary import read_npz_delay_record

                metadata = read_npz_delay_record(sample.npz_path, sample.csv_path)
                pulse_width_s = metadata.delay_s
                pulse_width_valid = metadata.valid
                attempts = metadata.attempts
                raw_value = metadata.raw_value
                measurement_type = "DELAY"
            elif is_legacy_pwid:
                metadata = read_npz_summary_record(sample.npz_path, sample.csv_path)
                pulse_width_s = metadata.pulse_width_s
                pulse_width_valid = metadata.valid
                attempts = metadata.attempts
                raw_value = metadata.raw_value
                measurement_type = "PWID"
            else:
                # Old waveform NPZ files predate measurement metadata entirely.
                # Keep them readable as unavailable legacy PWID, never as DELAY.
                metadata = read_npz_summary_record(sample.npz_path, sample.csv_path)
                pulse_width_s = metadata.pulse_width_s
                pulse_width_valid = False
                attempts = metadata.attempts
                raw_value = metadata.raw_value
                measurement_type = "PWID"
        except Exception as exc:
            pair_status = "invalid_npz"
            error = f"{type(exc).__name__}: {exc}"
            measurement_type = "UNKNOWN"

    if not sample.npz_exists:
        measurement_type = "UNKNOWN"

    return (
        MeasurementRecord(
            frequency_mhz=sample.dataset.frequency_mhz,
            frequency_name=sample.dataset.directory_name,
            index=sample.index,
            measurement_s=pulse_width_s,
            measurement_valid=pulse_width_valid,
            attempts=attempts,
            raw_value=raw_value,
            npz_file=_relative_path(sample.npz_path, data_root),
            csv_file=_relative_path(sample.csv_path, data_root),
            pair_status=pair_status,
            measurement_type=measurement_type,
        ),
        error,
    )


def read_pulse_width_record(
    sample: SampleFiles,
    data_root: Path,
) -> tuple[PulseWidthRecord, str | None]:
    """Backward-compatible reader that preserves explicit measurement type."""
    return read_measurement_record(sample, data_root)


def compute_frequency_statistics(records: list[PulseWidthRecord]) -> list[FrequencyStatistics]:
    groups: dict[tuple[float | None, str], list[PulseWidthRecord]] = {}
    for record in records:
        groups.setdefault((record.frequency_mhz, record.frequency_name), []).append(record)

    statistics: list[FrequencyStatistics] = []
    for (frequency_mhz, frequency_name), group in sorted(
        groups.items(),
        key=lambda item: (_frequency_sort_value(item[0][0]), item[0][1].casefold()),
    ):
        values = np.asarray([record.analysis_value for record in group if record.valid])
        valid_count = int(values.size)
        total_count = len(group)
        invalid_count = total_count - valid_count
        if valid_count:
            mean_ns = float(np.mean(values))
            median_ns = float(np.median(values))
            std_ns = float(np.std(values))
            min_ns = float(np.min(values))
            max_ns = float(np.max(values))
            p05_ns = float(np.percentile(values, 5))
            p95_ns = float(np.percentile(values, 95))
        else:
            mean_ns = median_ns = std_ns = min_ns = max_ns = p05_ns = p95_ns = float("nan")
        statistics.append(
            FrequencyStatistics(
                frequency_mhz=frequency_mhz,
                frequency_name=frequency_name,
                total_samples=total_count,
                valid_samples=valid_count,
                invalid_samples=invalid_count,
                valid_rate=valid_count / total_count if total_count else 0.0,
                mean_ns=mean_ns,
                median_ns=median_ns,
                std_ns=std_ns,
                min_ns=min_ns,
                max_ns=max_ns,
                p05_ns=p05_ns,
                p95_ns=p95_ns,
                missing_csv=sum(record.pair_status == "missing_csv" for record in group),
                missing_npz=sum(record.pair_status == "missing_npz" for record in group),
            )
        )
    return statistics


def write_frequency_summary(records: list[PulseWidthRecord], output_path: str | Path) -> Path:
    header = DELAY_SUMMARY_HEADER if _measurement_type(records) == "DELAY" else SUMMARY_HEADER
    return _write_csv_atomic(
        Path(output_path),
        header,
        (_summary_row(record) for record in sorted(records, key=lambda item: item.index)),
    )


def write_all_summary(records: list[PulseWidthRecord], output_path: str | Path) -> Path:
    ordered = sorted(
        records,
        key=lambda item: (_frequency_sort_value(item.frequency_mhz), item.frequency_name, item.index),
    )
    header = (
        DELAY_ALL_SUMMARY_HEADER
        if _measurement_type(records) == "DELAY"
        else ALL_SUMMARY_HEADER
    )
    return _write_csv_atomic(
        Path(output_path),
        header,
        (
            (_format_number(record.frequency_mhz),)
            + _summary_row(record)
            + (record.pair_status,)
            for record in ordered
        ),
    )


def write_by_frequency_summary(
    statistics: list[FrequencyStatistics],
    output_path: str | Path,
) -> Path:
    rows = (
        (
            _format_number(item.frequency_mhz),
            item.total_samples,
            item.valid_samples,
            item.invalid_samples,
            _format_number(item.valid_rate),
            _format_number(item.mean_ns),
            _format_number(item.median_ns),
            _format_number(item.std_ns),
            _format_number(item.min_ns),
            _format_number(item.max_ns),
            _format_number(item.p05_ns),
            _format_number(item.p95_ns),
            item.missing_csv,
            item.missing_npz,
        )
        for item in statistics
    )
    return _write_csv_atomic(Path(output_path), BY_FREQUENCY_HEADER, rows)


def generate_measurement_html_report(
    data_root: str | Path,
    records: list[PulseWidthRecord],
    statistics: list[FrequencyStatistics],
    output_path: str | Path,
) -> Path:
    import plotly.graph_objects as go
    from plotly.offline import get_plotlyjs

    root = Path(data_root)
    output = Path(output_path)
    valid_records = [record for record in records if record.valid]
    frequencies = [item.frequency_mhz for item in statistics if item.frequency_mhz is not None]
    total = len(records)
    valid = len(valid_records)
    invalid = total - valid
    measurement_type = _measurement_type(records)
    legacy = measurement_type == "PWID"
    metric_name = "Positive Pulse Width" if legacy else "Delay"
    measurement_display = "PWID (Legacy)" if legacy else "DELAY"

    histogram = go.Figure()
    frequency_groups = _records_by_frequency(records)
    group_names = list(frequency_groups)
    for position, name in enumerate(group_names):
        group = frequency_groups[name]
        histogram.add_trace(
            go.Histogram(
                x=[record.measurement_ns for record in group if record.valid],
                name=name,
                visible=position == 0,
            )
        )
    if group_names:
        histogram.update_layout(
            updatemenus=[
                {
                    "buttons": [
                        {
                            "label": name,
                            "method": "update",
                            "args": [
                                {"visible": [index == position for index in range(len(group_names))]},
                                {"title": f"{metric_name} Histogram — {name}"},
                            ],
                        }
                        for position, name in enumerate(group_names)
                    ],
                    "direction": "down",
                    "x": 1.0,
                    "xanchor": "right",
                    "y": 1.18,
                }
            ],
            title=f"{metric_name} Histogram — {group_names[0]}",
        )
    else:
        histogram.update_layout(title=f"{metric_name} Histogram — No valid samples")
    histogram.update_xaxes(title=f"{metric_name} (ns)")
    histogram.update_yaxes(title="Sample Count")

    scatter = go.Figure()
    for name, group in frequency_groups.items():
        valid_group = [record for record in group if record.valid]
        scatter.add_trace(
            go.Scatter(
                x=[record.index for record in valid_group],
                y=[record.measurement_ns for record in valid_group],
                mode="markers",
                name=name,
                customdata=[
                    [record.frequency_name, record.npz_file, record.csv_file]
                    for record in valid_group
                ],
                hovertemplate=(
                    "Frequency: %{customdata[0]}<br>"
                    "Sample Index: %{x}<br>"
                    f"{metric_name}: %{{y:.6g}} ns<br>"
                    "NPZ: %{customdata[1]}<br>"
                    "CSV: %{customdata[2]}<extra></extra>"
                ),
            )
        )
    scatter.update_layout(title=f"{metric_name} by Sample Index")
    scatter.update_xaxes(title="Sample Index")
    scatter.update_yaxes(title=f"{metric_name} (ns)")

    box = go.Figure()
    for name, group in frequency_groups.items():
        box.add_trace(
            go.Box(
                x=[name] * sum(record.valid for record in group),
                y=[record.measurement_ns for record in group if record.valid],
                name=name,
                boxpoints="outliers",
            )
        )
    box.update_layout(title=f"{metric_name} Distribution by Frequency", showlegend=False)
    box.update_xaxes(title="Frequency (MHz)")
    box.update_yaxes(title=f"{metric_name} (ns)")

    trend = go.Figure()
    trend_x = [
        item.frequency_mhz if item.frequency_mhz is not None else item.frequency_name
        for item in statistics
    ]
    trend.add_trace(
        go.Scatter(
            x=trend_x,
            y=[item.mean_ns for item in statistics],
            mode="lines+markers",
            name="Mean",
        )
    )
    trend.add_trace(
        go.Scatter(
            x=trend_x,
            y=[item.median_ns for item in statistics],
            mode="lines+markers",
            name="Median",
        )
    )
    trend.update_layout(title=f"Mean and Median {metric_name} Trend")
    trend.update_xaxes(title="Frequency (MHz)")
    trend.update_yaxes(title=f"{metric_name} (ns)")

    report_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{metric_name} Analysis Report</title>
<script>{get_plotlyjs()}</script>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2937; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }}
.card {{ padding: 14px; border: 1px solid #d1d5db; border-radius: 8px; background: #f9fafb; }}
.card strong {{ display: block; font-size: 1.35rem; margin-top: 6px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
th, td {{ padding: 7px; border: 1px solid #d1d5db; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
.plot {{ margin-top: 28px; }}
</style>
</head>
<body>
<h1>{metric_name} Analysis Report</h1>
<p>Data root: <code>{html.escape(str(root.resolve()))}</code></p>
<p><strong>Measurement Type: {measurement_display}</strong></p>
<div class="cards">
{_report_card("Frequency range", _frequency_range_text(frequencies))}
{_report_card("Frequency points", str(len(statistics)))}
{_report_card("Total samples", str(total))}
{_report_card("Valid samples", str(valid))}
{_report_card(f"Invalid {measurement_display}", f"{invalid} / {total}")}
{_report_card("Valid rate", f"{valid / total:.2%}" if total else "0.00%")}
</div>
<h2>Per-frequency statistics</h2>
{_statistics_html_table(statistics)}
<div class="plot">{histogram.to_html(full_html=False, include_plotlyjs=False)}</div>
<div class="plot">{scatter.to_html(full_html=False, include_plotlyjs=False)}</div>
<div class="plot">{box.to_html(full_html=False, include_plotlyjs=False)}</div>
<div class="plot">{trend.to_html(full_html=False, include_plotlyjs=False)}</div>
</body>
</html>
"""
    _write_text_atomic(output, report_html)
    return output


def generate_pulse_width_html_report(
    data_root: str | Path,
    records: list[PulseWidthRecord],
    statistics: list[FrequencyStatistics],
    output_path: str | Path,
) -> Path:
    """Compatibility wrapper; the report itself identifies DELAY versus legacy PWID."""
    return generate_measurement_html_report(data_root, records, statistics, output_path)


def generate_dual_measurement_html_report(
    data_root: str | Path,
    records: list[PulseWidthRecord],
    output_path: str | Path,
) -> Path:
    """Create one offline report with strictly separate DELAY and CYCLES analyses."""
    from plotly.offline import get_plotlyjs

    root = Path(data_root)
    output = Path(output_path)
    delay_records = [record for record in records if record.measurement_type == "DELAY"]
    cycles_records = [record for record in records if record.measurement_type == "CYCLES"]
    if not delay_records or not cycles_records:
        raise ValueError("A dual measurement report requires both DELAY and CYCLES records")
    delay_section = _dual_analysis_section(delay_records, "DELAY", "Delay", "ns")
    cycles_section = _dual_analysis_section(cycles_records, "CYCLES", "Cycles", "count")
    report_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DELAY + CYCLES Analysis Report</title>
<script>{get_plotlyjs()}</script>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; color: #1f2937; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }}
.card {{ padding: 14px; border: 1px solid #d1d5db; border-radius: 8px; background: #f9fafb; }}
.card strong {{ display: block; font-size: 1.25rem; margin-top: 6px; }}
section {{ margin-top: 36px; padding-top: 12px; border-top: 3px solid #334155; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
th, td {{ padding: 7px; border: 1px solid #d1d5db; text-align: right; }}
th:first-child, td:first-child {{ text-align: left; }}
.plot {{ margin-top: 24px; }}
</style>
</head>
<body>
<h1>Advanced Measurement Analysis Report</h1>
<p>Data root: <code>{html.escape(str(root.resolve()))}</code></p>
{delay_section}
{cycles_section}
</body>
</html>
"""
    _write_text_atomic(output, report_html)
    return output


def _dual_analysis_section(
    records: list[PulseWidthRecord],
    measurement_type: str,
    metric_name: str,
    unit: str,
) -> str:
    import plotly.graph_objects as go

    statistics = compute_frequency_statistics(records)
    groups = _records_by_frequency(records)
    values = [record.analysis_value for record in records if record.valid]
    total = len(records)
    valid = len(values)

    histogram = go.Figure()
    scatter = go.Figure()
    box = go.Figure()
    for name, group in groups.items():
        valid_group = [record for record in group if record.valid]
        histogram.add_trace(
            go.Histogram(x=[record.analysis_value for record in valid_group], name=name)
        )
        scatter.add_trace(
            go.Scatter(
                x=[record.index for record in valid_group],
                y=[record.analysis_value for record in valid_group],
                mode="markers",
                name=name,
            )
        )
        box.add_trace(
            go.Box(
                x=[name] * len(valid_group),
                y=[record.analysis_value for record in valid_group],
                name=name,
                boxpoints="outliers",
            )
        )
    histogram.update_layout(title=f"{measurement_type} Histogram")
    histogram.update_xaxes(title=f"{metric_name} ({unit})")
    scatter.update_layout(title=f"Sample Index vs {measurement_type}")
    scatter.update_xaxes(title="Sample Index")
    scatter.update_yaxes(title=f"{metric_name} ({unit})")
    box.update_layout(title=f"{measurement_type} Frequency Box Plot", showlegend=False)
    box.update_yaxes(title=f"{metric_name} ({unit})")

    trend = go.Figure()
    trend_x = [
        item.frequency_mhz if item.frequency_mhz is not None else item.frequency_name
        for item in statistics
    ]
    trend.add_trace(
        go.Scatter(x=trend_x, y=[item.mean_ns for item in statistics], mode="lines+markers", name="Mean")
    )
    trend.add_trace(
        go.Scatter(x=trend_x, y=[item.median_ns for item in statistics], mode="lines+markers", name="Median")
    )
    trend.update_layout(title=f"Frequency vs Mean/Median {measurement_type}")
    trend.update_xaxes(title="Frequency (MHz)")
    trend.update_yaxes(title=f"{metric_name} ({unit})")

    aggregate = np.asarray(values, dtype=np.float64)
    def aggregate_text(function) -> str:
        return _format_number(float(function(aggregate))) if valid else "nan"

    cards = (
        _report_card("Total", str(total))
        + _report_card("Valid", str(valid))
        + _report_card("Invalid", str(total - valid))
        + _report_card("Valid Rate", f"{valid / total:.2%}" if total else "0.00%")
        + _report_card(f"Mean ({unit})", aggregate_text(np.mean))
        + _report_card(f"Median ({unit})", aggregate_text(np.median))
        + _report_card(f"Std ({unit})", aggregate_text(np.std))
        + _report_card(f"Min ({unit})", aggregate_text(np.min))
        + _report_card(f"Max ({unit})", aggregate_text(np.max))
        + _report_card(f"P05 ({unit})", aggregate_text(lambda values: np.percentile(values, 5)))
        + _report_card(f"P95 ({unit})", aggregate_text(lambda values: np.percentile(values, 95)))
    )
    return (
        f"<section><h2>{measurement_type} Analysis</h2>"
        f"<div class=\"cards\">{cards}</div>"
        + _statistics_html_table(statistics, unit)
        + f'<div class="plot">{histogram.to_html(full_html=False, include_plotlyjs=False)}</div>'
        + f'<div class="plot">{scatter.to_html(full_html=False, include_plotlyjs=False)}</div>'
        + f'<div class="plot">{box.to_html(full_html=False, include_plotlyjs=False)}</div>'
        + f'<div class="plot">{trend.to_html(full_html=False, include_plotlyjs=False)}</div></section>'
    )


def run_data_export(
    data_root: str | Path,
    output_root: str | Path,
    options: ExportOptions,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_start: Callable[[int, int, str], None] | None = None,
    on_result: Callable[[int, int, str, str, str], None] | None = None,
) -> ExportSummary:
    if not options.has_output():
        raise ValueError("Select at least one export or analysis output")
    data_root_path = Path(data_root)
    output_root_path = Path(output_root)
    if output_root_path.resolve() == data_root_path.resolve():
        raise ValueError("Output directory must be separate from the raw data directory")

    datasets = scan_frequency_datasets(
        data_root_path,
        all_frequencies=options.all_frequencies,
    )
    if not datasets:
        raise ValueError("No frequency directories were found")
    samples = [sample for dataset in datasets for sample in scan_samples(dataset)]
    if not samples:
        raise ValueError("No numeric CSV or NPZ sample files were found")

    file_jobs: list[tuple[str, Path, Path, Callable[[Path, Path], Path]]] = []
    for sample in samples:
        relative_folder = Path(sample.dataset.directory_name)
        scope_sources: list[tuple[Path, str, str]] = []
        if sample.npz_exists:
            scope_sources.append(
                (sample.npz_path, f"{sample.stem}_scope.csv", f"{sample.stem}_npz.png")
            )
        if sample.delay_npz_exists:
            scope_sources.append(
                (
                    sample.delay_npz_path,
                    f"{sample.stem}_delay_scope.csv",
                    f"{sample.stem}_delay.png",
                )
            )
        if sample.cycles_npz_exists:
            scope_sources.append(
                (
                    sample.cycles_npz_path,
                    f"{sample.stem}_cycles_scope.csv",
                    f"{sample.stem}_cycles.png",
                )
            )
        if options.scope_csv:
            for source, csv_name, _png_name in scope_sources:
                file_jobs.append(
                    (
                        f"Scope CSV: {sample.dataset.directory_name}/{source.name}",
                        source,
                        output_root_path / "scope_csv" / relative_folder / csv_name,
                        export_scope_npz_to_csv,
                    )
                )
        if options.n9020a_png and sample.csv_exists:
            file_jobs.append(
                (
                    f"N9020A PNG: {sample.dataset.directory_name}/{sample.csv_path.name}",
                    sample.csv_path,
                    output_root_path / "images" / relative_folder / f"{sample.stem}_csv.png",
                    convert_csv_to_png,
                )
            )
        if options.scope_png:
            for source, _csv_name, png_name in scope_sources:
                file_jobs.append(
                    (
                        f"Scope PNG: {sample.dataset.directory_name}/{source.name}",
                        source,
                        output_root_path / "images" / relative_folder / png_name,
                        convert_npz_to_png,
                    )
                )

    summary_requested = options.measurement_summary or options.pulse_width_summary
    needs_records = summary_requested or options.html_report
    metadata_jobs = len(datasets) if needs_records else 0
    summary_jobs = 2 if summary_requested else 0
    report_jobs = 1 if options.html_report else 0
    total_jobs = len(file_jobs) + metadata_jobs + summary_jobs + report_jobs
    summary = ExportSummary(total=total_jobs)
    position = 0

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    def report_result(label: str, status: str, message: str = "") -> None:
        nonlocal position
        position += 1
        if status == "success":
            summary.success += 1
        elif status == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1
            summary.errors.append(f"{label}: {message}")
        if on_result is not None:
            on_result(position, total_jobs, label, status, message)

    for label, source, output, action in file_jobs:
        if stopped():
            summary.stopped = True
            return summary
        if on_start is not None:
            on_start(position + 1, total_jobs, label)
        if output.exists() and not options.overwrite:
            report_result(label, "skipped")
            continue
        try:
            action(source, output)
        except Exception as exc:
            report_result(label, "failed", f"{type(exc).__name__}: {exc}")
        else:
            report_result(label, "success")

    records: list[PulseWidthRecord] = []
    if needs_records:
        for dataset in datasets:
            if stopped():
                summary.stopped = True
                return summary
            rebuild = summary_requested
            measurement_type = _dataset_measurement_type(dataset)
            if measurement_type == "DUAL":
                summary_filename = MEASUREMENT_SUMMARY_FILENAME
                metric_label = "DELAY + CYCLES"
            elif measurement_type == "DELAY":
                summary_filename = DELAY_SUMMARY_FILENAME
                metric_label = "DELAY"
            elif measurement_type == "PWID":
                summary_filename = PULSE_WIDTH_SUMMARY_FILENAME
                metric_label = "PWID (Legacy)"
            else:
                summary_filename = DELAY_SUMMARY_FILENAME
                metric_label = "DELAY"
            label = (
                f"Rebuild {metric_label} summary: {dataset.directory_name}"
                if rebuild
                else f"Load {metric_label} summary: {dataset.directory_name}"
            )
            if on_start is not None:
                on_start(position + 1, total_jobs, label)
            try:
                summary_path = dataset.path / summary_filename
                if measurement_type == "UNKNOWN":
                    raise ValueError(
                        "No DELAY or legacy PWID measurement metadata was found"
                    )
                if rebuild or not summary_path.is_file():
                    if measurement_type == "DUAL":
                        rebuild_measurement_summary(dataset.path)
                    elif measurement_type == "DELAY":
                        rebuild_delay_summary(dataset.path)
                    else:
                        rebuild_pulse_width_summary(dataset.path)
                if measurement_type == "DUAL":
                    records.extend(
                        _records_from_measurement_summary(
                            dataset,
                            data_root_path,
                            summary_path,
                        )
                    )
                else:
                    records.extend(
                        _records_from_summary(
                            dataset,
                            data_root_path,
                            summary_path,
                            measurement_type,
                        )
                    )
            except Exception as exc:
                report_result(label, "failed", f"{type(exc).__name__}: {exc}")
            else:
                report_result(label, "success")

    record_types = {
        record.measurement_type for record in records if record.measurement_type != "UNKNOWN"
    }
    dual_measurement = record_types == {"DELAY", "CYCLES"}
    measurement_type = "DUAL" if dual_measurement else _measurement_type(records)
    statistics = (
        [] if dual_measurement else compute_frequency_statistics(records)
    )
    if summary_requested:
        summary_outputs: list[tuple[str, Path, Callable[[], Path]]] = []
        if measurement_type == "DUAL":
            file_prefix = "measurement"
            display_label = "DELAY + CYCLES"
        else:
            file_prefix = "delay" if measurement_type == "DELAY" else "pulse_width"
            display_label = "DELAY" if measurement_type == "DELAY" else "PWID (Legacy)"
        all_path = output_root_path / "summary" / f"{file_prefix}_all.csv"
        by_frequency_path = output_root_path / "summary" / f"{file_prefix}_by_frequency.csv"
        if measurement_type == "DUAL":
            summary_outputs.extend(
                (
                    (
                        f"{display_label} summary: all",
                        all_path,
                        lambda: _write_dual_all_summary(records, all_path),
                    ),
                    (
                        f"{display_label} summary: by frequency",
                        by_frequency_path,
                        lambda: _write_dual_by_frequency_summary(records, by_frequency_path),
                    ),
                )
            )
        else:
            summary_outputs.extend(
                (
                    (
                        f"{display_label} summary: all",
                        all_path,
                        lambda: write_all_summary(records, all_path),
                    ),
                    (
                        f"{display_label} summary: by frequency",
                        by_frequency_path,
                        lambda: write_by_frequency_summary(statistics, by_frequency_path),
                    ),
                )
            )
        for label, destination, action in summary_outputs:
            if stopped():
                summary.stopped = True
                return summary
            if on_start is not None:
                on_start(position + 1, total_jobs, label)
            if destination.exists() and not options.overwrite:
                report_result(label, "skipped")
                continue
            try:
                action()
            except Exception as exc:
                report_result(label, "failed", f"{type(exc).__name__}: {exc}")
            else:
                report_result(label, "success")

    if options.html_report:
        legacy = measurement_type == "PWID"
        if measurement_type == "DUAL":
            label = "Offline HTML DELAY + CYCLES report"
            filename = "measurement_report.html"
        else:
            label = "Offline HTML PWID (Legacy) report" if legacy else "Offline HTML DELAY report"
            filename = "pulse_width_report.html" if legacy else "delay_report.html"
        destination = output_root_path / "reports" / filename
        if stopped():
            summary.stopped = True
            return summary
        if on_start is not None:
            on_start(position + 1, total_jobs, label)
        if destination.exists() and not options.overwrite:
            report_result(label, "skipped")
        else:
            try:
                if measurement_type == "DUAL":
                    generate_dual_measurement_html_report(
                        data_root_path,
                        records,
                        destination,
                    )
                else:
                    generate_measurement_html_report(
                        data_root_path,
                        records,
                        statistics,
                        destination,
                    )
            except Exception as exc:
                report_result(label, "failed", f"{type(exc).__name__}: {exc}")
            else:
                report_result(label, "success")
    return summary


def _records_from_summary(
    dataset: FrequencyDataset,
    data_root: Path,
    summary_path: Path,
    measurement_type: str,
) -> list[PulseWidthRecord]:
    records: list[PulseWidthRecord] = []
    if measurement_type == "DELAY":
        summary_items = load_delay_summary(summary_path)
    else:
        summary_items = load_pulse_width_summary(summary_path)
    completed_items = [
        item
        for item in summary_items
        if (dataset.path / item.npz_file).is_file()
        and (dataset.path / item.csv_file).is_file()
    ]
    if len(completed_items) != len(summary_items):
        if measurement_type == "DELAY":
            write_delay_summary(summary_path, completed_items)
        else:
            write_pulse_width_summary(summary_path, completed_items)

    for item in completed_items:
        npz_path = dataset.path / item.npz_file
        csv_path = dataset.path / item.csv_file
        records.append(
            MeasurementRecord(
                frequency_mhz=dataset.frequency_mhz,
                frequency_name=dataset.directory_name,
                index=item.index,
                measurement_s=(
                    item.delay_s if measurement_type == "DELAY" else item.pulse_width_s
                ),
                measurement_valid=item.valid,
                attempts=item.attempts,
                raw_value=item.raw_value,
                npz_file=_relative_path(npz_path, data_root),
                csv_file=_relative_path(csv_path, data_root),
                pair_status="paired",
                measurement_type=measurement_type,
            )
        )
    return records


def _records_from_measurement_summary(
    dataset: FrequencyDataset,
    data_root: Path,
    summary_path: Path,
) -> list[PulseWidthRecord]:
    records: list[PulseWidthRecord] = []
    items = load_measurement_summary(summary_path)
    completed = [
        item
        for item in items
        if all(
            (dataset.path / name).is_file()
            for name in (item.csv_file, item.delay_npz_file, item.cycles_npz_file)
        )
    ]
    for item in completed:
        csv_file = _relative_path(dataset.path / item.csv_file, data_root)
        records.extend(
            (
                MeasurementRecord(
                    frequency_mhz=dataset.frequency_mhz,
                    frequency_name=dataset.directory_name,
                    index=item.index,
                    measurement_s=item.delay_s,
                    measurement_valid=item.delay_valid,
                    attempts=item.delay_attempts,
                    raw_value=item.delay_raw,
                    npz_file=_relative_path(dataset.path / item.delay_npz_file, data_root),
                    csv_file=csv_file,
                    pair_status="paired",
                    measurement_type="DELAY",
                ),
                MeasurementRecord(
                    frequency_mhz=dataset.frequency_mhz,
                    frequency_name=dataset.directory_name,
                    index=item.index,
                    measurement_s=item.cycles_count,
                    measurement_valid=item.cycles_valid,
                    attempts=item.cycles_attempts,
                    raw_value=item.cycles_raw,
                    npz_file=_relative_path(dataset.path / item.cycles_npz_file, data_root),
                    csv_file=csv_file,
                    pair_status="paired",
                    measurement_type="CYCLES",
                ),
            )
        )
    return records


def _write_dual_all_summary(records: list[PulseWidthRecord], output_path: Path) -> Path:
    header = (
        "frequency_mhz",
        "frequency_name",
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
    grouped: dict[tuple[str, int], dict[str, PulseWidthRecord]] = {}
    for record in records:
        grouped.setdefault((record.frequency_name, record.index), {})[
            record.measurement_type
        ] = record
    rows = []
    for (frequency_name, index), pair in sorted(
        grouped.items(),
        key=lambda item: (
            _frequency_sort_value(next(iter(item[1].values())).frequency_mhz),
            item[0][0],
            item[0][1],
        ),
    ):
        delay = pair.get("DELAY")
        cycles = pair.get("CYCLES")
        if delay is None or cycles is None:
            continue
        rows.append(
            (
                _format_number(delay.frequency_mhz),
                frequency_name,
                index,
                _format_number(delay.measurement_s),
                _format_number(delay.measurement_ns),
                int(delay.valid),
                delay.attempts,
                delay.raw_value,
                _format_number(cycles.measurement_s),
                int(cycles.valid),
                cycles.attempts,
                cycles.raw_value,
                delay.npz_file,
                cycles.npz_file,
                delay.csv_file,
            )
        )
    return _write_csv_atomic(output_path, header, rows)


def _write_dual_by_frequency_summary(
    records: list[PulseWidthRecord],
    output_path: Path,
) -> Path:
    header = (
        "frequency_mhz",
        "frequency_name",
        "delay_total",
        "delay_valid",
        "delay_mean_ns",
        "delay_median_ns",
        "cycles_total",
        "cycles_valid",
        "cycles_mean_count",
        "cycles_median_count",
    )
    delay_stats = {
        item.frequency_name: item
        for item in compute_frequency_statistics(
            [record for record in records if record.measurement_type == "DELAY"]
        )
    }
    cycles_stats = {
        item.frequency_name: item
        for item in compute_frequency_statistics(
            [record for record in records if record.measurement_type == "CYCLES"]
        )
    }
    rows = []
    for name in sorted(
        set(delay_stats) | set(cycles_stats),
        key=lambda item: _frequency_sort_value(
            (delay_stats.get(item) or cycles_stats[item]).frequency_mhz
        ),
    ):
        delay = delay_stats.get(name)
        cycles = cycles_stats.get(name)
        exemplar = delay or cycles
        rows.append(
            (
                _format_number(exemplar.frequency_mhz if exemplar else None),
                name,
                delay.total_samples if delay else 0,
                delay.valid_samples if delay else 0,
                _format_number(delay.mean_ns if delay else float("nan")),
                _format_number(delay.median_ns if delay else float("nan")),
                cycles.total_samples if cycles else 0,
                cycles.valid_samples if cycles else 0,
                _format_number(cycles.mean_ns if cycles else float("nan")),
                _format_number(cycles.median_ns if cycles else float("nan")),
            )
        )
    return _write_csv_atomic(output_path, header, rows)


def _summary_row(record: PulseWidthRecord) -> tuple:
    return (
        record.index,
        _format_number(record.measurement_s),
        _format_number(record.measurement_ns),
        int(record.valid),
        record.attempts,
        record.raw_value,
        record.npz_file,
        record.csv_file,
    )


def _dataset_measurement_type(dataset: FrequencyDataset) -> str:
    """Prefer a DELAY summary and otherwise identify legacy NPZ metadata explicitly."""
    if (dataset.path / MEASUREMENT_SUMMARY_FILENAME).is_file() or any(
        dataset.path.glob("*_delay.npz")
    ):
        return "DUAL"
    if (dataset.path / DELAY_SUMMARY_FILENAME).is_file():
        return "DELAY"
    if (dataset.path / PULSE_WIDTH_SUMMARY_FILENAME).is_file():
        return "PWID"
    found_delay = False
    found_pwid = False
    for npz_path in dataset.path.glob("*.npz"):
        if not npz_path.stem.isdigit():
            continue
        try:
            with np.load(npz_path, allow_pickle=False) as data:
                found_delay = found_delay or "delay_s" in data
                found_pwid = found_pwid or "positive_pulse_width_s" in data
        except Exception:
            continue
    if found_delay:
        return "DELAY"
    if found_pwid:
        return "PWID"
    return "UNKNOWN"


def _measurement_type(records: list[PulseWidthRecord]) -> str:
    types = {record.measurement_type for record in records if record.measurement_type != "UNKNOWN"}
    if len(types) > 1:
        raise ValueError(
            "DELAY and legacy PWID datasets cannot be mixed in one analysis report"
        )
    return next(iter(types), "DELAY")


def _sample_stem(path: Path) -> str | None:
    stem = path.stem
    if stem.isdigit():
        return stem
    for suffix in ("_delay", "_cycles"):
        if stem.endswith(suffix):
            candidate = stem.removesuffix(suffix)
            return candidate if candidate.isdigit() else None
    return None


def _write_csv_atomic(path: Path, header: tuple[str, ...], rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as output_file:
            writer = csv.writer(output_file, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _format_number(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return "nan"
    return f"{float(value):.12g}"


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _frequency_sort_value(frequency: float | None) -> float:
    return float(frequency) if frequency is not None else float("inf")


def _records_by_frequency(records: list[PulseWidthRecord]) -> dict[str, list[PulseWidthRecord]]:
    groups: dict[str, list[PulseWidthRecord]] = {}
    for record in sorted(
        records,
        key=lambda item: (_frequency_sort_value(item.frequency_mhz), item.frequency_name, item.index),
    ):
        groups.setdefault(record.frequency_name, []).append(record)
    return groups


def _report_card(label: str, value: str) -> str:
    return f'<div class="card">{html.escape(label)}<strong>{html.escape(value)}</strong></div>'


def _frequency_range_text(frequencies: list[float]) -> str:
    if not frequencies:
        return "N/A"
    return f"{min(frequencies):g}–{max(frequencies):g} MHz"


def _statistics_html_table(
    statistics: list[FrequencyStatistics],
    unit_label: str = "ns",
) -> str:
    columns = (
        "Frequency",
        "Total",
        "Valid",
        "Invalid",
        "Valid rate",
        f"Mean {unit_label}",
        f"Median {unit_label}",
        f"Std {unit_label}",
        f"Min {unit_label}",
        f"Max {unit_label}",
        f"P05 {unit_label}",
        f"P95 {unit_label}",
    )
    rows = []
    for item in statistics:
        frequency = (
            f"{item.frequency_mhz:g} MHz"
            if item.frequency_mhz is not None
            else item.frequency_name
        )
        values = (
            frequency,
            str(item.total_samples),
            str(item.valid_samples),
            str(item.invalid_samples),
            f"{item.valid_rate:.2%}",
            _format_number(item.mean_ns),
            _format_number(item.median_ns),
            _format_number(item.std_ns),
            _format_number(item.min_ns),
            _format_number(item.max_ns),
            _format_number(item.p05_ns),
            _format_number(item.p95_ns),
        )
        rows.append("<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in values) + "</tr>")
    header = "<tr>" + "".join(f"<th>{html.escape(column)}</th>" for column in columns) + "</tr>"
    return "<table><thead>" + header + "</thead><tbody>" + "".join(rows) + "</tbody></table>"

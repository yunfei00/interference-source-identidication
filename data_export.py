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


FREQUENCY_DIRECTORY_PATTERN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*MHz\s*$",
    re.IGNORECASE,
)
SCOPE_CSV_HEADER = ("index", "time_s", "voltage_v", "adc")
SUMMARY_HEADER = (
    "index",
    "pulse_width_s",
    "pulse_width_ns",
    "valid",
    "npz_file",
    "csv_file",
    "pair_status",
)
ALL_SUMMARY_HEADER = ("frequency_mhz",) + SUMMARY_HEADER
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

    @property
    def csv_exists(self) -> bool:
        return self.csv_path.is_file()

    @property
    def npz_exists(self) -> bool:
        return self.npz_path.is_file()


@dataclass(frozen=True)
class PulseWidthRecord:
    frequency_mhz: float | None
    frequency_name: str
    index: int
    pulse_width_s: float
    npz_file: str
    csv_file: str
    pair_status: str

    @property
    def valid(self) -> bool:
        return math.isfinite(self.pulse_width_s)

    @property
    def pulse_width_ns(self) -> float:
        return self.pulse_width_s * 1e9 if self.valid else float("nan")


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
    return datasets


def scan_samples(dataset: FrequencyDataset) -> list[SampleFiles]:
    stems: set[str] = set()
    for suffix in (".csv", ".npz"):
        for path in dataset.path.glob(f"*{suffix}"):
            if path.stem.isdigit():
                stems.add(path.stem)

    return [
        SampleFiles(
            dataset=dataset,
            index=int(stem),
            stem=stem,
            csv_path=dataset.path / f"{stem}.csv",
            npz_path=dataset.path / f"{stem}.npz",
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


def read_pulse_width_record(sample: SampleFiles, data_root: Path) -> tuple[PulseWidthRecord, str | None]:
    error: str | None = None
    pulse_width_s = float("nan")
    if not sample.npz_exists:
        pair_status = "missing_npz"
    else:
        pair_status = "paired" if sample.csv_exists else "missing_csv"
        try:
            with np.load(sample.npz_path, allow_pickle=False) as data:
                if "positive_pulse_width_s" in data:
                    pulse_width_s = float(np.asarray(data["positive_pulse_width_s"]).reshape(-1)[0])
                    if not math.isfinite(pulse_width_s):
                        pulse_width_s = float("nan")
        except Exception as exc:
            pair_status = "invalid_npz"
            error = f"{type(exc).__name__}: {exc}"

    return (
        PulseWidthRecord(
            frequency_mhz=sample.dataset.frequency_mhz,
            frequency_name=sample.dataset.directory_name,
            index=sample.index,
            pulse_width_s=pulse_width_s,
            npz_file=_relative_path(sample.npz_path, data_root),
            csv_file=_relative_path(sample.csv_path, data_root),
            pair_status=pair_status,
        ),
        error,
    )


def compute_frequency_statistics(records: list[PulseWidthRecord]) -> list[FrequencyStatistics]:
    groups: dict[tuple[float | None, str], list[PulseWidthRecord]] = {}
    for record in records:
        groups.setdefault((record.frequency_mhz, record.frequency_name), []).append(record)

    statistics: list[FrequencyStatistics] = []
    for (frequency_mhz, frequency_name), group in sorted(
        groups.items(),
        key=lambda item: (_frequency_sort_value(item[0][0]), item[0][1].casefold()),
    ):
        values = np.asarray([record.pulse_width_ns for record in group if record.valid])
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
    return _write_csv_atomic(
        Path(output_path),
        SUMMARY_HEADER,
        (_summary_row(record) for record in sorted(records, key=lambda item: item.index)),
    )


def write_all_summary(records: list[PulseWidthRecord], output_path: str | Path) -> Path:
    ordered = sorted(
        records,
        key=lambda item: (_frequency_sort_value(item.frequency_mhz), item.frequency_name, item.index),
    )
    return _write_csv_atomic(
        Path(output_path),
        ALL_SUMMARY_HEADER,
        ((_format_number(record.frequency_mhz),) + _summary_row(record) for record in ordered),
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


def generate_pulse_width_html_report(
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

    histogram = go.Figure()
    frequency_groups = _records_by_frequency(records)
    group_names = list(frequency_groups)
    for position, name in enumerate(group_names):
        group = frequency_groups[name]
        histogram.add_trace(
            go.Histogram(
                x=[record.pulse_width_ns for record in group if record.valid],
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
                                {"title": f"Positive Pulse Width Histogram — {name}"},
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
            title=f"Positive Pulse Width Histogram — {group_names[0]}",
        )
    else:
        histogram.update_layout(title="Positive Pulse Width Histogram — No valid samples")
    histogram.update_xaxes(title="Positive Pulse Width (ns)")
    histogram.update_yaxes(title="Sample Count")

    scatter = go.Figure()
    for name, group in frequency_groups.items():
        valid_group = [record for record in group if record.valid]
        scatter.add_trace(
            go.Scatter(
                x=[record.index for record in valid_group],
                y=[record.pulse_width_ns for record in valid_group],
                mode="markers",
                name=name,
                customdata=[
                    [record.frequency_name, record.npz_file, record.csv_file]
                    for record in valid_group
                ],
                hovertemplate=(
                    "Frequency: %{customdata[0]}<br>"
                    "Sample Index: %{x}<br>"
                    "Pulse Width: %{y:.6g} ns<br>"
                    "NPZ: %{customdata[1]}<br>"
                    "CSV: %{customdata[2]}<extra></extra>"
                ),
            )
        )
    scatter.update_layout(title="Pulse Width by Sample Index")
    scatter.update_xaxes(title="Sample Index")
    scatter.update_yaxes(title="Pulse Width (ns)")

    box = go.Figure()
    for name, group in frequency_groups.items():
        box.add_trace(
            go.Box(
                x=[name] * sum(record.valid for record in group),
                y=[record.pulse_width_ns for record in group if record.valid],
                name=name,
                boxpoints="outliers",
            )
        )
    box.update_layout(title="Pulse Width Distribution by Frequency", showlegend=False)
    box.update_xaxes(title="Frequency (MHz)")
    box.update_yaxes(title="Positive Pulse Width (ns)")

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
    trend.update_layout(title="Mean and Median Pulse Width Trend")
    trend.update_xaxes(title="Frequency (MHz)")
    trend.update_yaxes(title="Positive Pulse Width (ns)")

    report_html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Positive Pulse Width Analysis Report</title>
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
<h1>Positive Pulse Width Analysis Report</h1>
<p>Data root: <code>{html.escape(str(root.resolve()))}</code></p>
<div class="cards">
{_report_card("Frequency range", _frequency_range_text(frequencies))}
{_report_card("Frequency points", str(len(statistics)))}
{_report_card("Total samples", str(total))}
{_report_card("Valid samples", str(valid))}
{_report_card("Invalid PWID", f"{invalid} / {total}")}
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
        if options.scope_csv and sample.npz_exists:
            file_jobs.append(
                (
                    f"Scope CSV: {sample.dataset.directory_name}/{sample.npz_path.name}",
                    sample.npz_path,
                    output_root_path / "scope_csv" / relative_folder / f"{sample.stem}_scope.csv",
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
        if options.scope_png and sample.npz_exists:
            file_jobs.append(
                (
                    f"Scope PNG: {sample.dataset.directory_name}/{sample.npz_path.name}",
                    sample.npz_path,
                    output_root_path / "images" / relative_folder / f"{sample.stem}_npz.png",
                    convert_npz_to_png,
                )
            )

    needs_records = options.pulse_width_summary or options.html_report
    metadata_jobs = len(samples) if needs_records else 0
    summary_jobs = len(datasets) + 2 if options.pulse_width_summary else 0
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
        for sample in samples:
            if stopped():
                summary.stopped = True
                return summary
            label = f"PWID: {sample.dataset.directory_name}/{sample.stem}"
            if on_start is not None:
                on_start(position + 1, total_jobs, label)
            record, error = read_pulse_width_record(sample, data_root_path)
            records.append(record)
            report_result(label, "failed" if error else "success", error or "")

    statistics = compute_frequency_statistics(records)
    if options.pulse_width_summary:
        by_frequency_records = {
            dataset.directory_name: [
                record for record in records if record.frequency_name == dataset.directory_name
            ]
            for dataset in datasets
        }
        summary_outputs: list[tuple[str, Path, Callable[[], Path]]] = []
        for dataset in datasets:
            destination = (
                output_root_path
                / "summary"
                / dataset.directory_name
                / "pulse_width_summary.csv"
            )
            summary_outputs.append(
                (
                    f"PWID summary: {dataset.directory_name}",
                    destination,
                    lambda destination=destination, dataset=dataset: write_frequency_summary(
                        by_frequency_records[dataset.directory_name], destination
                    ),
                )
            )
        all_path = output_root_path / "summary" / "pulse_width_all.csv"
        by_frequency_path = output_root_path / "summary" / "pulse_width_by_frequency.csv"
        summary_outputs.extend(
            (
                ("PWID summary: all", all_path, lambda: write_all_summary(records, all_path)),
                (
                    "PWID summary: by frequency",
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
        label = "Offline HTML pulse width report"
        destination = output_root_path / "reports" / "pulse_width_report.html"
        if stopped():
            summary.stopped = True
            return summary
        if on_start is not None:
            on_start(position + 1, total_jobs, label)
        if destination.exists() and not options.overwrite:
            report_result(label, "skipped")
        else:
            try:
                generate_pulse_width_html_report(
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


def _summary_row(record: PulseWidthRecord) -> tuple:
    return (
        record.index,
        _format_number(record.pulse_width_s),
        _format_number(record.pulse_width_ns),
        int(record.valid),
        record.npz_file,
        record.csv_file,
        record.pair_status,
    )


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


def _statistics_html_table(statistics: list[FrequencyStatistics]) -> str:
    columns = (
        "Frequency",
        "Total",
        "Valid",
        "Invalid",
        "Valid rate",
        "Mean ns",
        "Median ns",
        "Std ns",
        "Min ns",
        "Max ns",
        "P05 ns",
        "P95 ns",
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

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from time_formatting import format_time_value


MAX_PLOT_POINTS = 100_000
SUPPORTED_SUFFIXES = {".csv", ".npz"}


@dataclass(frozen=True)
class NPZPlotData:
    time_s: np.ndarray
    voltage_v: np.ndarray
    measurement_s: float
    measurement_type: str
    channel: str
    sample_rate: float
    point_count: int

    @property
    def delay_s(self) -> float:
        return self.measurement_s if self.measurement_type == "DELAY" else float("nan")

    @property
    def positive_pulse_width_s(self) -> float:
        """Legacy compatibility; never maps a DELAY value to PWID."""
        return self.measurement_s if self.measurement_type == "PWID" else float("nan")


@dataclass(frozen=True)
class CSVPlotData:
    x: np.ndarray
    y: np.ndarray
    title: str
    x_label: str
    y_label: str


@dataclass(frozen=True)
class ConversionTask:
    source: Path
    output: Path


@dataclass
class ConversionSummary:
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


def downsample_min_max(
    x: np.ndarray,
    y: np.ndarray,
    max_points: int = MAX_PLOT_POINTS,
) -> tuple[np.ndarray, np.ndarray]:
    """Build a min/max envelope with no more than ``max_points`` points."""
    x_array = np.asarray(x).reshape(-1)
    y_array = np.asarray(y).reshape(-1)
    if x_array.size != y_array.size:
        raise ValueError("X and Y arrays must have the same length")
    if x_array.size == 0:
        raise ValueError("Cannot plot an empty data series")
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    if x_array.size <= max_points:
        return x_array, y_array

    bin_count = max(1, max_points // 2)
    boundaries = np.linspace(0, x_array.size, bin_count + 1, dtype=np.int64)
    starts = boundaries[:-1]
    ends = boundaries[1:]
    centers = (starts + ends - 1) // 2

    minimums = np.minimum.reduceat(y_array, starts)
    maximums = np.maximum.reduceat(y_array, starts)
    output_x = np.repeat(x_array[centers], 2)
    output_y = np.empty(output_x.size, dtype=y_array.dtype)
    output_y[0::2] = minimums
    output_y[1::2] = maximums
    return output_x, output_y


def load_npz_plot_data(
    path: str | Path,
    max_plot_points: int = MAX_PLOT_POINTS,
) -> NPZPlotData:
    npz_path = Path(path)
    with np.load(npz_path, allow_pickle=False) as data:
        missing = [key for key in ("time_s", "voltage_v") if key not in data]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}")

        time_s = np.asarray(data["time_s"], dtype=np.float64).reshape(-1)
        voltage_v = np.asarray(data["voltage_v"], dtype=np.float32).reshape(-1)
        if time_s.size != voltage_v.size:
            raise ValueError("time_s and voltage_v have different lengths")
        valid = np.isfinite(time_s) & np.isfinite(voltage_v)
        if not np.any(valid):
            raise ValueError("waveform contains no finite points")
        time_s, voltage_v = downsample_min_max(
            time_s[valid],
            voltage_v[valid],
            max_plot_points,
        )

        if "delay_s" in data:
            measurement = _npz_float(data, "delay_s", float("nan"))
            measurement_type = "DELAY"
        elif "positive_pulse_width_s" in data:
            measurement = _npz_float(data, "positive_pulse_width_s", float("nan"))
            measurement_type = "PWID"
        else:
            measurement = float("nan")
            measurement_type = "UNKNOWN"
        channel = _npz_text(data, "channel", "--")
        sample_rate = _npz_float(data, "sample_rate", float("nan"))
        point_count = int(_npz_float(data, "point_count", int(valid.sum())))

    return NPZPlotData(
        time_s=time_s,
        voltage_v=voltage_v,
        measurement_s=measurement,
        measurement_type=measurement_type,
        channel=channel,
        sample_rate=sample_rate,
        point_count=point_count,
    )


def load_csv_plot_data(
    path: str | Path,
    max_plot_points: int = MAX_PLOT_POINTS,
) -> CSVPlotData:
    rows = _read_csv_rows(Path(path))
    start, end, x_column, y_column = _find_longest_numeric_run(rows)
    if end <= start:
        raise ValueError("CSV contains no plottable numeric data")
    x_column, y_column = _select_preferred_numeric_columns(
        rows,
        start,
        end,
        x_column,
        y_column,
    )

    x_values: list[float] = []
    y_values: list[float] = []
    for row in rows[start:end]:
        if max(x_column, y_column) >= len(row):
            continue
        x_value = _parse_number(row[x_column])
        y_value = _parse_number(row[y_column])
        if x_value is None or y_value is None:
            continue
        if math.isfinite(x_value) and math.isfinite(y_value):
            x_values.append(x_value)
            y_values.append(y_value)
    if not x_values:
        raise ValueError("CSV contains no finite numeric data")

    raw_x_label, raw_y_label = _find_column_labels(
        rows,
        start,
        x_column,
        y_column,
    )
    has_zero_span_metadata = any(
        row and row[0].strip().casefold() == "frequency_mhz" for row in rows[:start]
    )
    looks_like_time = _contains_any(raw_x_label, ("time", "second", " sec", "_s"))
    zero_span = has_zero_span_metadata or looks_like_time
    title = "N9020A Zero Span" if zero_span else "N9020A Spectrum"
    x_label = _normalise_axis_label(
        raw_x_label,
        "Time" if zero_span else "Frequency",
        ("ps", "ns", "μs", "us", "ms", "s")
        if zero_span
        else ("GHz", "MHz", "kHz", "Hz"),
    )
    y_label = _normalise_axis_label(
        raw_y_label,
        "Amplitude",
        ("dBm", "dBμV", "dBuV", "mV", "V"),
    )

    x, y = downsample_min_max(
        np.asarray(x_values, dtype=np.float64),
        np.asarray(y_values, dtype=np.float64),
        max_plot_points,
    )
    return CSVPlotData(x=x, y=y, title=title, x_label=x_label, y_label=y_label)


def convert_npz_to_png(
    source: str | Path,
    output: str | Path,
    max_plot_points: int = MAX_PLOT_POINTS,
) -> Path:
    source_path = Path(source)
    output_path = Path(output)
    plot_data = load_npz_plot_data(source_path, max_plot_points)
    plt = _load_pyplot()
    figure = None
    try:
        figure, axes = plt.subplots(figsize=(12, 6))
        axes.plot(plot_data.time_s, plot_data.voltage_v, linewidth=0.7)
        axes.set_title("SDS3104X HD Waveform")
        axes.set_xlabel("Time (s)")
        axes.set_ylabel("Voltage (V)")
        axes.grid(True, alpha=0.25)
        details = [
            f"Channel: {plot_data.channel}",
            f"Points: {plot_data.point_count:,}",
        ]
        if plot_data.measurement_type == "DELAY":
            details.extend(
                (
                    "Measurement Type: DELAY",
                    f"Delay: {format_time_value(plot_data.measurement_s)}",
                )
            )
        elif plot_data.measurement_type == "PWID":
            details.extend(
                (
                    "Measurement Type: PWID (Legacy)",
                    f"Positive Pulse Width: {format_time_value(plot_data.measurement_s)}",
                )
            )
        else:
            details.extend(("Measurement Type: Unknown", "Advanced Measurement: N/A"))
        sample_rate = _format_sample_rate(plot_data.sample_rate)
        if sample_rate:
            details.insert(1, f"Sample Rate: {sample_rate}")
        axes.text(
            0.99,
            0.98,
            "\n".join(details),
            transform=axes.transAxes,
            ha="right",
            va="top",
            bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8},
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        figure.tight_layout()
        figure.savefig(output_path, dpi=150, format="png")
    finally:
        if figure is not None:
            plt.close(figure)
    return output_path


def convert_csv_to_png(
    source: str | Path,
    output: str | Path,
    max_plot_points: int = MAX_PLOT_POINTS,
) -> Path:
    source_path = Path(source)
    output_path = Path(output)
    plot_data = load_csv_plot_data(source_path, max_plot_points)
    plt = _load_pyplot()
    figure = None
    try:
        figure, axes = plt.subplots(figsize=(12, 6))
        axes.plot(plot_data.x, plot_data.y, linewidth=0.8)
        axes.set_title(plot_data.title)
        axes.set_xlabel(plot_data.x_label)
        axes.set_ylabel(plot_data.y_label)
        axes.grid(True, alpha=0.25)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        figure.tight_layout()
        figure.savefig(output_path, dpi=150, format="png")
    finally:
        if figure is not None:
            plt.close(figure)
    return output_path


def default_output_directory(source: str | Path) -> Path:
    source_path = Path(source)
    base = source_path if source_path.is_dir() else source_path.parent
    return base / "images"


def build_conversion_tasks(source: str | Path, output_directory: str | Path) -> list[ConversionTask]:
    source_path = Path(source)
    output_root = Path(output_directory)
    if not source_path.exists():
        raise FileNotFoundError(f"Data source does not exist: {source_path}")

    if source_path.is_file():
        if source_path.suffix.casefold() not in SUPPORTED_SUFFIXES:
            raise ValueError(f"Unsupported data file: {source_path.name}")
        source_root = source_path.parent
        files = [source_path]
    else:
        source_root = source_path
        output_resolved = output_root.resolve()
        source_resolved = source_root.resolve()
        output_is_inside_source = _is_relative_to(output_resolved, source_resolved)
        files = []
        for candidate in source_path.rglob("*"):
            if not candidate.is_file() or candidate.suffix.casefold() not in SUPPORTED_SUFFIXES:
                continue
            if output_is_inside_source and _is_relative_to(candidate.resolve(), output_resolved):
                continue
            files.append(candidate)
        files.sort(key=lambda item: str(item.relative_to(source_root)).casefold())

    if output_root.resolve() == source_root.resolve():
        raise ValueError("Output directory must be separate from the data directory")

    tasks: list[ConversionTask] = []
    for data_file in files:
        relative = data_file.relative_to(source_root)
        kind = data_file.suffix.casefold().lstrip(".")
        output_name = f"{data_file.stem}_{kind}.png"
        tasks.append(
            ConversionTask(
                source=data_file,
                output=output_root / relative.parent / output_name,
            )
        )
    return tasks


def convert_data_file(source: Path, output: Path) -> Path:
    suffix = source.suffix.casefold()
    if suffix == ".csv":
        return convert_csv_to_png(source, output)
    if suffix == ".npz":
        return convert_npz_to_png(source, output)
    raise ValueError(f"Unsupported data file: {source.name}")


def batch_convert(
    source: str | Path,
    output_directory: str | Path,
    *,
    overwrite: bool = False,
    should_stop: Callable[[], bool] | None = None,
    on_start: Callable[[int, int, ConversionTask], None] | None = None,
    on_result: Callable[[int, int, ConversionTask, str, str], None] | None = None,
) -> ConversionSummary:
    tasks = build_conversion_tasks(source, output_directory)
    summary = ConversionSummary(total=len(tasks))
    for position, task in enumerate(tasks, start=1):
        if should_stop is not None and should_stop():
            summary.stopped = True
            break
        if on_start is not None:
            on_start(position, len(tasks), task)

        status = "success"
        message = ""
        if task.output.exists() and not overwrite:
            status = "skipped"
            summary.skipped += 1
        else:
            try:
                convert_data_file(task.source, task.output)
                summary.success += 1
            except Exception as exc:
                status = "failed"
                message = f"{type(exc).__name__}: {exc}"
                summary.failed += 1
                summary.errors.append(f"{task.source}: {message}")

        if on_result is not None:
            on_result(position, len(tasks), task, status, message)
    return summary


def _load_pyplot():
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def _npz_float(data, key: str, default: float) -> float:
    if key not in data:
        return float(default)
    try:
        return float(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return float(default)


def _npz_text(data, key: str, default: str) -> str:
    if key not in data:
        return default
    try:
        return str(np.asarray(data[key]).reshape(-1)[0])
    except (IndexError, TypeError, ValueError):
        return default


def _read_csv_rows(path: Path) -> list[list[str]]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    else:
        raise ValueError(f"Unable to decode CSV: {last_error}")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(text.splitlines(), dialect) if any(cell.strip() for cell in row)]


def _find_longest_numeric_run(rows: list[list[str]]) -> tuple[int, int, int, int]:
    best = (0, 0, 0, 1)
    current_start = 0
    current_pair: tuple[int, int] | None = None
    previous_row = -2
    for row_index, row in enumerate(rows):
        numeric_columns = [index for index, cell in enumerate(row) if _parse_number(cell) is not None]
        pair = tuple(numeric_columns[:2]) if len(numeric_columns) >= 2 else None
        if pair is not None and pair == current_pair and row_index == previous_row + 1:
            pass
        elif pair is not None:
            current_start = row_index
            current_pair = pair
        else:
            current_pair = None
        previous_row = row_index

        if current_pair is not None:
            candidate = (current_start, row_index + 1, current_pair[0], current_pair[1])
            if candidate[1] - candidate[0] > best[1] - best[0]:
                best = candidate
    return best


def _find_column_labels(
    rows: list[list[str]],
    data_start: int,
    x_column: int,
    y_column: int,
) -> tuple[str, str]:
    best_score = -1
    best_labels = ("", "")
    for row in rows[max(0, data_start - 50) : data_start]:
        if max(x_column, y_column) >= len(row):
            continue
        x_label = row[x_column].strip()
        y_label = row[y_column].strip()
        if not x_label or not y_label:
            continue
        if _parse_number(x_label) is not None or _parse_number(y_label) is not None:
            continue
        score = 1
        if _contains_any(x_label, ("frequency", "freq", "time", "second")):
            score += 4
        if _contains_any(y_label, ("amplitude", "power", "level", "dbm", "voltage")):
            score += 4
        if score >= best_score:
            best_score = score
            best_labels = (x_label, y_label)
    return best_labels


def _select_preferred_numeric_columns(
    rows: list[list[str]],
    data_start: int,
    data_end: int,
    default_x: int,
    default_y: int,
) -> tuple[int, int]:
    x_words = ("frequency", "freq", "time", "second")
    y_words = ("amplitude", "power", "level", "dbm", "voltage")
    candidates: list[tuple[int, int, int]] = []
    for distance, row in enumerate(reversed(rows[max(0, data_start - 50) : data_start])):
        x_columns = [index for index, label in enumerate(row) if _contains_any(label, x_words)]
        y_columns = [index for index, label in enumerate(row) if _contains_any(label, y_words)]
        for x_column in x_columns:
            for y_column in y_columns:
                if x_column != y_column:
                    candidates.append((distance, x_column, y_column))

    required_rows = max(1, (data_end - data_start + 1) // 2)
    for _distance, x_column, y_column in sorted(candidates):
        valid_rows = sum(
            max(x_column, y_column) < len(row)
            and _parse_number(row[x_column]) is not None
            and _parse_number(row[y_column]) is not None
            for row in rows[data_start:data_end]
        )
        if valid_rows >= required_rows:
            return x_column, y_column
    return default_x, default_y


def _parse_number(value: str) -> float | None:
    text = value.strip().strip('"').strip("'")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _contains_any(value: str, words: tuple[str, ...]) -> bool:
    lower = value.casefold()
    return any(word.casefold() in lower for word in words)


def _normalise_axis_label(raw_label: str, generic: str, units: tuple[str, ...]) -> str:
    compact = raw_label.replace("µ", "μ")
    tokens = {
        token.casefold()
        for token in re.split(r"[^A-Za-z0-9μ]+", compact)
        if token
    }
    for unit in units:
        if unit.casefold() in tokens:
            display_unit = "μs" if unit in ("us", "μs") else unit
            return f"{generic} ({display_unit})"
    return generic


def _format_sample_rate(sample_rate: float) -> str:
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        return ""
    if sample_rate >= 1e9:
        return f"{sample_rate / 1e9:.5g} GSa/s"
    if sample_rate >= 1e6:
        return f"{sample_rate / 1e6:.5g} MSa/s"
    if sample_rate >= 1e3:
        return f"{sample_rate / 1e3:.5g} kSa/s"
    return f"{sample_rate:.5g} Sa/s"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from data_visualizer import (
    MAX_PLOT_POINTS,
    batch_convert,
    convert_csv_to_png,
    convert_npz_to_png,
    downsample_min_max,
    load_csv_plot_data,
    load_npz_plot_data,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _write_csv(path: Path, zero_span: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = "frequency_mhz,600.000000\n" if zero_span else ""
    x_label = "Time (s)" if zero_span else "Frequency (Hz)"
    path.write_text(
        "timestamp,2026-08-12T10:00:00\n"
        + metadata
        + f"{x_label},Amplitude (dBm)\n"
        + "0,-50\n1,-42\n2,-47\n",
        encoding="utf-8",
    )


def _write_npz(path: Path, pulse_width: float | None = 1.3447e-7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "time_s": np.linspace(0.0, 1e-6, 1000, dtype=np.float64),
        "voltage_v": np.sin(np.linspace(0.0, 8.0, 1000)).astype(np.float32),
        "channel": np.asarray("C1"),
        "sample_rate": np.asarray(1e9, dtype=np.float64),
        "point_count": np.asarray(1000, dtype=np.int32),
    }
    if pulse_width is not None:
        values["positive_pulse_width_s"] = np.asarray(pulse_width, dtype=np.float64)
    np.savez(path, **values)


def test_min_max_downsampling_preserves_narrow_peak() -> None:
    x = np.arange(250_001, dtype=np.float64)
    y = np.zeros(x.size, dtype=np.float32)
    y[123_456] = 99.0

    reduced_x, reduced_y = downsample_min_max(x, y, MAX_PLOT_POINTS)

    assert reduced_x.size <= MAX_PLOT_POINTS
    assert reduced_y.size <= MAX_PLOT_POINTS
    assert float(reduced_y.max()) == pytest.approx(99.0)


def test_large_npz_is_downsampled_to_configured_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.npz"
    point_count = 250_001
    time_s = np.arange(point_count, dtype=np.float64) * 1e-9
    voltage_v = np.zeros(point_count, dtype=np.float32)
    voltage_v[125_000] = 3.0
    np.savez(path, time_s=time_s, voltage_v=voltage_v, point_count=point_count)

    plot_data = load_npz_plot_data(path)

    assert plot_data.time_s.size <= MAX_PLOT_POINTS
    assert plot_data.voltage_v.size <= MAX_PLOT_POINTS
    assert float(plot_data.voltage_v.max()) == pytest.approx(3.0)


def test_npz_reads_positive_pulse_width(tmp_path: Path) -> None:
    path = tmp_path / "waveform.npz"
    _write_npz(path, 1.3447e-7)

    plot_data = load_npz_plot_data(path)

    assert plot_data.positive_pulse_width_s == pytest.approx(1.3447e-7)


@pytest.mark.parametrize("pulse_width", [None, float("nan")])
def test_old_or_nan_npz_converts_with_unavailable_pwid(
    tmp_path: Path,
    pulse_width: float | None,
) -> None:
    source = tmp_path / "waveform.npz"
    output = tmp_path / "waveform.png"
    _write_npz(source, pulse_width)

    plot_data = load_npz_plot_data(source)
    convert_npz_to_png(source, output)

    assert np.isnan(plot_data.positive_pulse_width_s)
    assert output.read_bytes().startswith(PNG_SIGNATURE)


def test_csv_sample_is_detected_and_converted(tmp_path: Path) -> None:
    source = tmp_path / "spectrum.csv"
    output = tmp_path / "spectrum.png"
    _write_csv(source)

    plot_data = load_csv_plot_data(source)
    convert_csv_to_png(source, output)

    assert plot_data.title == "N9020A Spectrum"
    assert plot_data.x_label == "Frequency (Hz)"
    assert plot_data.y_label == "Amplitude (dBm)"
    assert output.read_bytes().startswith(PNG_SIGNATURE)


def test_csv_prefers_frequency_and_amplitude_over_numeric_index(tmp_path: Path) -> None:
    source = tmp_path / "three_columns.csv"
    source.write_text(
        "Index,Frequency (MHz),Amplitude (dBm)\n"
        "1,600.0,-50\n"
        "2,600.5,-42\n"
        "3,601.0,-47\n",
        encoding="utf-8",
    )

    plot_data = load_csv_plot_data(source)

    assert plot_data.x.tolist() == pytest.approx([600.0, 600.5, 601.0])
    assert plot_data.y.tolist() == pytest.approx([-50.0, -42.0, -47.0])
    assert plot_data.x_label == "Frequency (MHz)"


def test_zero_span_csv_uses_time_axis_and_title(tmp_path: Path) -> None:
    source = tmp_path / "zero_span.csv"
    _write_csv(source, zero_span=True)

    plot_data = load_csv_plot_data(source)

    assert plot_data.title == "N9020A Zero Span"
    assert plot_data.x_label == "Time (s)"


def test_recursive_batch_keeps_hierarchy_and_separates_csv_npz(tmp_path: Path) -> None:
    source_root = tmp_path / "data"
    frequency_folder = source_root / "600.000MHz"
    _write_csv(frequency_folder / "000001.csv", zero_span=True)
    _write_npz(frequency_folder / "000001.npz")
    output_root = source_root / "images"

    summary = batch_convert(source_root, output_root)

    csv_png = output_root / "600.000MHz" / "000001_csv.png"
    npz_png = output_root / "600.000MHz" / "000001_npz.png"
    assert summary.total == 2
    assert summary.success == 2
    assert summary.failed == 0
    assert csv_png.read_bytes().startswith(PNG_SIGNATURE)
    assert npz_png.read_bytes().startswith(PNG_SIGNATURE)

    skipped = batch_convert(source_root, output_root, overwrite=False)
    assert skipped.total == 2
    assert skipped.success == 0
    assert skipped.skipped == 2
    assert skipped.failed == 0

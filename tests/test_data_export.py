from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from data_export import (
    ExportOptions,
    compute_frequency_statistics,
    export_scope_npz_to_csv,
    generate_pulse_width_html_report,
    parse_frequency_directory,
    read_pulse_width_record,
    run_data_export,
    scan_frequency_datasets,
    scan_samples,
)


def _write_npz(path: Path, pulse_width_s: float | None = 1.0e-7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "time_s": np.asarray([-1.0e-6, 0.0, 1.0e-6], dtype=np.float64),
        "voltage_v": np.asarray([-0.25, 0.5, 1.25], dtype=np.float32),
        "adc": np.asarray([-10, 20, 50], dtype=np.int16),
    }
    if pulse_width_s is not None:
        values["positive_pulse_width_s"] = np.asarray(
            pulse_width_s,
            dtype=np.float64,
        )
    np.savez(path, **values)


def _write_n9020a_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Time (s),Amplitude (dBm)\n0,-50\n1e-6,-42\n",
        encoding="utf-8",
    )


def _write_delay_npz(path: Path, delay_s: float = 1.0e-7) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        path,
        time_s=np.asarray([-1e-6, 0, 1e-6], dtype=np.float64),
        voltage_v=np.asarray([-1, 0, 1], dtype=np.float32),
        adc=np.asarray([-1, 0, 1], dtype=np.int16),
        delay_s=np.asarray(delay_s, dtype=np.float64),
        delay_raw=np.asarray(f"{delay_s:.4E}"),
        delay_valid=np.asarray(True, dtype=np.bool_),
        delay_attempts=np.asarray(2, dtype=np.int32),
        advanced_measurement_type=np.asarray("DELAY"),
    )


@pytest.mark.parametrize(
    ("directory_name", "expected"),
    [
        ("600MHz", 600.0),
        ("600.000MHz", 600.0),
        ("600.0mhz", 600.0),
        (" 605 MHz ", 605.0),
        ("export", None),
        ("600kHz", None),
    ],
)
def test_parse_frequency_directory(directory_name: str, expected: float | None) -> None:
    assert parse_frequency_directory(directory_name) == expected


def test_frequency_scan_ignores_non_frequency_and_nested_export(tmp_path: Path) -> None:
    for name in ("605.0MHz", "600MHz", "images", "reports", "export"):
        (tmp_path / name).mkdir()
    (tmp_path / "export" / "999MHz").mkdir()

    datasets = scan_frequency_datasets(tmp_path)

    assert [item.directory_name for item in datasets] == ["600MHz", "605.0MHz"]
    assert [item.frequency_mhz for item in datasets] == [600.0, 605.0]


def test_scan_and_summary_support_plain_root_directory(tmp_path: Path) -> None:
    _write_n9020a_csv(tmp_path / "000001.csv")
    _write_npz(tmp_path / "000001.npz", 125e-9)

    datasets = scan_frequency_datasets(tmp_path)
    summary = run_data_export(
        tmp_path,
        tmp_path / "export",
        ExportOptions(pulse_width_summary=True),
    )

    assert len(datasets) == 1
    assert datasets[0].path == tmp_path
    assert datasets[0].frequency_mhz is None
    assert summary.failed == 0
    assert (tmp_path / "pulse_width_summary.csv").is_file()


def test_scope_npz_to_csv_has_required_columns_and_values(tmp_path: Path) -> None:
    source = tmp_path / "000001.npz"
    output = tmp_path / "export" / "000001_scope.csv"
    _write_npz(source)

    export_scope_npz_to_csv(source, output, chunk_rows=2)

    with output.open(encoding="utf-8", newline="") as input_file:
        rows = list(csv.reader(input_file))
    assert rows[0] == ["index", "time_s", "voltage_v", "adc"]
    assert rows[1][0] == "0"
    assert float(rows[1][1]) == pytest.approx(-1.0e-6)
    assert float(rows[1][2]) == pytest.approx(-0.25)
    assert rows[1][3] == "-10"
    assert rows[-1][0] == "2"


def test_old_npz_missing_pwid_is_invalid_without_failure(tmp_path: Path) -> None:
    frequency_dir = tmp_path / "600.000MHz"
    _write_n9020a_csv(frequency_dir / "000001.csv")
    _write_npz(frequency_dir / "000001.npz", pulse_width_s=None)
    dataset = scan_frequency_datasets(tmp_path)[0]

    record, error = read_pulse_width_record(scan_samples(dataset)[0], tmp_path)

    assert error is None
    assert not record.valid
    assert np.isnan(record.pulse_width_s)
    assert record.pair_status == "paired"


def test_pwid_statistics_include_nan_and_pairing_status(tmp_path: Path) -> None:
    frequency_dir = tmp_path / "600.000MHz"
    for index, pulse_width in ((1, 100e-9), (2, 200e-9), (3, float("nan"))):
        stem = f"{index:06d}"
        _write_n9020a_csv(frequency_dir / f"{stem}.csv")
        _write_npz(frequency_dir / f"{stem}.npz", pulse_width)
    _write_n9020a_csv(frequency_dir / "000004.csv")
    _write_npz(frequency_dir / "000005.npz", 150e-9)
    dataset = scan_frequency_datasets(tmp_path)[0]
    records = [
        read_pulse_width_record(sample, tmp_path)[0]
        for sample in scan_samples(dataset)
    ]

    statistics = compute_frequency_statistics(records)[0]

    assert statistics.total_samples == 5
    assert statistics.valid_samples == 3
    assert statistics.invalid_samples == 2
    assert statistics.valid_rate == pytest.approx(0.6)
    assert statistics.mean_ns == pytest.approx(150.0)
    assert statistics.median_ns == pytest.approx(150.0)
    assert statistics.std_ns == pytest.approx(np.std([100.0, 200.0, 150.0]))
    assert statistics.min_ns == pytest.approx(100.0)
    assert statistics.max_ns == pytest.approx(200.0)
    assert statistics.p05_ns == pytest.approx(105.0)
    assert statistics.p95_ns == pytest.approx(195.0)
    assert statistics.missing_csv == 1
    assert statistics.missing_npz == 1


def test_export_writes_scope_csv_and_all_summary_outputs(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    for frequency_name, pulse_width in (("600MHz", 100e-9), ("605.000MHz", 120e-9)):
        folder = data_root / frequency_name
        _write_n9020a_csv(folder / "000001.csv")
        _write_npz(folder / "000001.npz", pulse_width)
    output_root = data_root / "export"
    options = ExportOptions(scope_csv=True, pulse_width_summary=True)

    summary = run_data_export(data_root, output_root, options)

    assert summary.failed == 0
    assert (output_root / "scope_csv" / "600MHz" / "000001_scope.csv").is_file()
    assert (data_root / "600MHz" / "pulse_width_summary.csv").is_file()
    assert (data_root / "605.000MHz" / "pulse_width_summary.csv").is_file()
    assert (output_root / "summary" / "pulse_width_all.csv").is_file()
    assert (output_root / "summary" / "pulse_width_by_frequency.csv").is_file()

    with (output_root / "summary" / "pulse_width_all.csv").open(
        encoding="utf-8-sig",
        newline="",
    ) as input_file:
        rows = list(csv.DictReader(input_file))
    assert [float(row["frequency_mhz"]) for row in rows] == [600.0, 605.0]
    assert [float(row["pulse_width_ns"]) for row in rows] == pytest.approx(
        [100.0, 120.0]
    )
    assert [int(row["attempts"]) for row in rows] == [1, 1]


def test_existing_scope_csv_is_skipped_when_overwrite_is_false(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    folder = data_root / "600MHz"
    _write_npz(folder / "000001.npz")
    output_root = data_root / "export"
    options = ExportOptions(scope_csv=True)

    first = run_data_export(data_root, output_root, options)
    second = run_data_export(data_root, output_root, options)

    assert first.success == 1
    assert first.skipped == 0
    assert second.success == 0
    assert second.skipped == 1
    assert second.failed == 0


def test_html_report_is_single_file_offline_and_contains_frequency(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    folder = data_root / "600.000MHz"
    _write_n9020a_csv(folder / "000001.csv")
    _write_npz(folder / "000001.npz", 134.47e-9)
    dataset = scan_frequency_datasets(data_root)[0]
    records = [
        read_pulse_width_record(sample, data_root)[0]
        for sample in scan_samples(dataset)
    ]
    statistics = compute_frequency_statistics(records)
    output = data_root / "export" / "reports" / "pulse_width_report.html"

    generate_pulse_width_html_report(data_root, records, statistics, output)

    report = output.read_text(encoding="utf-8")
    assert "600 MHz" in report
    assert "Positive Pulse Width" in report
    assert "Plotly.newPlot" in report
    assert '<script src="http' not in report
    assert not list(output.parent.glob("*.js"))


def test_html_export_prefers_existing_summary_over_loading_npz(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    folder = data_root / "600MHz"
    _write_n9020a_csv(folder / "000001.csv")
    _write_npz(folder / "000001.npz", 100e-9)
    first = run_data_export(
        data_root,
        data_root / "export",
        ExportOptions(pulse_width_summary=True),
    )
    assert first.failed == 0
    (folder / "000001.npz").write_bytes(b"not an npz")

    html = run_data_export(
        data_root,
        data_root / "html-export",
        ExportOptions(html_report=True),
    )

    assert html.failed == 0
    assert (data_root / "html-export" / "reports" / "pulse_width_report.html").is_file()


def test_delay_export_uses_new_summary_names_and_report_wording(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    folder = data_root / "600MHz"
    _write_n9020a_csv(folder / "000001.csv")
    _write_delay_npz(folder / "000001.npz", 134.47e-9)
    output_root = data_root / "export"

    result = run_data_export(
        data_root,
        output_root,
        ExportOptions(measurement_summary=True, html_report=True),
    )

    assert result.failed == 0
    assert (folder / "delay_summary.csv").is_file()
    all_summary = output_root / "summary" / "delay_all.csv"
    report_path = output_root / "reports" / "delay_report.html"
    assert all_summary.is_file() and report_path.is_file()
    with all_summary.open(encoding="utf-8-sig", newline="") as source:
        row = next(csv.DictReader(source))
    assert float(row["delay_ns"]) == pytest.approx(134.47)
    report = report_path.read_text(encoding="utf-8")
    assert "Measurement Type: DELAY" in report
    assert "Delay Distribution" in report


def test_legacy_report_is_explicitly_labeled_and_not_mixed_with_delay(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_folder = legacy_root / "600MHz"
    _write_n9020a_csv(legacy_folder / "000001.csv")
    _write_npz(legacy_folder / "000001.npz", 100e-9)
    result = run_data_export(
        legacy_root,
        legacy_root / "export",
        ExportOptions(html_report=True),
    )
    assert result.failed == 0
    report = (legacy_root / "export" / "reports" / "pulse_width_report.html").read_text(
        encoding="utf-8"
    )
    assert "Measurement Type: PWID (Legacy)" in report

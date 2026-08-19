from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from acquisition_worker import AcquisitionWorker, CaptureRequest
from file_pairing import is_capture_complete, next_capture_index
from measurement_summary import load_measurement_summary
from sds3104xhd_client import AdvancedMeasurementResult


def _touch(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).touch()


def test_legacy_csv_only_mode_uses_max_csv_index(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000003.csv")
    _touch(tmp_path, "not-an-index.csv")
    assert next_capture_index(tmp_path, scope_enabled=False) == 4


def test_scope_complete_requires_csv_delay_and_cycles(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000001_delay.npz")
    assert not is_capture_complete(tmp_path, 1, scope_enabled=True)
    _touch(tmp_path, "000001_cycles.npz")
    assert is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert next_capture_index(tmp_path, scope_enabled=True) == 2


def test_missing_delay_or_cycles_is_incomplete(tmp_path: Path) -> None:
    for index, missing in ((1, "delay"), (2, "cycles")):
        _touch(tmp_path, f"{index:06d}.csv")
        if missing != "delay":
            _touch(tmp_path, f"{index:06d}_delay.npz")
        if missing != "cycles":
            _touch(tmp_path, f"{index:06d}_cycles.npz")
        assert not is_capture_complete(tmp_path, index, scope_enabled=True)


def test_legacy_unsuffixed_npz_is_not_a_new_complete_group(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000001.npz")
    assert not is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert next_capture_index(tmp_path, scope_enabled=True) == 1


class _FakeN9020A:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def set_center_and_span_mhz(self, _center: float, _span: float) -> None:
        pass

    def fetch_csv_text(self) -> str:
        if self.events is not None:
            self.events.append("n9020a_csv")
        return "frequency,power\n1,-20"


class _FakeWaveform:
    point_count = 2
    voltage_v = np.asarray([-0.25, 1.5], dtype=np.float32)

    def __init__(self, frame: int) -> None:
        self.frame = frame
        self.time_s = np.asarray([0.0, 1.0], dtype=np.float64)
        self.adc = np.asarray([0, 1], dtype=np.int16)


class _FakeScope:
    def __init__(self, events: list[str] | None = None, fail_save: bool = False) -> None:
        self.events = events
        self.fail_save = fail_save
        self.frame = 0
        self.pending_type = ""
        self.saved_frames: dict[str, int] = {}
        self.config = SimpleNamespace(delay_time_scale_sec=5e-7, cycles_time_scale_sec=1e-4)

    def set_time_scale(self, seconds: float) -> None:
        if self.events is not None:
            self.events.append(f"scale:{seconds:g}")

    def configure_advanced_measurement(self, measurement_type: str) -> None:
        self.pending_type = measurement_type
        if self.events is not None:
            self.events.append(f"type:{measurement_type}")

    def acquire_single_with_measurement_retry(self, measurement_type: str, **_kwargs):
        assert measurement_type == self.pending_type
        self.frame += 1
        if self.events is not None:
            self.events.append(f"single:{measurement_type}")
        value = 125e-9 if measurement_type == "DELAY" else 23.0
        return AdvancedMeasurementResult(
            measurement_type,
            value,
            f"{value:g}",
            True,
            1,
            0.1,
            "s" if measurement_type == "DELAY" else "count",
        )

    def read_waveform(self) -> _FakeWaveform:
        if self.events is not None:
            self.events.append(f"waveform:{self.pending_type}")
        return _FakeWaveform(self.frame)

    def save_npz(self, path: Path, waveform: _FakeWaveform, index: int, *, measurement_result) -> None:
        if self.events is not None:
            self.events.append(f"save:{measurement_result.measurement_type}")
        self.saved_frames[measurement_result.measurement_type] = waveform.frame
        with path.open("wb") as output:
            np.savez(
                output,
                index=np.asarray(index, dtype=np.int32),
                time_s=waveform.time_s,
                voltage_v=waveform.voltage_v,
                adc=waveform.adc,
                measurement_type=np.asarray(measurement_result.measurement_type),
                measurement_value=np.asarray(measurement_result.value, dtype=np.float64),
                measurement_unit=np.asarray(measurement_result.unit),
                measurement_raw=np.asarray(measurement_result.raw_value),
                measurement_valid=np.asarray(measurement_result.valid, dtype=np.bool_),
                measurement_attempts=np.asarray(measurement_result.attempts, dtype=np.int32),
            )
        if self.fail_save and measurement_result.measurement_type == "CYCLES":
            raise OSError("simulated NPZ save error")


def test_worker_strict_order_independent_singles_and_waveforms(tmp_path: Path) -> None:
    events: list[str] = []
    scope = _FakeScope(events)
    worker = AcquisitionWorker(
        _FakeN9020A(events), scope, CaptureRequest(tmp_path, index=1, scope_enabled=True)
    )  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert events == [
        "n9020a_csv",
        "scale:5e-07",
        "type:DELAY",
        "single:DELAY",
        "waveform:DELAY",
        "scale:0.0001",
        "type:CYCLES",
        "single:CYCLES",
        "waveform:CYCLES",
        "save:DELAY",
        "save:CYCLES",
    ]
    assert scope.saved_frames == {"DELAY": 1, "CYCLES": 2}
    assert is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert not list(tmp_path.glob("*.tmp"))


def test_worker_commits_three_files_and_updates_summary(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    worker = AcquisitionWorker(
        _FakeN9020A(), _FakeScope(), CaptureRequest(tmp_path, 1, True)
    )  # type: ignore[arg-type]
    worker.capture_one()

    assert {path.name for path in tmp_path.glob("000001*")} == {
        "000001.csv",
        "000001_delay.npz",
        "000001_cycles.npz",
    }
    records = load_measurement_summary(tmp_path / "measurement_summary.csv")
    assert len(records) == 1
    assert records[0].delay_s == 125e-9
    assert records[0].cycles_count == 23.0


def test_worker_failure_cleans_all_tmp_and_formal_files(tmp_path: Path) -> None:
    worker = AcquisitionWorker(
        _FakeN9020A(), _FakeScope(fail_save=True), CaptureRequest(tmp_path, 1, True)
    )  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.capture_one()

    assert errors and "simulated NPZ save error" in errors[0]
    assert not list(tmp_path.glob("000001*"))
    assert next_capture_index(tmp_path, scope_enabled=True) == 1

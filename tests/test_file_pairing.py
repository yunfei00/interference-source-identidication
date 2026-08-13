from __future__ import annotations

from pathlib import Path

import numpy as np

from acquisition_worker import AcquisitionWorker, CaptureRequest
from file_pairing import is_capture_complete, next_capture_index
from pulse_width_summary import load_pulse_width_summary
from sds3104xhd_client import PulseWidthAcquisitionResult


def _touch(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).touch()


def test_legacy_csv_only_mode_uses_max_csv_index(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000003.csv")
    _touch(tmp_path, "not-an-index.csv")
    assert next_capture_index(tmp_path, scope_enabled=False) == 4


def test_scope_enabled_csv_only_is_not_complete(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    assert not is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert next_capture_index(tmp_path, scope_enabled=True) == 1


def test_scope_enabled_npz_only_is_not_complete(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.npz")
    assert not is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert next_capture_index(tmp_path, scope_enabled=True) == 1


def test_scope_enabled_csv_and_npz_are_complete(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000001.npz")
    assert is_capture_complete(tmp_path, 1, scope_enabled=True)
    assert next_capture_index(tmp_path, scope_enabled=True) == 2


def test_scope_enabled_stops_at_first_incomplete_pair(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")
    _touch(tmp_path, "000001.npz")
    _touch(tmp_path, "000002.csv")
    _touch(tmp_path, "000003.csv")
    _touch(tmp_path, "000003.npz")
    assert next_capture_index(tmp_path, scope_enabled=True) == 2


class _FakeN9020A:
    def set_center_and_span_mhz(self, _center: float, _span: float) -> None:
        pass

    def fetch_csv_text(self) -> str:
        return "frequency,power\n1,-20"


class _FakeWaveform:
    point_count = 2
    voltage_v = np.asarray([-0.25, 1.5], dtype=np.float32)

    def __init__(self, frame: int = 1) -> None:
        self.frame = frame


class _FakeScope:
    def __init__(self, fail_save: bool = False) -> None:
        self.fail_save = fail_save

    def acquire_single_with_pwid_retry(
        self,
        *,
        capture_index: int,
        should_stop,
    ) -> PulseWidthAcquisitionResult:
        assert capture_index >= 1
        assert not should_stop()
        return PulseWidthAcquisitionResult(1.25e-7, "1.25E-07", True, 1, 0.1)

    def read_waveform(self) -> _FakeWaveform:
        return _FakeWaveform()

    def save_npz(
        self,
        path: Path,
        _waveform: _FakeWaveform,
        index: int,
        positive_pulse_width_s: float,
        *,
        positive_pulse_width_raw: str,
        positive_pulse_width_valid: bool,
        positive_pulse_width_attempts: int,
    ) -> None:
        with path.open("wb") as output_file:
            np.savez(
                output_file,
                index=np.asarray(index, dtype=np.int32),
                positive_pulse_width_s=np.asarray(
                    positive_pulse_width_s,
                    dtype=np.float64,
                ),
                positive_pulse_width_raw=np.asarray(positive_pulse_width_raw),
                positive_pulse_width_valid=np.asarray(
                    positive_pulse_width_valid,
                    dtype=np.bool_,
                ),
                positive_pulse_width_attempts=np.asarray(
                    positive_pulse_width_attempts,
                    dtype=np.int32,
                ),
            )
        if self.fail_save:
            raise OSError("simulated NPZ save error")


def test_worker_commits_csv_and_npz_pair(tmp_path: Path) -> None:
    _touch(tmp_path, "000001.csv")  # An orphan from an interrupted older run.
    request = CaptureRequest(tmp_path, index=1, scope_enabled=True)
    worker = AcquisitionWorker(_FakeN9020A(), _FakeScope(), request)  # type: ignore[arg-type]
    results: list[dict] = []
    errors: list[str] = []
    worker.finished.connect(results.append)
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert len(results) == 1
    assert (tmp_path / "000001.csv").is_file()
    assert (tmp_path / "000001.npz").is_file()
    summary = load_pulse_width_summary(tmp_path / "pulse_width_summary.csv")
    assert len(summary) == 1
    assert summary[0].index == 1
    assert summary[0].attempts == 1
    assert not (tmp_path / "000001.csv.tmp").exists()
    assert not (tmp_path / "000001.npz.tmp").exists()


def test_worker_save_failure_cleans_tmp_and_formal_files(tmp_path: Path) -> None:
    request = CaptureRequest(tmp_path, index=1, scope_enabled=True)
    worker = AcquisitionWorker(_FakeN9020A(), _FakeScope(fail_save=True), request)  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)

    worker.capture_one()

    assert errors and "simulated NPZ save error" in errors[0]
    assert list(tmp_path.iterdir()) == []
    assert next_capture_index(tmp_path, scope_enabled=True) == 1


def test_worker_reads_pwid_after_single_and_before_waveform(tmp_path: Path) -> None:
    events: list[str] = []

    class OrderedN9020A(_FakeN9020A):
        def fetch_csv_text(self) -> str:
            events.append("n9020a_csv")
            return super().fetch_csv_text()

    class OrderedScope(_FakeScope):
        def acquire_single_with_pwid_retry(
            self,
            *,
            capture_index: int,
            should_stop,
        ) -> PulseWidthAcquisitionResult:
            events.append("single")
            events.append("pwid")
            return super().acquire_single_with_pwid_retry(
                capture_index=capture_index,
                should_stop=should_stop,
            )

        def read_waveform(self) -> _FakeWaveform:
            events.append("waveform")
            return super().read_waveform()

        def save_npz(
            self,
            path: Path,
            waveform: _FakeWaveform,
            index: int,
            pulse_width: float,
            **metadata,
        ) -> None:
            events.append("save_npz")
            super().save_npz(path, waveform, index, pulse_width, **metadata)

    worker = AcquisitionWorker(
        OrderedN9020A(),
        OrderedScope(),
        CaptureRequest(tmp_path, index=1, scope_enabled=True),
    )  # type: ignore[arg-type]

    worker.capture_one()

    assert events == ["single", "pwid", "n9020a_csv", "waveform", "save_npz"]


def test_unavailable_pwid_does_not_fail_capture_pair(tmp_path: Path) -> None:
    class UnavailablePWIDScope(_FakeScope):
        def acquire_single_with_pwid_retry(
            self,
            *,
            capture_index: int,
            should_stop,
        ) -> PulseWidthAcquisitionResult:
            return PulseWidthAcquisitionResult(float("nan"), "****", False, 5, 0.1)

    worker = AcquisitionWorker(
        _FakeN9020A(),
        UnavailablePWIDScope(),
        CaptureRequest(tmp_path, index=1, scope_enabled=True),
    )  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert is_capture_complete(tmp_path, 1, scope_enabled=True)
    summary = load_pulse_width_summary(tmp_path / "pulse_width_summary.csv")
    assert summary[0].valid is False
    assert summary[0].attempts == 5
    assert summary[0].raw_value == "****"


def test_worker_reads_waveform_from_last_pwid_attempt(tmp_path: Path) -> None:
    saved: dict[str, object] = {}

    class FiveAttemptScope(_FakeScope):
        def __init__(self) -> None:
            super().__init__()
            self.frame = 0

        def acquire_single_with_pwid_retry(
            self,
            *,
            capture_index: int,
            should_stop,
        ) -> PulseWidthAcquisitionResult:
            for _attempt in range(5):
                self.frame += 1
            return PulseWidthAcquisitionResult(float("nan"), "****", False, 5, 0.5)

        def read_waveform(self) -> _FakeWaveform:
            return _FakeWaveform(self.frame)

        def save_npz(
            self,
            path: Path,
            waveform: _FakeWaveform,
            index: int,
            positive_pulse_width_s: float,
            **metadata,
        ) -> None:
            saved["frame"] = waveform.frame
            saved["attempts"] = metadata["positive_pulse_width_attempts"]
            super().save_npz(
                path,
                waveform,
                index,
                positive_pulse_width_s,
                **metadata,
            )

    worker = AcquisitionWorker(
        _FakeN9020A(),
        FiveAttemptScope(),
        CaptureRequest(tmp_path, index=1, scope_enabled=True),
    )  # type: ignore[arg-type]

    worker.capture_one()

    assert saved == {"frame": 5, "attempts": 5}
    assert is_capture_complete(tmp_path, 1, scope_enabled=True)

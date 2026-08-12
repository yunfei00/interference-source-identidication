from __future__ import annotations

from pathlib import Path

import numpy as np

from acquisition_worker import AcquisitionWorker, CaptureRequest
from file_pairing import is_capture_complete, next_capture_index


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


class _FakeScope:
    def __init__(self, fail_save: bool = False) -> None:
        self.fail_save = fail_save

    def acquire_single(self) -> float:
        return 0.1

    def read_waveform(self) -> _FakeWaveform:
        return _FakeWaveform()

    def read_positive_pulse_width(self, _index: int) -> float:
        return 1.25e-7

    def save_npz(
        self,
        path: Path,
        _waveform: _FakeWaveform,
        _index: int,
        _positive_pulse_width_s: float,
    ) -> None:
        path.write_bytes(b"npz")
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
        def acquire_single(self) -> float:
            events.append("single")
            return super().acquire_single()

        def read_positive_pulse_width(self, index: int) -> float:
            events.append("pwid")
            return super().read_positive_pulse_width(index)

        def read_waveform(self) -> _FakeWaveform:
            events.append("waveform")
            return super().read_waveform()

        def save_npz(
            self,
            path: Path,
            waveform: _FakeWaveform,
            index: int,
            pulse_width: float,
        ) -> None:
            events.append("save_npz")
            super().save_npz(path, waveform, index, pulse_width)

    worker = AcquisitionWorker(
        OrderedN9020A(),
        OrderedScope(),
        CaptureRequest(tmp_path, index=1, scope_enabled=True),
    )  # type: ignore[arg-type]

    worker.capture_one()

    assert events == ["single", "pwid", "n9020a_csv", "waveform", "save_npz"]


def test_unavailable_pwid_does_not_fail_capture_pair(tmp_path: Path) -> None:
    class UnavailablePWIDScope(_FakeScope):
        def read_positive_pulse_width(self, _index: int) -> float:
            return float("nan")

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

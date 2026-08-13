from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from file_pairing import is_capture_complete, remove_incomplete_pair
from n9020a_client import N9020AClient
from pulse_width_summary import rebuild_pulse_width_summary, update_capture_summary
from sds3104xhd_client import AcquisitionStopped, SDS3104XHDClient


@dataclass(frozen=True)
class CaptureRequest:
    output_folder: Path
    index: int
    scope_enabled: bool
    frequency_mhz: float | None = None


class AcquisitionWorker(QObject):
    """Perform exactly one capture without touching GUI objects."""

    finished = Signal(dict)
    error = Signal(str)
    completed = Signal()

    def __init__(
        self,
        n9020a_client: N9020AClient,
        scope_client: SDS3104XHDClient | None,
        request: CaptureRequest,
    ) -> None:
        super().__init__()
        self.n9020a_client = n9020a_client
        self.scope_client = scope_client
        self.request = request
        self._stop_requested = Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    @Slot()
    def capture_one(self) -> None:
        folder = self.request.output_folder
        stem = f"{self.request.index:06d}"
        csv_path = folder / f"{stem}.csv"
        npz_path = folder / f"{stem}.npz"
        csv_tmp = folder / f"{stem}.csv.tmp"
        npz_tmp = folder / f"{stem}.npz.tmp"

        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._unlink(csv_tmp)
            self._unlink(npz_tmp)

            if self.request.frequency_mhz is not None:
                self.n9020a_client.set_center_and_span_mhz(self.request.frequency_mhz, 0.0)

            if self.request.scope_enabled:
                if self.scope_client is None:
                    raise RuntimeError("Scope is enabled but not connected")
                if is_capture_complete(folder, self.request.index, scope_enabled=True):
                    raise FileExistsError(f"Capture pair already exists: {stem}")
                remove_incomplete_pair(folder, self.request.index)

                pulse_width = self.scope_client.acquire_single_with_pwid_retry(
                    capture_index=self.request.index,
                    should_stop=self._stop_requested.is_set,
                )
                self._raise_if_stopped()
                csv_text = self.n9020a_client.fetch_csv_text()
                self._raise_if_stopped()
                read_started = time.monotonic()
                waveform = self.scope_client.read_waveform()
                read_seconds = time.monotonic() - read_started
                self._raise_if_stopped()

                self._write_csv_tmp(csv_tmp, csv_text)
                save_started = time.monotonic()
                self.scope_client.save_npz(
                    npz_tmp,
                    waveform,
                    self.request.index,
                    pulse_width.pulse_width_s,
                    positive_pulse_width_raw=pulse_width.pulse_width_raw,
                    positive_pulse_width_valid=pulse_width.pulse_width_valid,
                    positive_pulse_width_attempts=pulse_width.attempts,
                )
                save_seconds = time.monotonic() - save_started
                self._commit_pair(csv_tmp, npz_tmp, csv_path, npz_path)
                try:
                    update_capture_summary(folder, self.request.index)
                except Exception:
                    csv_path.unlink(missing_ok=True)
                    npz_path.unlink(missing_ok=True)
                    rebuild_pulse_width_summary(folder)
                    raise

                self.finished.emit(
                    {
                        "index": self.request.index,
                        "frequency_mhz": self.request.frequency_mhz,
                        "csv_path": str(csv_path),
                        "npz_path": str(npz_path),
                        "scope": {
                            "point_count": waveform.point_count,
                            "min_voltage": float(waveform.voltage_v.min()),
                            "max_voltage": float(waveform.voltage_v.max()),
                            "single_seconds": pulse_width.single_seconds,
                            "read_seconds": read_seconds,
                            "save_seconds": save_seconds,
                            "positive_pulse_width_s": pulse_width.pulse_width_s,
                            "positive_pulse_width_raw": pulse_width.pulse_width_raw,
                            "positive_pulse_width_valid": pulse_width.pulse_width_valid,
                            "positive_pulse_width_attempts": pulse_width.attempts,
                        },
                    }
                )
                self.completed.emit()
                return

            if csv_path.exists():
                raise FileExistsError(f"CSV already exists: {csv_path.name}")
            csv_text = self.n9020a_client.fetch_csv_text()
            self._write_csv_tmp(csv_tmp, csv_text)
            csv_tmp.replace(csv_path)
            self.finished.emit(
                {
                    "index": self.request.index,
                    "frequency_mhz": self.request.frequency_mhz,
                    "csv_path": str(csv_path),
                    "npz_path": None,
                    "scope": None,
                }
            )
            self.completed.emit()
        except AcquisitionStopped:
            self._unlink(csv_tmp)
            self._unlink(npz_tmp)
            self.completed.emit()
        except Exception as exc:
            cleanup_errors: list[str] = []
            for path in (csv_tmp, npz_tmp):
                try:
                    self._unlink(path)
                except OSError as cleanup_exc:
                    cleanup_errors.append(f"{path.name}: {cleanup_exc}")
            if self.request.scope_enabled and csv_path.exists() != npz_path.exists():
                for path in (csv_path, npz_path):
                    try:
                        self._unlink(path)
                    except OSError as cleanup_exc:
                        cleanup_errors.append(f"{path.name}: {cleanup_exc}")
            message = f"{type(exc).__name__}: {exc}"
            if cleanup_errors:
                message += "; cleanup failed: " + "; ".join(cleanup_errors)
            self.error.emit(message)
            self.completed.emit()

    def _raise_if_stopped(self) -> None:
        if self._stop_requested.is_set():
            raise AcquisitionStopped("Capture stopped by user")

    def _write_csv_tmp(self, path: Path, csv_text: str) -> None:
        header = f"timestamp,{datetime.now().isoformat()}\n"
        if self.request.frequency_mhz is not None:
            header += f"frequency_mhz,{self.request.frequency_mhz:.6f}\n"
        path.write_text(header + csv_text + "\n", encoding="utf-8")

    @staticmethod
    def _commit_pair(csv_tmp: Path, npz_tmp: Path, csv_path: Path, npz_path: Path) -> None:
        if not csv_tmp.is_file() or not npz_tmp.is_file():
            raise RuntimeError("Capture temporary files are incomplete")
        try:
            csv_tmp.replace(csv_path)
            npz_tmp.replace(npz_path)
        except Exception:
            csv_path.unlink(missing_ok=True)
            npz_path.unlink(missing_ok=True)
            raise
        if not csv_path.is_file() or not npz_path.is_file():
            csv_path.unlink(missing_ok=True)
            npz_path.unlink(missing_ok=True)
            raise RuntimeError("Capture pair commit verification failed")

    @staticmethod
    def _unlink(path: Path) -> None:
        path.unlink(missing_ok=True)

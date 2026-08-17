from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from delay_summary import rebuild_delay_summary, update_delay_summary
from file_pairing import is_capture_complete, remove_incomplete_pair
from instrument_errors import (
    InstrumentCommunicationError,
    N9020ACommunicationError,
    ScopeCommunicationError,
)
from n9020a_client import N9020AClient
from sds3104xhd_client import AcquisitionStopped, SDS3104XHDClient


logger = logging.getLogger(__name__)
STOP_CHECK_INTERVAL_SEC = 0.1


@dataclass(frozen=True)
class CaptureRequest:
    output_folder: Path
    index: int
    scope_enabled: bool
    frequency_mhz: float | None = None


class AcquisitionWorker(QObject):
    """Perform exactly one atomic capture, recovering a failed instrument in place."""

    finished = Signal(dict)
    error = Signal(str)
    status = Signal(str)
    completed = Signal()

    def __init__(
        self,
        n9020a_client: N9020AClient,
        scope_client: SDS3104XHDClient | None,
        request: CaptureRequest,
        max_sample_recovery_attempts: int = 5,
    ) -> None:
        super().__init__()
        self.n9020a_client = n9020a_client
        self.scope_client = scope_client
        self.request = request
        self.max_sample_recovery_attempts = max(0, max_sample_recovery_attempts)
        self._stop_requested = Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    @Slot()
    def capture_one(self) -> None:
        paths = self._capture_paths()
        recovery_count = 0
        try:
            paths["folder"].mkdir(parents=True, exist_ok=True)
            self._cleanup_capture(paths)
            while True:
                self._raise_if_stopped()
                try:
                    result = self._capture_attempt(paths)
                except InstrumentCommunicationError as exc:
                    self._cleanup_capture(paths)
                    if recovery_count >= self.max_sample_recovery_attempts:
                        raise RuntimeError(
                            "[ACQUISITION] maximum sample recovery attempts exceeded "
                            f"({self.max_sample_recovery_attempts}); last error: {exc}"
                        ) from exc
                    recovery_count += 1
                    self._recover_instrument(exc)
                    message = self._prefix(
                        "[ACQUISITION] restarting current sample "
                        f"(recovery {recovery_count}/{self.max_sample_recovery_attempts})"
                    )
                    logger.info(message)
                    self.status.emit("Acquisition: restarting current sample")
                    continue
                self.finished.emit(result)
                self.completed.emit()
                return
        except AcquisitionStopped:
            self._cleanup_capture(paths)
            self.completed.emit()
        except Exception as exc:
            cleanup_errors = self._cleanup_capture(paths, collect_errors=True)
            message = f"{type(exc).__name__}: {exc}"
            if cleanup_errors:
                message += "; cleanup failed: " + "; ".join(cleanup_errors)
            self.error.emit(message)
            self.completed.emit()

    def _capture_attempt(self, paths: dict[str, Path]) -> dict:
        folder = paths["folder"]
        if self.request.frequency_mhz is not None:
            self.n9020a_client.set_center_and_span_mhz(self.request.frequency_mhz, 0.0)

        if self.request.scope_enabled:
            if self.scope_client is None:
                raise RuntimeError("Scope is enabled but not connected")
            if is_capture_complete(folder, self.request.index, scope_enabled=True):
                raise FileExistsError(f"Capture pair already exists: {self.request.index:06d}")
            remove_incomplete_pair(folder, self.request.index)

            delay = self.scope_client.acquire_single_with_delay_retry(
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

            self._write_csv_tmp(paths["csv_tmp"], csv_text)
            save_started = time.monotonic()
            self.scope_client.save_npz(
                paths["npz_tmp"],
                waveform,
                self.request.index,
                delay.delay_s,
                delay_raw=delay.delay_raw,
                delay_valid=delay.delay_valid,
                delay_attempts=delay.attempts,
            )
            save_seconds = time.monotonic() - save_started
            self._commit_pair(
                paths["csv_tmp"], paths["npz_tmp"], paths["csv"], paths["npz"]
            )
            try:
                update_delay_summary(folder, self.request.index)
            except Exception:
                paths["csv"].unlink(missing_ok=True)
                paths["npz"].unlink(missing_ok=True)
                rebuild_delay_summary(folder)
                raise

            return {
                "index": self.request.index,
                "frequency_mhz": self.request.frequency_mhz,
                "csv_path": str(paths["csv"]),
                "npz_path": str(paths["npz"]),
                "scope": {
                    "point_count": waveform.point_count,
                    "min_voltage": float(waveform.voltage_v.min()),
                    "max_voltage": float(waveform.voltage_v.max()),
                    "single_seconds": delay.single_seconds,
                    "read_seconds": read_seconds,
                    "save_seconds": save_seconds,
                    "delay_s": delay.delay_s,
                    "delay_raw": delay.delay_raw,
                    "delay_valid": delay.delay_valid,
                    "delay_attempts": delay.attempts,
                    "advanced_measurement_type": "DELAY",
                },
            }

        if paths["csv"].exists():
            raise FileExistsError(f"CSV already exists: {paths['csv'].name}")
        csv_text = self.n9020a_client.fetch_csv_text()
        self._write_csv_tmp(paths["csv_tmp"], csv_text)
        paths["csv_tmp"].replace(paths["csv"])
        return {
            "index": self.request.index,
            "frequency_mhz": self.request.frequency_mhz,
            "csv_path": str(paths["csv"]),
            "npz_path": None,
            "scope": None,
        }

    def _recover_instrument(self, exc: InstrumentCommunicationError) -> None:
        if isinstance(exc, N9020ACommunicationError):
            client = self.n9020a_client
            label = "N9020A"
            possible_calibration = True
        elif isinstance(exc, ScopeCommunicationError):
            if self.scope_client is None:
                raise exc
            client = self.scope_client
            label = "SDS3104XHD"
            possible_calibration = False
        else:
            raise exc

        config = client.config
        failure_message = self._prefix(f"[{label}] Communication failure detected: {exc}")
        logger.warning(failure_message)
        if possible_calibration:
            logger.warning(
                self._prefix("[N9020A] Possible auto-calibration or temporary disconnect")
            )
        if not config.reconnect_enabled:
            raise exc

        client.disconnect()
        for attempt in range(1, config.reconnect_max_attempts + 1):
            self.status.emit(
                "N9020A: Calibration wait" if possible_calibration else "Scope: Reconnecting"
            )
            logger.info(
                self._prefix(
                    f"[{label}] waiting {config.reconnect_delay_sec:.1f} s before reconnect"
                )
            )
            self._sleep_interruptibly(config.reconnect_delay_sec)
            self.status.emit(f"{label}: Reconnecting")
            logger.info(
                self._prefix(
                    f"[{label}] reconnect attempt {attempt}/{config.reconnect_max_attempts}"
                )
            )
            try:
                identity = client.reconnect()
            except AcquisitionStopped:
                raise
            except Exception as reconnect_exc:
                logger.warning(
                    self._prefix(
                        f"[{label}] reconnect attempt {attempt}/{config.reconnect_max_attempts} "
                        f"failed: {reconnect_exc}"
                    )
                )
                if attempt == config.reconnect_max_attempts:
                    raise RuntimeError(
                        f"[{label}] reconnect failed after {config.reconnect_max_attempts} attempts"
                    ) from reconnect_exc
                continue

            logger.info(self._prefix(f"[{label}] connected: {identity or '<unknown>'}"))
            logger.info(self._prefix(f"[{label}] reconnect success"))
            if label == "SDS3104XHD":
                # connect() already performs this; repeating makes the recovery invariant explicit.
                client.configure_delay_measurement()
                logger.info(
                    self._prefix("[SDS3104XHD] advanced measurement configured: DELAY")
                )
            self.status.emit(f"{label}: Connected")
            return

    def _sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self._raise_if_stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._stop_requested.wait(min(remaining, STOP_CHECK_INTERVAL_SEC)):
                raise AcquisitionStopped("Capture stopped during reconnect wait")

    def _raise_if_stopped(self) -> None:
        if self._stop_requested.is_set():
            raise AcquisitionStopped("Capture stopped by user")

    def _prefix(self, message: str) -> str:
        return f"[{self.request.index:06d}] {message}"

    def _capture_paths(self) -> dict[str, Path]:
        folder = self.request.output_folder
        stem = f"{self.request.index:06d}"
        return {
            "folder": folder,
            "csv": folder / f"{stem}.csv",
            "npz": folder / f"{stem}.npz",
            "csv_tmp": folder / f"{stem}.csv.tmp",
            "npz_tmp": folder / f"{stem}.npz.tmp",
        }

    def _cleanup_capture(
        self,
        paths: dict[str, Path],
        *,
        collect_errors: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        for path in (paths["csv_tmp"], paths["npz_tmp"]):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                if collect_errors:
                    errors.append(f"{path.name}: {exc}")
                else:
                    raise
        if self.request.scope_enabled and paths["csv"].exists() != paths["npz"].exists():
            for path in (paths["csv"], paths["npz"]):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    if collect_errors:
                        errors.append(f"{path.name}: {exc}")
                    else:
                        raise
        return errors

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

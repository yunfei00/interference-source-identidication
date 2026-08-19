from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot

from file_pairing import is_capture_complete, remove_incomplete_group
from instrument_errors import (
    CommunicationFailureKind,
    InstrumentCommunicationError,
    N9020ACommunicationError,
    ScopeCommunicationError,
    classify_communication_failure,
)
from measurement_summary import rebuild_measurement_summary, update_measurement_summary
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
                        "[ACQUISITION] restarting current sample from beginning "
                        f"(recovery {recovery_count}/{self.max_sample_recovery_attempts})"
                    )
                    logger.info(message)
                    self.status.emit("Acquisition: restarting current sample from beginning")
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
                raise FileExistsError(f"Capture group already exists: {self.request.index:06d}")
            remove_incomplete_group(folder, self.request.index)

            # The analyzer capture is intentionally first. A recovery at either Scope
            # stage restarts this entire sample and fetches a fresh analyzer CSV.
            csv_text = self.n9020a_client.fetch_csv_text()
            self._raise_if_stopped()

            delay_result, delay_waveform, delay_read_seconds = self._capture_scope_stage(
                "DELAY",
                self.scope_client.config.delay_time_scale_sec,
            )
            cycles_result, cycles_waveform, cycles_read_seconds = self._capture_scope_stage(
                "CYCLES",
                self.scope_client.config.cycles_time_scale_sec,
            )
            self._raise_if_stopped()

            self._write_csv_tmp(paths["csv_tmp"], csv_text)
            save_started = time.monotonic()
            self.scope_client.save_npz(
                paths["delay_npz_tmp"],
                delay_waveform,
                self.request.index,
                measurement_result=delay_result,
            )
            self.scope_client.save_npz(
                paths["cycles_npz_tmp"],
                cycles_waveform,
                self.request.index,
                measurement_result=cycles_result,
            )
            save_seconds = time.monotonic() - save_started
            self._commit_group(
                (paths["csv_tmp"], paths["delay_npz_tmp"], paths["cycles_npz_tmp"]),
                (paths["csv"], paths["delay_npz"], paths["cycles_npz"]),
            )
            try:
                update_measurement_summary(folder, self.request.index)
            except Exception:
                for path in (paths["csv"], paths["delay_npz"], paths["cycles_npz"]):
                    path.unlink(missing_ok=True)
                rebuild_measurement_summary(folder)
                raise

            return {
                "index": self.request.index,
                "frequency_mhz": self.request.frequency_mhz,
                "csv_path": str(paths["csv"]),
                "npz_path": None,
                "delay_npz_path": str(paths["delay_npz"]),
                "cycles_npz_path": str(paths["cycles_npz"]),
                "scope": {
                    "delay": self._scope_result_dict(
                        delay_result, delay_waveform, delay_read_seconds
                    ),
                    "cycles": self._scope_result_dict(
                        cycles_result, cycles_waveform, cycles_read_seconds
                    ),
                    "point_count": delay_waveform.point_count + cycles_waveform.point_count,
                    "read_seconds": delay_read_seconds + cycles_read_seconds,
                    "save_seconds": save_seconds,
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

    def _capture_scope_stage(self, measurement_type: str, time_scale_sec: float):
        if self.scope_client is None:
            raise RuntimeError("Scope is enabled but not connected")
        self.scope_client.set_time_scale(time_scale_sec)
        self.scope_client.configure_advanced_measurement(measurement_type)
        result = self.scope_client.acquire_single_with_measurement_retry(
            measurement_type,
            capture_index=self.request.index,
            should_stop=self._stop_requested.is_set,
        )
        self._raise_if_stopped()
        read_started = time.monotonic()
        waveform = self.scope_client.read_waveform()
        return result, waveform, time.monotonic() - read_started

    @staticmethod
    def _scope_result_dict(result, waveform, read_seconds: float) -> dict:
        return {
            "measurement_type": result.measurement_type,
            "value": result.value,
            "unit": result.unit,
            "raw": result.raw_value,
            "valid": result.valid,
            "attempts": result.attempts,
            "single_seconds": result.single_seconds,
            "point_count": waveform.point_count,
            "min_voltage": float(waveform.voltage_v.min()),
            "max_voltage": float(waveform.voltage_v.max()),
            "read_seconds": read_seconds,
        }

    def _recover_instrument(self, exc: InstrumentCommunicationError) -> None:
        if isinstance(exc, N9020ACommunicationError):
            client = self.n9020a_client
            label = "N9020A"
            failure_kind = classify_communication_failure(exc)
        elif isinstance(exc, ScopeCommunicationError):
            if self.scope_client is None:
                raise exc
            client = self.scope_client
            label = "SDS3104XHD"
            failure_kind = CommunicationFailureKind.DISCONNECTED
        else:
            raise exc

        config = client.config
        failure_message = self._prefix(f"[{label}] Communication failure detected: {exc}")
        logger.warning(failure_message)
        if label == "N9020A":
            self._log_n9020a_failure(failure_kind)
        if not config.reconnect_enabled:
            raise exc

        self._disconnect_for_recovery(client, label)
        for attempt in range(1, config.reconnect_max_attempts + 1):
            delay = self._reconnect_delay(config, label, failure_kind)
            self.status.emit(self._recovery_status(label, failure_kind))
            logger.info(
                self._prefix(
                    f"[{label}] waiting {delay:.1f} s before reconnect"
                )
            )
            self._sleep_interruptibly(delay)
            self.status.emit(
                "Scope: Reconnecting"
                if label == "SDS3104XHD"
                else "N9020A: Reconnecting"
            )
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
                if label == "N9020A":
                    failure_kind = classify_communication_failure(reconnect_exc)
                    self._log_n9020a_failure(failure_kind)
                logger.warning(
                    self._prefix(
                        f"[{label}] reconnect attempt {attempt}/{config.reconnect_max_attempts} "
                        f"failed: {reconnect_exc}"
                    )
                )
                if attempt == config.reconnect_max_attempts:
                    self.status.emit(
                        "Scope: Reconnect failed"
                        if label == "SDS3104XHD"
                        else "N9020A: Reconnect failed"
                    )
                    raise RuntimeError(
                        f"[{label}] reconnect failed after {config.reconnect_max_attempts} attempts"
                    ) from reconnect_exc
                continue

            logger.info(self._prefix(f"[{label}] connected: {identity or '<unknown>'}"))
            logger.info(self._prefix(f"[{label}] reconnect success"))
            self.status.emit(
                "Scope: Reconnect success"
                if label == "SDS3104XHD"
                else "N9020A: Connected"
            )
            return

    def _log_n9020a_failure(self, failure_kind: CommunicationFailureKind) -> None:
        if failure_kind == CommunicationFailureKind.TIMEOUT:
            message = "[N9020A] timeout / possible auto-calibration"
        elif failure_kind == CommunicationFailureKind.DISCONNECTED:
            message = "[N9020A] network disconnect detected"
        else:
            message = "[N9020A] communication unavailable"
        logger.warning(self._prefix(message))

    @staticmethod
    def _reconnect_delay(config, label: str, failure_kind: CommunicationFailureKind) -> float:
        if label != "N9020A":
            return float(config.reconnect_delay_sec)
        if failure_kind == CommunicationFailureKind.TIMEOUT:
            return float(getattr(config, "calibration_wait_sec", config.reconnect_delay_sec))
        return float(
            getattr(config, "disconnect_reconnect_delay_sec", config.reconnect_delay_sec)
        )

    @staticmethod
    def _recovery_status(label: str, failure_kind: CommunicationFailureKind) -> str:
        if label == "SDS3104XHD":
            return "Scope: Disconnected"
        if failure_kind == CommunicationFailureKind.TIMEOUT:
            return "N9020A: Possible calibration"
        if failure_kind == CommunicationFailureKind.DISCONNECTED:
            return "N9020A: Disconnected"
        return "N9020A: Communication unavailable"

    @staticmethod
    def _disconnect_for_recovery(client, label: str) -> None:
        if label == "N9020A":
            try:
                client.disconnect(return_to_local=False)
            except TypeError:
                client.disconnect()
        else:
            client.disconnect()

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
            "delay_npz": folder / f"{stem}_delay.npz",
            "cycles_npz": folder / f"{stem}_cycles.npz",
            "csv_tmp": folder / f"{stem}.csv.tmp",
            "delay_npz_tmp": folder / f"{stem}_delay.npz.tmp",
            "cycles_npz_tmp": folder / f"{stem}_cycles.npz.tmp",
        }

    def _cleanup_capture(
        self,
        paths: dict[str, Path],
        *,
        collect_errors: bool = False,
    ) -> list[str]:
        errors: list[str] = []
        temporary_paths = (paths["csv_tmp"], paths["delay_npz_tmp"], paths["cycles_npz_tmp"])
        formal_paths = (paths["csv"], paths["delay_npz"], paths["cycles_npz"])
        for path in temporary_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                if collect_errors:
                    errors.append(f"{path.name}: {exc}")
                else:
                    raise
        present = sum(path.is_file() for path in formal_paths)
        if self.request.scope_enabled and 0 < present < len(formal_paths):
            for path in formal_paths:
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
    def _commit_group(temporary_paths: tuple[Path, ...], formal_paths: tuple[Path, ...]) -> None:
        if len(temporary_paths) != 3 or len(formal_paths) != 3:
            raise ValueError("A Scope capture group must contain exactly three files")
        if not all(path.is_file() for path in temporary_paths):
            raise RuntimeError("Capture temporary files are incomplete")
        try:
            for temporary, formal in zip(temporary_paths, formal_paths):
                temporary.replace(formal)
        except Exception:
            for formal in formal_paths:
                formal.unlink(missing_ok=True)
            raise
        if not all(path.is_file() for path in formal_paths):
            for formal in formal_paths:
                formal.unlink(missing_ok=True)
            raise RuntimeError("Capture group commit verification failed")

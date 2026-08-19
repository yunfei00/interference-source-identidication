from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from acquisition_worker import AcquisitionWorker, CaptureRequest
from instrument_errors import (
    CommunicationFailureKind,
    N9020ACommunicationError,
    ScopeCommunicationError,
    classify_communication_failure,
)
from n9020a_client import N9020AClient, N9020AConfig
from sds3104xhd_client import AdvancedMeasurementResult


class _Waveform:
    point_count = 2
    voltage_v = np.asarray([-1.0, 1.0], dtype=np.float32)
    time_s = np.asarray([0.0, 1.0], dtype=np.float64)
    adc = np.asarray([0, 1], dtype=np.int16)


class _RecoveringN9020A:
    def __init__(self, failure: BaseException | None = None, reconnect_failures: int = 0) -> None:
        self.config = SimpleNamespace(
            reconnect_enabled=True,
            calibration_wait_sec=15.0,
            disconnect_reconnect_delay_sec=2.0,
            reconnect_delay_sec=15.0,
            reconnect_max_attempts=5,
        )
        self.failure = failure
        self.reconnect_failures = reconnect_failures
        self.fetches = 0
        self.reconnects = 0
        self.disconnect_args: list[bool] = []

    def set_center_and_span_mhz(self, _center: float, _span: float) -> None:
        pass

    def fetch_csv_text(self) -> str:
        self.fetches += 1
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise N9020ACommunicationError("MMEM:DATA?", failure)
        return "frequency,power\n1,-20"

    def disconnect(self, return_to_local: bool = False) -> None:
        self.disconnect_args.append(return_to_local)

    def reconnect(self) -> str:
        self.reconnects += 1
        if self.reconnects <= self.reconnect_failures:
            raise N9020ACommunicationError("*IDN?", TimeoutError("still unavailable"))
        return "Keysight Technologies,N9020A,TEST,1.0"


class _RecoveringScope:
    def __init__(self, waveform_failures: int = 0) -> None:
        self.config = SimpleNamespace(
            reconnect_enabled=True,
            reconnect_delay_sec=2.0,
            reconnect_max_attempts=5,
            delay_time_scale_sec=5e-7,
            cycles_time_scale_sec=1e-4,
        )
        self.waveform_failures = waveform_failures
        self.singles: list[str] = []
        self.waveform_reads = 0
        self.reconnects = 0
        self.disconnects = 0

    def set_time_scale(self, _seconds: float) -> None:
        pass

    def configure_advanced_measurement(self, _measurement_type: str) -> None:
        pass

    def acquire_single_with_measurement_retry(self, measurement_type: str, **_kwargs):
        self.singles.append(measurement_type)
        value = 100e-9 if measurement_type == "DELAY" else 20.0
        return AdvancedMeasurementResult(
            measurement_type,
            value,
            f"{value:g}",
            True,
            1,
            0.05,
            "s" if measurement_type == "DELAY" else "count",
        )

    def read_waveform(self) -> _Waveform:
        self.waveform_reads += 1
        if self.waveform_reads <= self.waveform_failures:
            raise ScopeCommunicationError("WAV:DATA?", ConnectionResetError("connection reset"))
        return _Waveform()

    def save_npz(self, path: Path, waveform, index: int, *, measurement_result) -> None:
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

    def disconnect(self) -> None:
        self.disconnects += 1

    def reconnect(self) -> str:
        self.reconnects += 1
        return "SIGLENT,SDS3104X HD,TEST,1.0"


def test_failure_classification_distinguishes_timeout_network_and_unknown() -> None:
    assert classify_communication_failure(TimeoutError("timeout")) == CommunicationFailureKind.TIMEOUT
    assert (
        classify_communication_failure(ConnectionResetError("reset"))
        == CommunicationFailureKind.DISCONNECTED
    )
    assert classify_communication_failure(RuntimeError("odd")) == CommunicationFailureKind.UNKNOWN


def test_n9020a_timeout_uses_calibration_wait_then_restarts_sample(tmp_path: Path) -> None:
    n9020a = _RecoveringN9020A(TimeoutError("VISA timeout"))
    scope = _RecoveringScope()
    worker = AcquisitionWorker(n9020a, scope, CaptureRequest(tmp_path, 1, True))  # type: ignore[arg-type]
    waits: list[float] = []
    worker._sleep_interruptibly = waits.append  # type: ignore[method-assign]
    statuses: list[str] = []
    errors: list[str] = []
    worker.status.connect(statuses.append)
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert waits == [15.0]
    assert "N9020A: Possible calibration" in statuses
    assert n9020a.fetches == 2
    assert scope.singles == ["DELAY", "CYCLES"]
    assert n9020a.disconnect_args == [False]


def test_n9020a_network_disconnect_uses_short_delay_not_calibration(tmp_path: Path) -> None:
    n9020a = _RecoveringN9020A(ConnectionResetError("connection reset"))
    worker = AcquisitionWorker(n9020a, None, CaptureRequest(tmp_path, 1, False))  # type: ignore[arg-type]
    waits: list[float] = []
    statuses: list[str] = []
    worker._sleep_interruptibly = waits.append  # type: ignore[method-assign]
    worker.status.connect(statuses.append)

    worker.capture_one()

    assert waits == [2.0]
    assert "N9020A: Disconnected" in statuses
    assert not any("calibration" in status.casefold() for status in statuses)


def test_scope_reconnect_success_restarts_entire_sample_without_final_error(tmp_path: Path) -> None:
    n9020a = _RecoveringN9020A()
    scope = _RecoveringScope(waveform_failures=1)
    worker = AcquisitionWorker(n9020a, scope, CaptureRequest(tmp_path, 1, True))  # type: ignore[arg-type]
    waits: list[float] = []
    errors: list[str] = []
    worker._sleep_interruptibly = waits.append  # type: ignore[method-assign]
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert waits == [2.0]
    assert scope.reconnects == 1
    assert n9020a.fetches == 2
    assert scope.singles == ["DELAY", "DELAY", "CYCLES"]
    assert (tmp_path / "000001_delay.npz").is_file()
    assert (tmp_path / "000001_cycles.npz").is_file()


class _Instrument:
    def __init__(self, local_failure: bool = False) -> None:
        self.commands: list[str] = []
        self.closed = False
        self.local_failure = local_failure

    def write(self, command: str) -> None:
        self.commands.append(command)
        if self.local_failure:
            raise OSError("network gone")

    def close(self) -> None:
        self.closed = True


def test_user_disconnect_sends_local_before_close() -> None:
    client = N9020AClient(N9020AConfig("TCPIP0::test::INSTR", 1000))
    instrument = _Instrument()
    client._inst = instrument
    client.disconnect(return_to_local=True)
    assert instrument.commands == ["SYST:LOC"]
    assert instrument.closed


def test_recovery_disconnect_does_not_send_local() -> None:
    client = N9020AClient(N9020AConfig("TCPIP0::test::INSTR", 1000))
    instrument = _Instrument()
    client._inst = instrument
    client.disconnect(return_to_local=False)
    assert instrument.commands == []
    assert instrument.closed


def test_local_failure_still_closes_session(caplog) -> None:
    client = N9020AClient(N9020AConfig("TCPIP0::test::INSTR", 1000))
    instrument = _Instrument(local_failure=True)
    client._inst = instrument
    with caplog.at_level("WARNING"):
        client.disconnect(return_to_local=True)
    assert instrument.closed
    assert "unable to restore Local control" in caplog.text

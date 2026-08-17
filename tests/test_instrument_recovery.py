from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from acquisition_worker import AcquisitionWorker, CaptureRequest
from instrument_errors import N9020ACommunicationError, ScopeCommunicationError
from n9020a_client import N9020AClient, N9020AConfig
from sds3104xhd_client import DelayAcquisitionResult, SDS3104XHDClient, SDS3104XHDConfig


class _Waveform:
    point_count = 2
    voltage_v = np.asarray([-1.0, 1.0], dtype=np.float32)


class _RecoveringN9020A:
    def __init__(self, *, fail_fetches: int = 1, reconnect_failures: int = 0) -> None:
        self.config = SimpleNamespace(
            reconnect_enabled=True,
            reconnect_delay_sec=15.0,
            reconnect_max_attempts=5,
        )
        self.fail_fetches = fail_fetches
        self.reconnect_failures = reconnect_failures
        self.fetches = 0
        self.reconnects = 0
        self.disconnects = 0

    def set_center_and_span_mhz(self, _center: float, _span: float) -> None:
        return None

    def fetch_csv_text(self) -> str:
        self.fetches += 1
        if self.fetches <= self.fail_fetches:
            raise N9020ACommunicationError("MMEM:DATA?", TimeoutError("VISA timeout"))
        return "frequency,power\n1,-20"

    def disconnect(self) -> None:
        self.disconnects += 1

    def reconnect(self) -> str:
        self.reconnects += 1
        if self.reconnects <= self.reconnect_failures:
            raise N9020ACommunicationError("*IDN?", TimeoutError("still calibrating"))
        return "Keysight Technologies,N9020A,TEST,1.0"


class _RecoveringScope:
    def __init__(self, *, waveform_failures: int = 0) -> None:
        self.config = SimpleNamespace(
            reconnect_enabled=True,
            reconnect_delay_sec=2.0,
            reconnect_max_attempts=5,
        )
        self.waveform_failures = waveform_failures
        self.singles = 0
        self.waveform_reads = 0
        self.reconnects = 0
        self.configurations = 0

    def acquire_single_with_delay_retry(self, *, capture_index: int, should_stop):
        assert capture_index == 1 and not should_stop()
        self.singles += 1
        return DelayAcquisitionResult(100e-9, "1.0E-7", True, 1, 0.05)

    def read_waveform(self) -> _Waveform:
        self.waveform_reads += 1
        if self.waveform_reads <= self.waveform_failures:
            raise ScopeCommunicationError("WAV:DATA?", OSError("connection reset"))
        return _Waveform()

    def save_npz(self, path: Path, _waveform, index: int, delay_s: float, **metadata) -> None:
        with path.open("wb") as output:
            np.savez(
                output,
                index=np.asarray(index, dtype=np.int32),
                delay_s=np.asarray(delay_s, dtype=np.float64),
                delay_raw=np.asarray(metadata["delay_raw"]),
                delay_valid=np.asarray(metadata["delay_valid"], dtype=np.bool_),
                delay_attempts=np.asarray(metadata["delay_attempts"], dtype=np.int32),
                advanced_measurement_type=np.asarray("DELAY"),
            )

    def disconnect(self) -> None:
        return None

    def reconnect(self) -> str:
        self.reconnects += 1
        return "SIGLENT,SDS3104X HD,TEST,1.0"

    def configure_delay_measurement(self) -> None:
        self.configurations += 1


def test_driver_classifies_transport_errors_but_not_value_errors() -> None:
    class TimeoutInstrument:
        def write(self, _command: str) -> None:
            raise TimeoutError("timeout")

    n9020a = N9020AClient(N9020AConfig("TCPIP0::test::INSTR", 1000))
    n9020a._inst = TimeoutInstrument()
    with pytest.raises(N9020ACommunicationError):
        n9020a.write("*OPC?")

    scope = SDS3104XHDClient(
        SDS3104XHDConfig("test", "C1", 1000, 1, 1024, 0.01, 0, 0, 3)
    )
    scope._inst = SimpleNamespace(query=lambda _command: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError, match="bad"):
        scope.query("MEAS:ADV:P1:VAL?")


def test_n9020a_failure_waits_15_seconds_then_restarts_entire_same_sample(tmp_path) -> None:
    n9020a = _RecoveringN9020A()
    scope = _RecoveringScope()
    worker = AcquisitionWorker(
        n9020a, scope, CaptureRequest(tmp_path, 1, True), max_sample_recovery_attempts=5
    )  # type: ignore[arg-type]
    waits: list[float] = []
    worker._sleep_interruptibly = waits.append  # type: ignore[method-assign]
    results: list[dict] = []
    worker.finished.connect(results.append)

    worker.capture_one()

    assert waits == [15.0]
    assert n9020a.reconnects == 1
    assert scope.singles == 2
    assert results[0]["index"] == 1
    assert (tmp_path / "000001.csv").is_file()
    assert (tmp_path / "000001.npz").is_file()
    assert not (tmp_path / "000002.csv").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_scope_failure_reconnects_reconfigures_delay_and_restarts_single(tmp_path) -> None:
    n9020a = _RecoveringN9020A(fail_fetches=0)
    scope = _RecoveringScope(waveform_failures=1)
    worker = AcquisitionWorker(n9020a, scope, CaptureRequest(tmp_path, 1, True))  # type: ignore[arg-type]
    waits: list[float] = []
    worker._sleep_interruptibly = waits.append  # type: ignore[method-assign]
    errors: list[str] = []
    worker.error.connect(errors.append)

    worker.capture_one()

    assert not errors
    assert waits == [2.0]
    assert scope.reconnects == 1
    assert scope.configurations == 1
    assert scope.singles == 2
    assert n9020a.fetches == 2
    assert (tmp_path / "delay_summary.csv").is_file()


def test_reconnect_retries_to_configured_max_without_real_sleep(tmp_path) -> None:
    n9020a = _RecoveringN9020A(fail_fetches=10, reconnect_failures=10)
    n9020a.config.reconnect_delay_sec = 0
    n9020a.config.reconnect_max_attempts = 3
    worker = AcquisitionWorker(
        n9020a, None, CaptureRequest(tmp_path, 1, False), max_sample_recovery_attempts=5
    )  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.capture_one()
    assert n9020a.reconnects == 3
    assert errors and "reconnect failed after 3 attempts" in errors[0]
    assert not list(tmp_path.glob("000001*"))


def test_sample_recovery_limit_prevents_infinite_restart(tmp_path) -> None:
    n9020a = _RecoveringN9020A(fail_fetches=99)
    n9020a.config.reconnect_delay_sec = 0
    scope = _RecoveringScope()
    worker = AcquisitionWorker(
        n9020a, scope, CaptureRequest(tmp_path, 1, True), max_sample_recovery_attempts=2
    )  # type: ignore[arg-type]
    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.capture_one()
    assert scope.singles == 3
    assert n9020a.reconnects == 2
    assert errors and "maximum sample recovery attempts exceeded" in errors[0]
    assert not list(tmp_path.glob("000001*"))


def test_stop_interrupts_long_reconnect_wait_quickly(tmp_path) -> None:
    n9020a = _RecoveringN9020A(fail_fetches=99)
    scope = _RecoveringScope()
    worker = AcquisitionWorker(n9020a, scope, CaptureRequest(tmp_path, 1, True))  # type: ignore[arg-type]
    thread = threading.Thread(target=worker.capture_one)
    started = time.monotonic()
    thread.start()
    time.sleep(0.05)
    worker.request_stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 0.5
    assert not list(tmp_path.glob("000001*"))

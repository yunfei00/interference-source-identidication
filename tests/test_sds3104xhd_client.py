from __future__ import annotations

import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import sds3104xhd_client as scope_module

from sds3104xhd_client import (
    AdvancedMeasurementResult,
    AcquisitionStopped,
    PREAMBLE_MIN_BYTES,
    SDS3104XHDClient,
    SDS3104XHDConfig,
    ScopeWaveform,
    convert_adc_to_voltage,
    decode_word_data,
    parse_ieee4882_binary_block,
    parse_delay_value,
    parse_preamble,
)
from time_formatting import format_time_value


def _scope_config(**overrides: object) -> SDS3104XHDConfig:
    values: dict[str, object] = {
        "ip": "192.0.2.1",
        "channel": "C1",
        "timeout_ms": 60_000,
        "single_timeout_sec": 30.0,
        "chunk_size": 20 * 1024 * 1024,
        "trigger_poll_interval_sec": 0.05,
        "delay_settle_delay_sec": 0.0,
        "delay_retry_delay_sec": 0.0,
        "delay_max_attempts": 3,
    }
    values.update(overrides)
    return SDS3104XHDConfig(**values)  # type: ignore[arg-type]


def _make_preamble() -> bytes:
    payload = bytearray(PREAMBLE_MIN_BYTES)
    payload[:8] = b"WAVEDESC"
    struct.pack_into("<i", payload, 0x3C, PREAMBLE_MIN_BYTES)
    struct.pack_into("<i", payload, 0x74, 4000)
    struct.pack_into("<i", payload, 0x84, 0)
    struct.pack_into("<i", payload, 0x88, 1)
    struct.pack_into("<f", payload, 0x9C, 0.1)
    struct.pack_into("<f", payload, 0xA0, 0.0)
    struct.pack_into("<f", payload, 0xA4, 6800.0)
    struct.pack_into("<h", payload, 0xAC, 16)
    struct.pack_into("<f", payload, 0xB0, 2e-9)
    struct.pack_into("<d", payload, 0xB4, 1e-6)
    struct.pack_into("<h", payload, 0x144, 14)
    struct.pack_into("<f", payload, 0x148, 10.0)
    return bytes(payload)


def test_ieee_binary_block_parser() -> None:
    payload = _make_preamble()
    raw = b"#9" + f"{len(payload):09d}".encode("ascii") + payload + b"\r\n"
    assert parse_ieee4882_binary_block(raw) == payload


def test_ieee_binary_block_rejects_truncated_payload() -> None:
    with pytest.raises(ValueError, match="expected 10 payload bytes"):
        parse_ieee4882_binary_block(b"#210short")


def test_preamble_parser() -> None:
    preamble = parse_preamble(_make_preamble())
    assert preamble.data_bytes == PREAMBLE_MIN_BYTES
    assert preamble.point_num == 4000
    assert preamble.adc_bit == 16
    assert preamble.vdiv == pytest.approx(1.0)
    assert preamble.offset == pytest.approx(0.0)
    assert preamble.code == pytest.approx(6800.0)
    assert preamble.interval == pytest.approx(2e-9)
    assert preamble.delay == pytest.approx(1e-6)
    assert preamble.tdiv == pytest.approx(10e-6)


def test_short_4000_point_waveform_keeps_3999_actual_points() -> None:
    raw = np.arange(3999, dtype="<i2").tobytes()
    with pytest.warns(RuntimeWarning, match="expected 4000, received 3999"):
        adc = decode_word_data(raw, expected_point_count=4000)
    assert adc.dtype == np.dtype("int16")
    assert adc.size == 3999


def test_voltage_conversion_matches_verified_scope_value() -> None:
    voltage = convert_adc_to_voltage(
        np.asarray([20368], dtype=np.int16),
        vdiv_raw=0.1,
        probe=10.0,
        code=6800.0,
        offset_raw=0.0,
    )
    assert voltage.dtype == np.dtype("float32")
    assert float(voltage[0]) == pytest.approx(2.995294, abs=1e-6)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.3447E-07", 1.3447e-07),
        ("****", None),
        ("", None),
        ("---", None),
        ("not-a-number", None),
    ],
)
def test_delay_parser(raw: str, expected: float | None) -> None:
    result = parse_delay_value(raw)
    if expected is None:
        assert np.isnan(result)
    else:
        assert result == pytest.approx(expected)


def test_delay_configuration_command(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    commands: list[str] = []
    monkeypatch.setattr(client, "write", commands.append)

    client.configure_delay_measurement()

    assert commands == ["MEAS:ADV:P1:TYPE DELAY"]


def test_delay_and_cycles_time_scale_commands_are_exact(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    commands: list[str] = []
    monkeypatch.setattr(client, "write", commands.append)
    client.set_time_scale(5e-7)
    client.set_time_scale(1e-4)
    assert commands == [":TIM:SCAL 5.00E-7", ":TIM:SCAL 1.00E-4"]


def test_cycles_configuration_and_allowlist(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    commands: list[str] = []
    monkeypatch.setattr(client, "write", commands.append)
    client.configure_advanced_measurement("cycles")
    assert commands == ["MEAS:ADV:P1:TYPE CYCLES"]
    with pytest.raises(ValueError, match="Unsupported"):
        client.configure_advanced_measurement("FREQ")


def test_connect_initializes_channel_without_fixing_measurement_type(monkeypatch) -> None:
    class Instrument:
        def __init__(self) -> None:
            self.commands: list[str] = []
            self.timeout = 0
            self.chunk_size = 0

        def query(self, command: str) -> str:
            assert command == "*IDN?"
            return "SIGLENT,SDS3104X HD,TEST,1.0"

        def write(self, command: str) -> None:
            self.commands.append(command)

        def close(self) -> None:
            pass

    instrument = Instrument()
    manager = SimpleNamespace(
        open_resource=lambda _resource: instrument,
        close=lambda: None,
    )
    monkeypatch.setitem(
        sys.modules,
        "pyvisa",
        SimpleNamespace(ResourceManager=lambda: manager),
    )
    client = SDS3104XHDClient(_scope_config())

    client.connect()

    assert instrument.commands == [":WAVeform:SOURce C1"]
    assert not any("MEAS:ADV:P1:TYPE" in command for command in instrument.commands)


def test_cycles_retry_result_uses_count_not_seconds(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    monkeypatch.setattr(client, "acquire_single", lambda: 0.1)
    monkeypatch.setattr(client, "query", lambda _command: "23.5")
    result = client.acquire_single_with_measurement_retry("CYCLES")
    assert result.measurement_type == "CYCLES"
    assert result.value == pytest.approx(23.5)
    assert result.unit == "count"
    assert np.isnan(result.delay_s)


def test_delay_read_uses_advanced_p1_query(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    queries: list[str] = []

    def query(command: str) -> str:
        queries.append(command)
        return "1.3447E-07"

    monkeypatch.setattr(client, "query", query)
    assert client.read_delay_value(25) == pytest.approx(1.3447e-07)
    assert queries == ["MEAS:ADV:P1:VAL?"]


def test_delay_unavailable_logs_warning(monkeypatch, caplog) -> None:
    client = SDS3104XHDClient(_scope_config())
    monkeypatch.setattr(client, "query", lambda _command: "****")

    with caplog.at_level("WARNING"):
        value = client.read_delay_value(25)

    assert np.isnan(value)
    assert "[000025] [SDS3104XHD] DELAY unavailable: ****" in caplog.text


@pytest.mark.parametrize(
    ("responses", "expected_attempts"),
    [
        (["1.3447E-07"], 1),
        (["****", "1.3447E-07"], 2),
        (["****", "****", "1.3447E-07"], 3),
    ],
)
def test_single_is_rearmed_until_delay_is_valid(
    monkeypatch,
    responses: list[str],
    expected_attempts: int,
) -> None:
    client = SDS3104XHDClient(_scope_config())
    singles: list[int] = []
    remaining = iter(responses)
    monkeypatch.setattr(client, "acquire_single", lambda: singles.append(1) or 0.1)
    monkeypatch.setattr(client, "query", lambda _command: next(remaining))

    result = client.acquire_single_with_delay_retry(capture_index=1)

    assert len(singles) == expected_attempts
    assert result.attempts == expected_attempts
    assert result.delay_valid is True
    assert result.delay_s == pytest.approx(1.3447e-7)
    assert result.delay_raw == "1.3447E-07"


def test_three_invalid_delay_frames_keep_last_acquisition(monkeypatch, caplog) -> None:
    client = SDS3104XHDClient(_scope_config())
    frames: list[int] = []
    monkeypatch.setattr(
        client,
        "acquire_single",
        lambda: frames.append(len(frames) + 1) or float(frames[-1]),
    )
    monkeypatch.setattr(client, "query", lambda _command: "****")

    with caplog.at_level("INFO"):
        result = client.acquire_single_with_delay_retry(capture_index=1)

    assert frames == [1, 2, 3]
    assert result.attempts == 3
    assert result.delay_valid is False
    assert np.isnan(result.delay_s)
    assert result.delay_raw == "****"
    assert result.single_seconds == pytest.approx(3.0)
    assert "[000001] [SDS3104XHD] Scope Single attempt 3/3" in caplog.text
    assert "DELAY unavailable after 3 Single attempts; using last acquisition" in caplog.text


def test_delay_query_communication_error_is_not_retried(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    singles: list[int] = []
    monkeypatch.setattr(client, "acquire_single", lambda: singles.append(1) or 0.1)

    def timeout(_command: str) -> str:
        raise TimeoutError("VISA timeout")

    monkeypatch.setattr(client, "query", timeout)

    with pytest.raises(TimeoutError, match="VISA timeout"):
        client.acquire_single_with_delay_retry(capture_index=1)
    assert len(singles) == 1


def test_retry_log_shows_each_new_single_and_final_value(monkeypatch, caplog) -> None:
    client = SDS3104XHDClient(_scope_config())
    responses = iter(["****", "****", "1.3447E-07"])
    monkeypatch.setattr(client, "acquire_single", lambda: 0.1)
    monkeypatch.setattr(client, "query", lambda _command: next(responses))

    with caplog.at_level("INFO"):
        result = client.acquire_single_with_delay_retry(capture_index=1)

    assert result.attempts == 3
    assert "[000001] [SDS3104XHD] Scope Single attempt 1/3" in caplog.text
    assert "[000001] [SDS3104XHD] Scope Single attempt 2/3" in caplog.text
    assert "[000001] [SDS3104XHD] Scope Single attempt 3/3" in caplog.text
    assert caplog.text.count("[000001] [SDS3104XHD] DELAY unavailable: ****") == 2
    assert "[000001] [SDS3104XHD] DELAY = 134.47 ns" in caplog.text


def test_retry_checks_stop_after_single_before_query(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config())
    singles: list[int] = []
    queries: list[str] = []
    stop_checks = iter([False, True])
    monkeypatch.setattr(client, "acquire_single", lambda: singles.append(1) or 0.1)
    monkeypatch.setattr(client, "query", lambda command: queries.append(command) or "****")

    with pytest.raises(AcquisitionStopped):
        client.acquire_single_with_delay_retry(
            capture_index=1,
            should_stop=lambda: next(stop_checks),
        )
    assert len(singles) == 1
    assert queries == []


def test_settle_and_retry_delays_are_called_without_real_sleep(monkeypatch) -> None:
    client = SDS3104XHDClient(
        _scope_config(delay_settle_delay_sec=0.2, delay_retry_delay_sec=0.3)
    )
    responses = iter(["****", "1.3447E-07"])
    waits: list[float] = []
    monkeypatch.setattr(client, "acquire_single", lambda: 0.087)
    monkeypatch.setattr(client, "query", lambda _command: next(responses))
    monkeypatch.setattr(
        client,
        "_sleep_interruptibly",
        lambda seconds, _should_stop: waits.append(seconds),
    )

    result = client.acquire_single_with_delay_retry(capture_index=1)

    assert result.attempts == 2
    assert waits == pytest.approx([0.2, 0.3, 0.2])


def test_trigger_polling_uses_configured_interval(monkeypatch) -> None:
    client = SDS3104XHDClient(_scope_config(trigger_poll_interval_sec=0.123))
    statuses = iter(["RUN", "STOP"])
    clock = iter([0.0, 0.1, 0.2])
    sleeps: list[float] = []
    monkeypatch.setattr(client, "write", lambda _command: None)
    monkeypatch.setattr(client, "query", lambda _command: next(statuses))
    monkeypatch.setattr(scope_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scope_module.time, "sleep", sleeps.append)

    elapsed = client.acquire_single()

    assert elapsed == pytest.approx(0.2)
    assert sleeps == [pytest.approx(0.123)]


def test_time_value_formatting() -> None:
    assert format_time_value(1.3447e-7) == "134.47 ns"
    assert format_time_value(2.53e-6) == "2.53 μs"
    assert format_time_value(2.3e-6) == "2.30 μs"
    assert format_time_value(0.0012) == "1.20 ms"
    assert format_time_value(float("nan")) == "N/A"


def test_npz_save_keeps_tmp_name_and_required_dtypes(tmp_path) -> None:
    preamble = parse_preamble(_make_preamble())
    adc = np.asarray([-2, 0, 2], dtype=np.int16)
    waveform = ScopeWaveform(
        adc=adc,
        time_s=np.asarray([0.0, 1.0, 2.0], dtype=np.float64),
        voltage_v=np.asarray([-1.0, 0.0, 1.0], dtype=np.float32),
        preamble=preamble,
    )
    client = SDS3104XHDClient(_scope_config())
    output = tmp_path / "000001.npz.tmp"

    client.save_npz(
        output,
        waveform,
        index=1,
        delay_s=1.3447e-7,
        delay_raw="1.3447E-07",
        delay_valid=True,
        delay_attempts=3,
    )

    assert output.is_file()
    assert not (tmp_path / "000001.npz.tmp.npz").exists()
    with np.load(output) as saved:
        assert saved["index"].dtype == np.dtype("int32")
        assert saved["time_s"].dtype == np.dtype("float64")
        assert saved["voltage_v"].dtype == np.dtype("float32")
        assert saved["adc"].dtype == np.dtype("int16")
        assert saved["delay_s"].dtype == np.dtype("float64")
        assert float(saved["delay_s"]) == pytest.approx(1.3447e-7)
        assert saved["delay_raw"].dtype.kind == "U"
        assert str(saved["delay_raw"]) == "1.3447E-07"
        assert saved["delay_valid"].dtype == np.dtype("bool")
        assert bool(saved["delay_valid"])
        assert saved["delay_attempts"].dtype == np.dtype("int32")
        assert int(saved["delay_attempts"]) == 3
        assert str(saved["advanced_measurement_type"]) == "DELAY"
        assert "positive_pulse_width_s" not in saved
        assert int(saved["point_count"]) == 3
        assert str(saved["channel"]) == "C1"


def test_cycles_npz_uses_generic_count_metadata(tmp_path) -> None:
    preamble = parse_preamble(_make_preamble())
    waveform = ScopeWaveform(
        adc=np.asarray([0, 1], dtype=np.int16),
        time_s=np.asarray([0.0, 1.0], dtype=np.float64),
        voltage_v=np.asarray([0.0, 1.0], dtype=np.float32),
        preamble=preamble,
    )
    output = tmp_path / "000001_cycles.npz.tmp"
    client = SDS3104XHDClient(_scope_config())
    client.save_npz(
        output,
        waveform,
        1,
        measurement_result=AdvancedMeasurementResult(
            "CYCLES", 23.5, "2.35E1", True, 2, 0.1, "count"
        ),
    )
    with np.load(output, allow_pickle=False) as saved:
        assert str(saved["measurement_type"]) == "CYCLES"
        assert str(saved["measurement_unit"]) == "count"
        assert float(saved["measurement_value"]) == pytest.approx(23.5)
        assert "delay_s" not in saved

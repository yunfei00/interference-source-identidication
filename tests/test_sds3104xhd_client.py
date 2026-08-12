from __future__ import annotations

import struct

import numpy as np
import pytest

from sds3104xhd_client import (
    PREAMBLE_MIN_BYTES,
    SDS3104XHDClient,
    SDS3104XHDConfig,
    ScopeWaveform,
    convert_adc_to_voltage,
    decode_word_data,
    parse_ieee4882_binary_block,
    parse_positive_pulse_width,
    parse_preamble,
)
from time_formatting import format_time_value


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
def test_positive_pulse_width_parser(raw: str, expected: float | None) -> None:
    result = parse_positive_pulse_width(raw)
    if expected is None:
        assert np.isnan(result)
    else:
        assert result == pytest.approx(expected)


def test_positive_pulse_width_configuration_command(monkeypatch) -> None:
    client = SDS3104XHDClient(SDS3104XHDConfig())
    commands: list[str] = []
    monkeypatch.setattr(client, "write", commands.append)

    client.configure_positive_pulse_width()

    assert commands == ["MEAS:ADV:P1:TYPE PWID"]


def test_positive_pulse_width_read_uses_advanced_p1_query(monkeypatch) -> None:
    client = SDS3104XHDClient(SDS3104XHDConfig())
    queries: list[str] = []

    def query(command: str) -> str:
        queries.append(command)
        return "1.3447E-07"

    monkeypatch.setattr(client, "query", query)
    assert client.read_positive_pulse_width(25) == pytest.approx(1.3447e-07)
    assert queries == ["MEAS:ADV:P1:VAL?"]


def test_positive_pulse_width_unavailable_logs_warning(monkeypatch, caplog) -> None:
    client = SDS3104XHDClient(SDS3104XHDConfig())
    monkeypatch.setattr(client, "query", lambda _command: "****")

    with caplog.at_level("WARNING"):
        value = client.read_positive_pulse_width(25)

    assert np.isnan(value)
    assert "[000025] PWID unavailable: ****" in caplog.text


def test_time_value_formatting() -> None:
    assert format_time_value(1.3447e-7) == "134.47 ns"
    assert format_time_value(2.53e-6) == "2.53 μs"
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
    client = SDS3104XHDClient(SDS3104XHDConfig())
    output = tmp_path / "000001.npz.tmp"

    client.save_npz(output, waveform, index=1, positive_pulse_width_s=1.3447e-7)

    assert output.is_file()
    assert not (tmp_path / "000001.npz.tmp.npz").exists()
    with np.load(output) as saved:
        assert saved["index"].dtype == np.dtype("int32")
        assert saved["time_s"].dtype == np.dtype("float64")
        assert saved["voltage_v"].dtype == np.dtype("float32")
        assert saved["adc"].dtype == np.dtype("int16")
        assert saved["positive_pulse_width_s"].dtype == np.dtype("float64")
        assert float(saved["positive_pulse_width_s"]) == pytest.approx(1.3447e-7)
        assert int(saved["point_count"]) == 3
        assert str(saved["channel"]) == "C1"

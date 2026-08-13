from __future__ import annotations

import logging
import math
import struct
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from time_formatting import format_time_value


TDIV_ENUM = (
    200e-12,
    500e-12,
    1e-9,
    2e-9,
    5e-9,
    10e-9,
    20e-9,
    50e-9,
    100e-9,
    200e-9,
    500e-9,
    1e-6,
    2e-6,
    5e-6,
    10e-6,
    20e-6,
    50e-6,
    100e-6,
    200e-6,
    500e-6,
    1e-3,
    2e-3,
    5e-3,
    10e-3,
    20e-3,
    50e-3,
    100e-3,
    200e-3,
    500e-3,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1000.0,
)

HORI_NUM = 10
PREAMBLE_MIN_BYTES = 346
BYTES_PER_POINT = 2
STOP_CHECK_MAX_INTERVAL_SEC = 0.05

logger = logging.getLogger(__name__)


@dataclass
class SDS3104XHDConfig:
    ip: str
    channel: str
    timeout_ms: int
    single_timeout_sec: float
    chunk_size: int
    trigger_poll_interval_sec: float
    pwid_settle_delay_sec: float
    pwid_retry_delay_sec: float
    pwid_max_attempts: int


@dataclass(frozen=True)
class WaveformPreamble:
    data_bytes: int
    point_num: int
    fp: int
    sp: int
    vdiv_raw: float
    offset_raw: float
    code: float
    adc_bit: int
    interval: float
    delay: float
    tdiv_index: int
    probe: float

    @property
    def tdiv(self) -> float:
        return TDIV_ENUM[self.tdiv_index]

    @property
    def vdiv(self) -> float:
        return self.vdiv_raw * self.probe

    @property
    def offset(self) -> float:
        return self.offset_raw * self.probe


@dataclass(frozen=True)
class ScopeWaveform:
    adc: np.ndarray
    time_s: np.ndarray
    voltage_v: np.ndarray
    preamble: WaveformPreamble

    @property
    def point_count(self) -> int:
        return int(self.adc.size)


@dataclass(frozen=True)
class PulseWidthAcquisitionResult:
    pulse_width_s: float
    pulse_width_raw: str
    pulse_width_valid: bool
    attempts: int
    single_seconds: float


class AcquisitionStopped(RuntimeError):
    """Raised at a safe checkpoint when the user stops an active capture."""


def parse_ieee4882_binary_block(raw: bytes) -> bytes:
    """Return the payload of an IEEE 488.2 definite-length binary block."""
    if len(raw) < 2 or raw[0:1] != b"#":
        raise ValueError("Malformed IEEE 488.2 binary block: missing '#' header")

    digit_byte = raw[1:2]
    if not digit_byte.isdigit():
        raise ValueError("Malformed IEEE 488.2 binary block: invalid length digit")
    length_digits = int(digit_byte)
    if length_digits <= 0:
        raise ValueError("Indefinite-length IEEE 488.2 blocks are not supported")

    header_end = 2 + length_digits
    if len(raw) < header_end:
        raise ValueError("Malformed IEEE 488.2 binary block: truncated header")
    length_field = raw[2:header_end]
    if not length_field.isdigit():
        raise ValueError("Malformed IEEE 488.2 binary block: invalid payload length")

    payload_length = int(length_field)
    payload_end = header_end + payload_length
    if len(raw) < payload_end:
        raise ValueError(
            "Malformed IEEE 488.2 binary block: "
            f"expected {payload_length} payload bytes, received {len(raw) - header_end}"
        )
    return raw[header_end:payload_end]


def parse_preamble(payload: bytes) -> WaveformPreamble:
    """Parse the little-endian WAVEDESC returned by SDS3104X HD."""
    if len(payload) < PREAMBLE_MIN_BYTES:
        raise ValueError(
            f"Malformed PREamble: expected at least {PREAMBLE_MIN_BYTES} bytes, "
            f"received {len(payload)}"
        )
    if not payload.startswith(b"WAVEDESC"):
        raise ValueError("Malformed PREamble: WAVEDESC signature missing")

    preamble = WaveformPreamble(
        data_bytes=struct.unpack_from("<i", payload, 0x3C)[0],
        point_num=struct.unpack_from("<i", payload, 0x74)[0],
        fp=struct.unpack_from("<i", payload, 0x84)[0],
        sp=struct.unpack_from("<i", payload, 0x88)[0],
        vdiv_raw=struct.unpack_from("<f", payload, 0x9C)[0],
        offset_raw=struct.unpack_from("<f", payload, 0xA0)[0],
        code=struct.unpack_from("<f", payload, 0xA4)[0],
        adc_bit=struct.unpack_from("<h", payload, 0xAC)[0],
        interval=struct.unpack_from("<f", payload, 0xB0)[0],
        delay=struct.unpack_from("<d", payload, 0xB4)[0],
        tdiv_index=struct.unpack_from("<h", payload, 0x144)[0],
        probe=struct.unpack_from("<f", payload, 0x148)[0],
    )
    if preamble.point_num <= 0:
        raise ValueError(f"Malformed PREamble: invalid point count {preamble.point_num}")
    if preamble.code == 0:
        raise ValueError("Malformed PREamble: code per division is zero")
    if preamble.interval <= 0:
        raise ValueError(f"Malformed PREamble: invalid interval {preamble.interval}")
    if not 0 <= preamble.tdiv_index < len(TDIV_ENUM):
        raise ValueError(f"Malformed PREamble: invalid TDIV index {preamble.tdiv_index}")
    return preamble


def decode_word_data(payload: bytes, expected_point_count: int) -> np.ndarray:
    """Decode complete little-endian int16 points and tolerate a short final point."""
    usable_bytes = len(payload) // BYTES_PER_POINT * BYTES_PER_POINT
    actual_point_count = usable_bytes // BYTES_PER_POINT
    if actual_point_count != expected_point_count:
        warnings.warn(
            "Scope waveform point count differs from PREamble: "
            f"expected {expected_point_count}, received {actual_point_count}",
            RuntimeWarning,
            stacklevel=2,
        )
    return np.frombuffer(payload[:usable_bytes], dtype="<i2").copy()


def convert_adc_to_voltage(
    adc: np.ndarray,
    *,
    vdiv_raw: float,
    probe: float,
    code: float,
    offset_raw: float,
) -> np.ndarray:
    if code == 0:
        raise ValueError("code per division must not be zero")
    vdiv = vdiv_raw * probe
    offset = offset_raw * probe
    return (adc.astype(np.float32) / np.float32(code) * np.float32(vdiv) - np.float32(offset)).astype(
        np.float32,
        copy=False,
    )


def parse_positive_pulse_width(raw: object) -> float:
    """Parse a PWID response, returning NaN for unavailable measurements."""
    if raw is None:
        return float("nan")
    text = str(raw).strip()
    if not text or "*" in text or text == "---":
        return float("nan")
    try:
        value = float(text)
    except (TypeError, ValueError):
        return float("nan")
    return value if math.isfinite(value) else float("nan")


class SDS3104XHDClient:
    """PyVISA driver for single-shot SDS3104X HD waveform capture."""

    def __init__(self, config: SDS3104XHDConfig):
        self.config = config
        self._rm = None
        self._inst = None
        self._idn = ""

    @property
    def resource(self) -> str:
        return f"TCPIP0::{self.config.ip}::INSTR"

    def connect(self) -> None:
        import pyvisa

        self._rm = pyvisa.ResourceManager()
        try:
            self._inst = self._rm.open_resource(self.resource)
            self._inst.timeout = self.config.timeout_ms
            self._inst.chunk_size = self.config.chunk_size
            self._idn = self.identify()
            self.configure_positive_pulse_width()
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        if self._inst is not None:
            try:
                self._inst.close()
            except Exception:
                pass
            finally:
                self._inst = None
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
            finally:
                self._rm = None
        self._idn = ""

    def identify(self) -> str:
        return self.query("*IDN?")

    def configure_positive_pulse_width(self) -> None:
        """Configure advanced measurement P1 as positive pulse width."""
        self.write("MEAS:ADV:P1:TYPE PWID")

    def read_positive_pulse_width(self, capture_index: int | None = None) -> float:
        """Read P1 in seconds; invalid values become NaN and I/O errors propagate."""
        prefix = f"[{capture_index:06d}] " if capture_index is not None else ""
        raw = self.query("MEAS:ADV:P1:VAL?").strip()
        value = parse_positive_pulse_width(raw)
        if math.isnan(value):
            logger.warning("%sPWID unavailable: %s", prefix, raw or "<empty>")
        return value

    def acquire_single_with_pwid_retry(
        self,
        *,
        capture_index: int | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> PulseWidthAcquisitionResult:
        """Acquire fresh Single frames until PWID is valid or attempts are exhausted."""
        attempt_limit = self.config.pwid_max_attempts
        if attempt_limit < 1:
            raise ValueError("max_attempts must be at least 1")

        prefix = f"[{capture_index:06d}] " if capture_index is not None else ""
        last_raw = ""
        last_single_seconds = 0.0
        for attempt in range(1, attempt_limit + 1):
            if should_stop is not None and should_stop():
                raise AcquisitionStopped("Capture stopped before Scope Single")

            logger.info(
                "%sScope Single attempt %d/%d",
                prefix,
                attempt,
                attempt_limit,
            )
            last_single_seconds = self.acquire_single()
            logger.info(
                "%sTrigger STOP after %.1f ms",
                prefix,
                last_single_seconds * 1000.0,
            )

            if should_stop is not None and should_stop():
                raise AcquisitionStopped("Capture stopped after Scope Single")

            logger.info(
                "%sWaiting %.1f ms before PWID query",
                prefix,
                self.config.pwid_settle_delay_sec * 1000.0,
            )
            self._sleep_interruptibly(self.config.pwid_settle_delay_sec, should_stop)

            # Query errors are communication failures and must follow the device error path.
            last_raw = self.query("MEAS:ADV:P1:VAL?").strip()
            pulse_width_s = parse_positive_pulse_width(last_raw)
            if math.isfinite(pulse_width_s):
                logger.info(
                    "%sPWID = %s",
                    prefix,
                    format_time_value(pulse_width_s),
                )
                return PulseWidthAcquisitionResult(
                    pulse_width_s=pulse_width_s,
                    pulse_width_raw=last_raw,
                    pulse_width_valid=True,
                    attempts=attempt,
                    single_seconds=last_single_seconds,
                )

            logger.warning("%sPWID unavailable: %s", prefix, last_raw or "<empty>")
            if attempt < attempt_limit:
                logger.info(
                    "%sWaiting %.1f ms before retry",
                    prefix,
                    self.config.pwid_retry_delay_sec * 1000.0,
                )
                self._sleep_interruptibly(self.config.pwid_retry_delay_sec, should_stop)

        logger.warning(
            "%sPWID unavailable after %d Single attempts; using last acquisition",
            prefix,
            attempt_limit,
        )
        return PulseWidthAcquisitionResult(
            pulse_width_s=float("nan"),
            pulse_width_raw=last_raw,
            pulse_width_valid=False,
            attempts=attempt_limit,
            single_seconds=last_single_seconds,
        )

    @staticmethod
    def _sleep_interruptibly(
        seconds: float,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        """Wait for instrument settling while honoring a cooperative stop request."""
        if should_stop is None:
            if seconds > 0:
                time.sleep(seconds)
            return

        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            if should_stop():
                raise AcquisitionStopped("Capture stopped while waiting for Scope")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, STOP_CHECK_MAX_INTERVAL_SEC))

    def write(self, command: str) -> None:
        if self._inst is None:
            raise RuntimeError("Scope not connected")
        self._inst.write(command)

    def query(self, command: str) -> str:
        if self._inst is None:
            raise RuntimeError("Scope not connected")
        return str(self._inst.query(command)).strip()

    def _read_binary_block(self, command: str) -> bytes:
        if self._inst is None:
            raise RuntimeError("Scope not connected")
        self.write(command)
        return parse_ieee4882_binary_block(bytes(self._inst.read_raw()))

    def acquire_single(self) -> float:
        """Arm one acquisition and wait until the newly-triggered frame stops."""
        self.write(":TRIGger:MODE SINGle")
        self.write(":TRIGger:RUN")
        started = time.monotonic()
        deadline = started + self.config.single_timeout_sec
        while True:
            status = self.query(":TRIGger:STATus?")
            if status.casefold() == "stop":
                return time.monotonic() - started
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Scope Single timed out after {self.config.single_timeout_sec} seconds "
                    f"(last status: {status or '<empty>'})"
                )
            time.sleep(self.config.trigger_poll_interval_sec)

    def read_preamble(self) -> WaveformPreamble:
        self.write(f":WAVeform:SOURce {self.config.channel}")
        payload = self._read_binary_block(":WAVeform:PREamble?")
        return parse_preamble(payload)

    def read_waveform(self) -> ScopeWaveform:
        preamble = self.read_preamble()
        self.write(":WAVeform:BYTeorder LSB")
        self.write(":WAVeform:WIDTh WORD")

        try:
            instrument_max_points = int(float(self.query(":WAVeform:MAXPoint?")))
        except (TypeError, ValueError) as exc:
            raise ValueError("Scope returned an invalid WAVeform:MAXPoint value") from exc
        if instrument_max_points <= 0:
            raise ValueError(f"Scope returned invalid MAXPoint {instrument_max_points}")

        configured_chunk_points = max(self.config.chunk_size // BYTES_PER_POINT, 1)
        points_per_chunk = min(instrument_max_points, configured_chunk_points)
        raw_chunks: list[bytes] = []
        for start in range(0, preamble.point_num, points_per_chunk):
            requested_points = min(points_per_chunk, preamble.point_num - start)
            self.write(f":WAVeform:STARt {start}")
            self.write(f":WAVeform:POINt {requested_points}")
            raw_chunks.append(self._read_binary_block(":WAVeform:DATA?"))

        adc = decode_word_data(b"".join(raw_chunks), preamble.point_num)
        if adc.size == 0:
            raise RuntimeError("Scope returned no complete waveform points")

        voltage_v = convert_adc_to_voltage(
            adc,
            vdiv_raw=preamble.vdiv_raw,
            probe=preamble.probe,
            code=preamble.code,
            offset_raw=preamble.offset_raw,
        )
        time_start = -(preamble.tdiv * HORI_NUM / 2.0) + preamble.delay
        time_s = time_start + np.arange(adc.size, dtype=np.float64) * preamble.interval
        return ScopeWaveform(adc=adc, time_s=time_s, voltage_v=voltage_v, preamble=preamble)

    def save_npz(
        self,
        path: str | Path,
        waveform: ScopeWaveform,
        index: int,
        positive_pulse_width_s: float = float("nan"),
        positive_pulse_width_raw: str = "",
        positive_pulse_width_valid: bool | None = None,
        positive_pulse_width_attempts: int = 1,
    ) -> None:
        output_path = Path(path)
        preamble = waveform.preamble
        sample_rate = 1.0 / preamble.interval
        device = self._idn or "SIGLENT SDS3104X HD"
        pulse_width_valid = (
            math.isfinite(positive_pulse_width_s)
            if positive_pulse_width_valid is None
            else bool(positive_pulse_width_valid) and math.isfinite(positive_pulse_width_s)
        )
        with output_path.open("wb") as output_file:
            np.savez_compressed(
                output_file,
                index=np.asarray(index, dtype=np.int32),
                time_s=waveform.time_s.astype(np.float64, copy=False),
                voltage_v=waveform.voltage_v.astype(np.float32, copy=False),
                adc=waveform.adc.astype(np.int16, copy=False),
                positive_pulse_width_s=np.asarray(positive_pulse_width_s, dtype=np.float64),
                positive_pulse_width_raw=np.asarray(positive_pulse_width_raw, dtype=np.str_),
                positive_pulse_width_valid=np.asarray(pulse_width_valid, dtype=np.bool_),
                positive_pulse_width_attempts=np.asarray(
                    positive_pulse_width_attempts,
                    dtype=np.int32,
                ),
                device=np.asarray(device),
                ip=np.asarray(self.config.ip),
                channel=np.asarray(self.config.channel),
                point_count=np.asarray(waveform.point_count, dtype=np.int32),
                adc_bit=np.asarray(preamble.adc_bit, dtype=np.int16),
                vdiv=np.asarray(preamble.vdiv, dtype=np.float32),
                offset=np.asarray(preamble.offset, dtype=np.float32),
                probe=np.asarray(preamble.probe, dtype=np.float32),
                code_per_div=np.asarray(preamble.code, dtype=np.float32),
                interval=np.asarray(preamble.interval, dtype=np.float64),
                sample_rate=np.asarray(sample_rate, dtype=np.float64),
                tdiv=np.asarray(preamble.tdiv, dtype=np.float64),
                delay=np.asarray(preamble.delay, dtype=np.float64),
            )

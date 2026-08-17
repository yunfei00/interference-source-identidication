from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import AppConfig, load_config, runtime_root  # noqa: E402
from sds3104xhd_client import (  # noqa: E402
    SDS3104XHDClient,
    SDS3104XHDConfig,
    parse_delay_value,
)
from time_formatting import format_time_value  # noqa: E402


DEFAULT_OUTPUT_DIR = (
    runtime_root() / "output"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent / "output"
)
SUMMARY_FILENAME = "delay_measurement_test_results.csv"
DETAIL_FILENAME = "delay_measurement_test_details.csv"


@dataclass(frozen=True)
class DelayMeasurementDetail:
    settle_delay_ms: float
    sample_index: int
    single_seconds: float
    raw_value: str
    valid: bool
    delay_s: float
    timestamp: str

    @property
    def delay_ns(self) -> float:
        return self.delay_s * 1e9 if self.valid else float("nan")


@dataclass(frozen=True)
class DelayMeasurementSummary:
    settle_delay_ms: float
    total: int
    valid: int
    invalid: int
    success_rate: float
    mean_delay_ns: float
    median_delay_ns: float
    min_delay_ns: float
    max_delay_ns: float


def summarize_delay(
    settle_delay_ms: float,
    details: Sequence[DelayMeasurementDetail],
) -> DelayMeasurementSummary:
    matching = [item for item in details if item.settle_delay_ms == settle_delay_ms]
    values_ns = [item.delay_ns for item in matching if item.valid]
    total = len(matching)
    valid = len(values_ns)
    unavailable = float("nan")
    return DelayMeasurementSummary(
        settle_delay_ms=settle_delay_ms,
        total=total,
        valid=valid,
        invalid=total - valid,
        success_rate=valid / total if total else 0.0,
        mean_delay_ns=statistics.fmean(values_ns) if values_ns else unavailable,
        median_delay_ns=statistics.median(values_ns) if values_ns else unavailable,
        min_delay_ns=min(values_ns) if values_ns else unavailable,
        max_delay_ns=max(values_ns) if values_ns else unavailable,
    )


def recommend_settle_delay(
    summaries: Sequence[DelayMeasurementSummary],
    *,
    target_success_rate: float = 0.99,
) -> tuple[DelayMeasurementSummary | None, bool]:
    if not summaries:
        return None, False
    qualifying = [item for item in summaries if item.success_rate >= target_success_rate]
    if qualifying:
        return min(qualifying, key=lambda item: item.settle_delay_ms), True
    best_rate = max(item.success_rate for item in summaries)
    return (
        min(
            (item for item in summaries if item.success_rate == best_rate),
            key=lambda item: item.settle_delay_ms,
        ),
        False,
    )


def run_delay_test(
    client: SDS3104XHDClient,
    *,
    delays_ms: Sequence[float],
    samples_per_delay: int,
    inter_sample_delay_ms: float,
    output_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[DelayMeasurementSummary], list[DelayMeasurementDetail]]:
    """Measure DELAY availability without retries, waveform reads, NPZ, or N9020A."""
    client.configure_delay_measurement()
    details: list[DelayMeasurementDetail] = []
    total_samples = len(delays_ms) * samples_per_delay
    completed_samples = 0
    for settle_delay_ms in delays_ms:
        print(f"\nTesting settle delay = {_format_milliseconds(settle_delay_ms)} ms")
        for sample_index in range(1, samples_per_delay + 1):
            single_seconds = client.acquire_single()
            sleep(settle_delay_ms / 1000.0)
            raw_value = client.query("MEAS:ADV:P1:VAL?").strip()
            delay_s = parse_delay_value(raw_value)
            valid = math.isfinite(delay_s)
            details.append(
                DelayMeasurementDetail(
                    settle_delay_ms=settle_delay_ms,
                    sample_index=sample_index,
                    single_seconds=single_seconds,
                    raw_value=raw_value,
                    valid=valid,
                    delay_s=delay_s,
                    timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                )
            )
            status = (
                f"VALID {format_time_value(delay_s)}"
                if valid
                else f"INVALID {raw_value or '<empty>'}"
            )
            print(f"{sample_index}/{samples_per_delay} {status}")
            completed_samples += 1
            if completed_samples < total_samples:
                sleep(inter_sample_delay_ms / 1000.0)

    summaries = [summarize_delay(delay_ms, details) for delay_ms in delays_ms]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / SUMMARY_FILENAME, summaries)
    _write_details(output_dir / DETAIL_FILENAME, details)
    return summaries, details


def _write_csv(path: Path, records: Sequence[DelayMeasurementSummary]) -> None:
    fieldnames = list(DelayMeasurementSummary.__dataclass_fields__)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(item) for item in records)


def _write_details(path: Path, records: Sequence[DelayMeasurementDetail]) -> None:
    fieldnames = list(DelayMeasurementDetail.__dataclass_fields__) + ["delay_ns"]
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for item in records:
            writer.writerow({**asdict(item), "delay_ns": item.delay_ns})


def _load_scope_state(path: Path) -> tuple[str | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    return str(raw.get("scope_ip", "")).strip() or None, str(
        raw.get("scope_channel", "")
    ).strip() or None


def _parse_delays(value: str) -> list[float]:
    try:
        delays = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delays must be comma-separated numbers") from exc
    if not delays or any(not math.isfinite(delay) or delay < 0 for delay in delays):
        raise argparse.ArgumentTypeError("delays must be non-negative numbers")
    return delays


def _format_milliseconds(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _make_scope_config(app_config: AppConfig, ip: str, channel: str) -> SDS3104XHDConfig:
    timing = app_config.scope
    return SDS3104XHDConfig(
        ip=ip,
        channel=channel,
        timeout_ms=timing.visa_timeout_ms,
        single_timeout_sec=timing.single_timeout_sec,
        chunk_size=timing.chunk_size_bytes,
        trigger_poll_interval_sec=timing.trigger_poll_interval_sec,
        delay_settle_delay_sec=timing.delay_settle_delay_sec,
        delay_retry_delay_sec=timing.delay_retry_delay_sec,
        delay_max_attempts=timing.delay_max_attempts,
        reconnect_enabled=timing.reconnect_enabled,
        reconnect_delay_sec=timing.reconnect_delay_sec,
        reconnect_max_attempts=timing.reconnect_max_attempts,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test SDS3104X HD DELAY measurement availability at STOP-to-query delays."
    )
    parser.add_argument("--ip", help="scope IP; defaults to collector state, then config.json")
    parser.add_argument("--channel", help="scope channel; defaults to collector state, then config")
    parser.add_argument("--samples", type=int, help="samples per tested settle delay")
    parser.add_argument("--delays", type=_parse_delays, help="comma-separated delays in ms")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"CSV output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    app_config = load_config()
    saved_ip, saved_channel = _load_scope_state(runtime_root() / "collector_state.json")
    ip = args.ip or saved_ip or app_config.scope.ip
    channel = args.channel or saved_channel or app_config.scope.channel
    test_config = app_config.delay_measurement_test
    samples = test_config.samples_per_delay if args.samples is None else args.samples
    if samples < 1:
        parser.error("--samples must be at least 1")
    delays_ms = args.delays or test_config.delays_ms

    client = SDS3104XHDClient(_make_scope_config(app_config, ip, channel))
    try:
        print(f"Connecting to SDS3104X HD at {ip}, channel {channel}...")
        client.connect()
        summaries, _ = run_delay_test(
            client,
            delays_ms=delays_ms,
            samples_per_delay=samples,
            inter_sample_delay_ms=test_config.inter_sample_delay_ms,
            output_dir=args.output_dir,
        )
    finally:
        client.disconnect()

    print("\nSettle   Total   Valid   Invalid   SuccessRate")
    for item in summaries:
        print(
            f"{_format_milliseconds(item.settle_delay_ms):>5} ms"
            f"   {item.total:>5}   {item.valid:>5}   {item.invalid:>7}"
            f"   {item.success_rate:>10.1%}"
        )
    recommendation, reached = recommend_settle_delay(summaries)
    if recommendation is not None and reached:
        print(
            "\nRecommended DELAY settle delay: "
            f"{_format_milliseconds(recommendation.settle_delay_ms)} ms"
        )
    elif recommendation is not None:
        print("\nNo tested settle delay reached 99% success rate.")
        print(
            f"Best tested delay: {_format_milliseconds(recommendation.settle_delay_ms)} ms "
            f"({recommendation.success_rate:.1%})"
        )
    print(f"Summary CSV: {args.output_dir / SUMMARY_FILENAME}")
    print(f"Details CSV: {args.output_dir / DETAIL_FILENAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

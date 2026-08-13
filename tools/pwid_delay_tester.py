from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
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
    parse_positive_pulse_width,
)
from time_formatting import format_time_value  # noqa: E402


DEFAULT_OUTPUT_DIR = (
    runtime_root() / "output"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent / "output"
)
SUMMARY_FILENAME = "pwid_delay_test_results.csv"
DETAIL_FILENAME = "pwid_delay_test_details.csv"


@dataclass(frozen=True)
class PWIDDelayDetail:
    delay_ms: float
    sample_index: int
    single_seconds: float
    raw_value: str
    valid: bool
    pulse_width_s: float
    timestamp: str

    @property
    def pulse_width_ns(self) -> float:
        return self.pulse_width_s * 1e9 if self.valid else float("nan")


@dataclass(frozen=True)
class PWIDDelaySummary:
    delay_ms: float
    total: int
    valid: int
    invalid: int
    success_rate: float
    mean_pwid_ns: float
    median_pwid_ns: float
    min_pwid_ns: float
    max_pwid_ns: float


def summarize_delay(
    delay_ms: float,
    details: Sequence[PWIDDelayDetail],
) -> PWIDDelaySummary:
    matching = [detail for detail in details if detail.delay_ms == delay_ms]
    values_ns = [detail.pulse_width_ns for detail in matching if detail.valid]
    total = len(matching)
    valid = len(values_ns)
    invalid = total - valid
    unavailable = float("nan")
    return PWIDDelaySummary(
        delay_ms=delay_ms,
        total=total,
        valid=valid,
        invalid=invalid,
        success_rate=valid / total if total else 0.0,
        mean_pwid_ns=statistics.fmean(values_ns) if values_ns else unavailable,
        median_pwid_ns=statistics.median(values_ns) if values_ns else unavailable,
        min_pwid_ns=min(values_ns) if values_ns else unavailable,
        max_pwid_ns=max(values_ns) if values_ns else unavailable,
    )


def recommend_settle_delay(
    summaries: Sequence[PWIDDelaySummary],
    *,
    target_success_rate: float = 0.99,
) -> tuple[PWIDDelaySummary | None, bool]:
    """Return the smallest target-reaching delay, or the best tested fallback."""
    if not summaries:
        return None, False
    qualifying = [item for item in summaries if item.success_rate >= target_success_rate]
    if qualifying:
        return min(qualifying, key=lambda item: item.delay_ms), True
    best_rate = max(item.success_rate for item in summaries)
    best = min(
        (item for item in summaries if item.success_rate == best_rate),
        key=lambda item: item.delay_ms,
    )
    return best, False


def run_delay_test(
    client: SDS3104XHDClient,
    *,
    delays_ms: Sequence[float],
    samples_per_delay: int,
    inter_sample_delay_ms: float,
    output_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[PWIDDelaySummary], list[PWIDDelayDetail]]:
    """Measure PWID availability without retries or waveform reads."""
    details: list[PWIDDelayDetail] = []
    total_samples = len(delays_ms) * samples_per_delay
    completed_samples = 0

    for delay_ms in delays_ms:
        print(f"\nTesting delay = {_format_milliseconds(delay_ms)} ms")
        for sample_index in range(1, samples_per_delay + 1):
            single_seconds = client.acquire_single()
            sleep(delay_ms / 1000.0)
            raw_value = client.query("MEAS:ADV:P1:VAL?").strip()
            pulse_width_s = parse_positive_pulse_width(raw_value)
            valid = math.isfinite(pulse_width_s)
            details.append(
                PWIDDelayDetail(
                    delay_ms=delay_ms,
                    sample_index=sample_index,
                    single_seconds=single_seconds,
                    raw_value=raw_value,
                    valid=valid,
                    pulse_width_s=pulse_width_s,
                    timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                )
            )
            status = f"VALID {format_time_value(pulse_width_s)}" if valid else (
                f"INVALID {raw_value or '<empty>'}"
            )
            print(f"{sample_index}/{samples_per_delay} {status}")

            completed_samples += 1
            if completed_samples < total_samples:
                sleep(inter_sample_delay_ms / 1000.0)

    summaries = [summarize_delay(delay_ms, details) for delay_ms in delays_ms]
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_summary_csv(output_dir / SUMMARY_FILENAME, summaries)
    _write_details_csv(output_dir / DETAIL_FILENAME, details)
    return summaries, details


def _write_summary_csv(path: Path, summaries: Sequence[PWIDDelaySummary]) -> None:
    fieldnames = [
        "delay_ms",
        "total",
        "valid",
        "invalid",
        "success_rate",
        "mean_pwid_ns",
        "median_pwid_ns",
        "min_pwid_ns",
        "max_pwid_ns",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for item in summaries:
            writer.writerow({name: getattr(item, name) for name in fieldnames})


def _write_details_csv(path: Path, details: Sequence[PWIDDelayDetail]) -> None:
    fieldnames = [
        "delay_ms",
        "sample_index",
        "single_seconds",
        "raw_value",
        "valid",
        "pulse_width_s",
        "pulse_width_ns",
        "timestamp",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for item in details:
            writer.writerow(
                {
                    "delay_ms": item.delay_ms,
                    "sample_index": item.sample_index,
                    "single_seconds": item.single_seconds,
                    "raw_value": item.raw_value,
                    "valid": item.valid,
                    "pulse_width_s": item.pulse_width_s,
                    "pulse_width_ns": item.pulse_width_ns,
                    "timestamp": item.timestamp,
                }
            )


def _load_scope_state(path: Path) -> tuple[str | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    ip = str(raw.get("scope_ip", "")).strip() or None
    channel = str(raw.get("scope_channel", "")).strip() or None
    return ip, channel


def _parse_delays(value: str) -> list[float]:
    try:
        delays = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delays must be comma-separated numbers") from exc
    if not delays or any(not math.isfinite(delay) or delay < 0 for delay in delays):
        raise argparse.ArgumentTypeError("delays must be a non-empty list of non-negative numbers")
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
        pwid_settle_delay_sec=timing.pwid_settle_delay_sec,
        pwid_retry_delay_sec=timing.pwid_retry_delay_sec,
        pwid_max_attempts=timing.pwid_max_attempts,
    )


def _print_summary(summaries: Sequence[PWIDDelaySummary]) -> None:
    print("\nDelay    Total   Valid   Invalid   SuccessRate")
    for item in summaries:
        print(
            f"{_format_milliseconds(item.delay_ms):>5} ms"
            f"   {item.total:>5}   {item.valid:>5}   {item.invalid:>7}"
            f"   {item.success_rate:>10.1%}"
        )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test SDS3104X HD PWID availability at different STOP-to-query delays."
    )
    parser.add_argument(
        "--ip",
        help="scope IP; defaults to collector_state.json, then config.json",
    )
    parser.add_argument(
        "--channel",
        help="scope channel; defaults to collector_state.json, then config.json",
    )
    parser.add_argument("--samples", type=int, help="samples per tested delay")
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
    samples = (
        app_config.pwid_delay_test.samples_per_delay
        if args.samples is None
        else args.samples
    )
    if samples < 1:
        parser.error("--samples must be at least 1")
    delays_ms = args.delays or app_config.pwid_delay_test.delays_ms

    client = SDS3104XHDClient(_make_scope_config(app_config, ip, channel))
    try:
        print(f"Connecting to SDS3104X HD at {ip}, channel {channel}...")
        client.connect()
        summaries, _details = run_delay_test(
            client,
            delays_ms=delays_ms,
            samples_per_delay=samples,
            inter_sample_delay_ms=app_config.pwid_delay_test.inter_sample_delay_ms,
            output_dir=args.output_dir,
        )
    finally:
        client.disconnect()

    _print_summary(summaries)
    recommendation, reached_target = recommend_settle_delay(summaries)
    if recommendation is not None and reached_target:
        print(
            "\nRecommended PWID settle delay: "
            f"{_format_milliseconds(recommendation.delay_ms)} ms"
        )
    elif recommendation is not None:
        print("\nNo tested delay reached 99% success rate.")
        print(
            "Recommended tested delay: "
            f"{_format_milliseconds(recommendation.delay_ms)} ms "
            f"({recommendation.success_rate:.1%})"
        )
    print(f"Summary CSV: {args.output_dir / SUMMARY_FILENAME}")
    print(f"Details CSV: {args.output_dir / DETAIL_FILENAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

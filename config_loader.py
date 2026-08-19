from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SOURCE_ROOT / "config.json"


class ConfigError(ValueError):
    """Raised when config.json exists but cannot be parsed or validated."""


@dataclass(frozen=True)
class ScopeTimingConfig:
    ip: str = "192.168.1.50"
    channel: str = "C1"
    single_timeout_sec: float = 30.0
    trigger_poll_interval_ms: int = 50
    delay_time_scale_sec: float = 5.0e-7
    cycles_time_scale_sec: float = 1.0e-4
    measurement_settle_delay_ms: int = 200
    measurement_retry_delay_ms: int = 200
    measurement_max_attempts: int = 3
    visa_timeout_ms: int = 60_000
    chunk_size_mb: int = 20
    reconnect_enabled: bool = True
    reconnect_delay_sec: float = 2.0
    reconnect_max_attempts: int = 5

    @property
    def trigger_poll_interval_sec(self) -> float:
        return self.trigger_poll_interval_ms / 1000.0

    @property
    def measurement_settle_delay_sec(self) -> float:
        return self.measurement_settle_delay_ms / 1000.0

    @property
    def measurement_retry_delay_sec(self) -> float:
        return self.measurement_retry_delay_ms / 1000.0

    # Compatibility aliases used by the standalone delay tester and older callers.
    @property
    def delay_settle_delay_ms(self) -> int:
        return self.measurement_settle_delay_ms

    @property
    def delay_retry_delay_ms(self) -> int:
        return self.measurement_retry_delay_ms

    @property
    def delay_max_attempts(self) -> int:
        return self.measurement_max_attempts

    @property
    def delay_settle_delay_sec(self) -> float:
        return self.measurement_settle_delay_sec

    @property
    def delay_retry_delay_sec(self) -> float:
        return self.measurement_retry_delay_sec

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024


@dataclass(frozen=True)
class N9020ATimingConfig:
    visa_timeout_ms: int = 5_000
    reconnect_enabled: bool = True
    calibration_wait_sec: float = 15.0
    disconnect_reconnect_delay_sec: float = 2.0
    reconnect_max_attempts: int = 10

    @property
    def reconnect_delay_sec(self) -> float:
        """Compatibility alias for releases that used one recovery delay."""
        return self.calibration_wait_sec


@dataclass(frozen=True)
class DelayMeasurementTestConfig:
    delays_ms: list[float] = field(
        default_factory=lambda: [0, 50, 100, 150, 200, 300, 500]
    )
    samples_per_delay: int = 50
    inter_sample_delay_ms: int = 200


@dataclass(frozen=True)
class AcquisitionRecoveryConfig:
    max_sample_recovery_attempts: int = 5


@dataclass(frozen=True)
class AppConfig:
    scope: ScopeTimingConfig = field(default_factory=ScopeTimingConfig)
    n9020a: N9020ATimingConfig = field(default_factory=N9020ATimingConfig)
    acquisition: AcquisitionRecoveryConfig = field(default_factory=AcquisitionRecoveryConfig)
    delay_measurement_test: DelayMeasurementTestConfig = field(
        default_factory=DelayMeasurementTestConfig
    )


def load_config(
    path: str | Path | None = None,
    *,
    create_if_missing: bool = True,
) -> AppConfig:
    config_path = Path(path) if path is not None else _find_default_config_path()
    defaults = AppConfig()
    if not config_path.exists():
        if create_if_missing:
            _write_default_config(config_path, defaults)
        return defaults

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid JSON in {config_path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config in {config_path}: root must be a JSON object")

    scope_raw = _section(raw, "scope", config_path)
    n9020a_raw = _section(raw, "n9020a", config_path)
    acquisition_raw = _section(raw, "acquisition", config_path)
    delay_test_raw = _section(
        raw,
        "delay_measurement_test"
        if "delay_measurement_test" in raw
        else "pwid_delay_test",
        config_path,
    )

    scope = ScopeTimingConfig(
        ip=_string(scope_raw, "ip", defaults.scope.ip, config_path),
        channel=_string(scope_raw, "channel", defaults.scope.channel, config_path),
        single_timeout_sec=_number(
            scope_raw,
            "single_timeout_sec",
            defaults.scope.single_timeout_sec,
            config_path,
        ),
        trigger_poll_interval_ms=_integer(
            scope_raw,
            "trigger_poll_interval_ms",
            defaults.scope.trigger_poll_interval_ms,
            config_path,
        ),
        delay_time_scale_sec=_number(
            scope_raw, "delay_time_scale_sec", defaults.scope.delay_time_scale_sec, config_path,
        ),
        cycles_time_scale_sec=_number(
            scope_raw, "cycles_time_scale_sec", defaults.scope.cycles_time_scale_sec, config_path,
        ),
        measurement_settle_delay_ms=_integer_with_legacy_keys(
            scope_raw,
            "measurement_settle_delay_ms",
            ("delay_settle_delay_ms", "pwid_settle_delay_ms"),
            defaults.scope.measurement_settle_delay_ms,
            config_path,
        ),
        measurement_retry_delay_ms=_integer_with_legacy_keys(
            scope_raw,
            "measurement_retry_delay_ms",
            ("delay_retry_delay_ms", "pwid_retry_delay_ms"),
            defaults.scope.measurement_retry_delay_ms,
            config_path,
        ),
        measurement_max_attempts=_integer_with_legacy_keys(
            scope_raw,
            "measurement_max_attempts",
            ("delay_max_attempts", "pwid_max_attempts"),
            defaults.scope.measurement_max_attempts,
            config_path,
        ),
        visa_timeout_ms=_integer(
            scope_raw,
            "visa_timeout_ms",
            defaults.scope.visa_timeout_ms,
            config_path,
        ),
        chunk_size_mb=_integer(
            scope_raw,
            "chunk_size_mb",
            defaults.scope.chunk_size_mb,
            config_path,
        ),
        reconnect_enabled=_boolean(
            scope_raw, "reconnect_enabled", defaults.scope.reconnect_enabled, config_path
        ),
        reconnect_delay_sec=_number(
            scope_raw, "reconnect_delay_sec", defaults.scope.reconnect_delay_sec, config_path
        ),
        reconnect_max_attempts=_integer(
            scope_raw, "reconnect_max_attempts", defaults.scope.reconnect_max_attempts,
            config_path,
        ),
    )
    n9020a = N9020ATimingConfig(
        visa_timeout_ms=_integer(
            n9020a_raw,
            "visa_timeout_ms",
            defaults.n9020a.visa_timeout_ms,
            config_path,
        ),
        reconnect_enabled=_boolean(
            n9020a_raw, "reconnect_enabled", defaults.n9020a.reconnect_enabled, config_path
        ),
        calibration_wait_sec=_number_with_legacy(
            n9020a_raw,
            "calibration_wait_sec",
            "reconnect_delay_sec",
            defaults.n9020a.calibration_wait_sec,
            config_path,
        ),
        disconnect_reconnect_delay_sec=_number(
            n9020a_raw,
            "disconnect_reconnect_delay_sec",
            defaults.n9020a.disconnect_reconnect_delay_sec,
            config_path,
        ),
        reconnect_max_attempts=_integer(
            n9020a_raw, "reconnect_max_attempts", defaults.n9020a.reconnect_max_attempts,
            config_path,
        ),
    )
    acquisition = AcquisitionRecoveryConfig(
        max_sample_recovery_attempts=_integer(
            acquisition_raw,
            "max_sample_recovery_attempts",
            defaults.acquisition.max_sample_recovery_attempts,
            config_path,
        )
    )
    delay_test = DelayMeasurementTestConfig(
        delays_ms=_number_list(
            delay_test_raw,
            "delays_ms",
            defaults.delay_measurement_test.delays_ms,
            config_path,
        ),
        samples_per_delay=_integer(
            delay_test_raw,
            "samples_per_delay",
            defaults.delay_measurement_test.samples_per_delay,
            config_path,
        ),
        inter_sample_delay_ms=_integer(
            delay_test_raw,
            "inter_sample_delay_ms",
            defaults.delay_measurement_test.inter_sample_delay_ms,
            config_path,
        ),
    )
    config = AppConfig(
        scope=scope,
        n9020a=n9020a,
        acquisition=acquisition,
        delay_measurement_test=delay_test,
    )
    _validate(config, config_path)
    return config


def _write_default_config(path: Path, config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ConfigError(f"Cannot create default config at {path}: {exc}") from exc


def runtime_root() -> Path:
    """Return the writable directory beside a packaged exe or the source root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


def config_search_paths() -> tuple[Path, ...]:
    """Return config candidates in runtime priority order without duplicates."""
    candidates = [runtime_root() / "config.json", Path.cwd() / "config.json"]
    if not getattr(sys, "frozen", False):
        candidates.append(DEFAULT_CONFIG_PATH)
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _find_default_config_path() -> Path:
    candidates = config_search_paths()
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def _section(raw: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid config in {path}: '{name}' must be an object")
    return value


def _string(section: dict[str, Any], key: str, default: str, path: Path) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Invalid config in {path}: '{key}' must be a non-empty string")
    return value.strip()


def _number(section: dict[str, Any], key: str, default: float, path: Path) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Invalid config in {path}: '{key}' must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"Invalid config in {path}: '{key}' must be finite")
    return number


def _integer(section: dict[str, Any], key: str, default: int, path: Path) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"Invalid config in {path}: '{key}' must be an integer")
    return value


def _integer_with_legacy(
    section: dict[str, Any],
    key: str,
    legacy_key: str,
    default: int,
    path: Path,
) -> int:
    selected_key = key if key in section else legacy_key if legacy_key in section else key
    return _integer(section, selected_key, default, path)


def _integer_with_legacy_keys(
    section: dict[str, Any],
    key: str,
    legacy_keys: tuple[str, ...],
    default: int,
    path: Path,
) -> int:
    selected_key = key
    if key not in section:
        selected_key = next((legacy for legacy in legacy_keys if legacy in section), key)
    return _integer(section, selected_key, default, path)


def _number_with_legacy(
    section: dict[str, Any],
    key: str,
    legacy_key: str,
    default: float,
    path: Path,
) -> float:
    selected_key = key if key in section else legacy_key if legacy_key in section else key
    return _number(section, selected_key, default, path)


def _boolean(section: dict[str, Any], key: str, default: bool, path: Path) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Invalid config in {path}: '{key}' must be a boolean")
    return value


def _number_list(
    section: dict[str, Any],
    key: str,
    default: list[float],
    path: Path,
) -> list[float]:
    value = section.get(key, default)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"Invalid config in {path}: '{key}' must be a non-empty list")
    result: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(
                f"Invalid config in {path}: '{key}[{index}]' must be a number"
            )
        number = float(item)
        if not math.isfinite(number):
            raise ConfigError(
                f"Invalid config in {path}: '{key}[{index}]' must be finite"
            )
        result.append(number)
    return result


def _validate(config: AppConfig, path: Path) -> None:
    scope = config.scope
    checks = (
        (scope.channel in {"C1", "C2", "C3", "C4"}, "scope.channel must be C1-C4"),
        (scope.single_timeout_sec > 0, "scope.single_timeout_sec must be > 0"),
        (
            scope.trigger_poll_interval_ms >= 1,
            "scope.trigger_poll_interval_ms must be >= 1",
        ),
        (
            scope.delay_time_scale_sec > 0,
            "scope.delay_time_scale_sec must be > 0",
        ),
        (
            scope.cycles_time_scale_sec > 0,
            "scope.cycles_time_scale_sec must be > 0",
        ),
        (
            scope.measurement_settle_delay_ms >= 0,
            "scope.measurement_settle_delay_ms must be >= 0",
        ),
        (
            scope.measurement_retry_delay_ms >= 0,
            "scope.measurement_retry_delay_ms must be >= 0",
        ),
        (
            1 <= scope.measurement_max_attempts <= 10,
            "scope.measurement_max_attempts must be between 1 and 10",
        ),
        (scope.visa_timeout_ms > 0, "scope.visa_timeout_ms must be > 0"),
        (scope.chunk_size_mb > 0, "scope.chunk_size_mb must be > 0"),
        (scope.reconnect_delay_sec >= 0, "scope.reconnect_delay_sec must be >= 0"),
        (
            1 <= scope.reconnect_max_attempts <= 100,
            "scope.reconnect_max_attempts must be between 1 and 100",
        ),
        (
            config.n9020a.visa_timeout_ms > 0,
            "n9020a.visa_timeout_ms must be > 0",
        ),
        (
            config.n9020a.calibration_wait_sec >= 0,
            "n9020a.calibration_wait_sec must be >= 0",
        ),
        (
            config.n9020a.disconnect_reconnect_delay_sec >= 0,
            "n9020a.disconnect_reconnect_delay_sec must be >= 0",
        ),
        (
            1 <= config.n9020a.reconnect_max_attempts <= 100,
            "n9020a.reconnect_max_attempts must be between 1 and 100",
        ),
        (
            1 <= config.acquisition.max_sample_recovery_attempts <= 100,
            "acquisition.max_sample_recovery_attempts must be between 1 and 100",
        ),
        (
            config.delay_measurement_test.samples_per_delay >= 1,
            "delay_measurement_test.samples_per_delay must be >= 1",
        ),
        (
            config.delay_measurement_test.inter_sample_delay_ms >= 0,
            "delay_measurement_test.inter_sample_delay_ms must be >= 0",
        ),
        (
            all(delay >= 0 for delay in config.delay_measurement_test.delays_ms),
            "delay_measurement_test.delays_ms values must be >= 0",
        ),
    )
    for valid, message in checks:
        if not valid:
            raise ConfigError(f"Invalid config in {path}: {message}")

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class ConfigError(ValueError):
    """Raised when config.json exists but cannot be parsed or validated."""


@dataclass(frozen=True)
class ScopeTimingConfig:
    single_timeout_sec: float = 30.0
    trigger_poll_interval_ms: int = 50
    pwid_settle_delay_ms: int = 200
    pwid_retry_delay_ms: int = 200
    pwid_max_attempts: int = 3
    visa_timeout_ms: int = 60_000
    chunk_size_mb: int = 20

    @property
    def trigger_poll_interval_sec(self) -> float:
        return self.trigger_poll_interval_ms / 1000.0

    @property
    def pwid_settle_delay_sec(self) -> float:
        return self.pwid_settle_delay_ms / 1000.0

    @property
    def pwid_retry_delay_sec(self) -> float:
        return self.pwid_retry_delay_ms / 1000.0

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024


@dataclass(frozen=True)
class N9020ATimingConfig:
    visa_timeout_ms: int = 5_000


@dataclass(frozen=True)
class PWIDDelayTestConfig:
    delays_ms: list[float] = field(
        default_factory=lambda: [0, 50, 100, 150, 200, 300, 500]
    )
    samples_per_delay: int = 50
    inter_sample_delay_ms: int = 200


@dataclass(frozen=True)
class AppConfig:
    scope: ScopeTimingConfig = field(default_factory=ScopeTimingConfig)
    n9020a: N9020ATimingConfig = field(default_factory=N9020ATimingConfig)
    pwid_delay_test: PWIDDelayTestConfig = field(default_factory=PWIDDelayTestConfig)


def load_config(
    path: str | Path | None = None,
    *,
    create_if_missing: bool = True,
) -> AppConfig:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
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
    delay_test_raw = _section(raw, "pwid_delay_test", config_path)

    scope = ScopeTimingConfig(
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
        pwid_settle_delay_ms=_integer(
            scope_raw,
            "pwid_settle_delay_ms",
            defaults.scope.pwid_settle_delay_ms,
            config_path,
        ),
        pwid_retry_delay_ms=_integer(
            scope_raw,
            "pwid_retry_delay_ms",
            defaults.scope.pwid_retry_delay_ms,
            config_path,
        ),
        pwid_max_attempts=_integer(
            scope_raw,
            "pwid_max_attempts",
            defaults.scope.pwid_max_attempts,
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
    )
    n9020a = N9020ATimingConfig(
        visa_timeout_ms=_integer(
            n9020a_raw,
            "visa_timeout_ms",
            defaults.n9020a.visa_timeout_ms,
            config_path,
        )
    )
    delay_test = PWIDDelayTestConfig(
        delays_ms=_number_list(
            delay_test_raw,
            "delays_ms",
            defaults.pwid_delay_test.delays_ms,
            config_path,
        ),
        samples_per_delay=_integer(
            delay_test_raw,
            "samples_per_delay",
            defaults.pwid_delay_test.samples_per_delay,
            config_path,
        ),
        inter_sample_delay_ms=_integer(
            delay_test_raw,
            "inter_sample_delay_ms",
            defaults.pwid_delay_test.inter_sample_delay_ms,
            config_path,
        ),
    )
    config = AppConfig(scope=scope, n9020a=n9020a, pwid_delay_test=delay_test)
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


def _section(raw: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Invalid config in {path}: '{name}' must be an object")
    return value


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
        (scope.single_timeout_sec > 0, "scope.single_timeout_sec must be > 0"),
        (
            scope.trigger_poll_interval_ms >= 1,
            "scope.trigger_poll_interval_ms must be >= 1",
        ),
        (
            scope.pwid_settle_delay_ms >= 0,
            "scope.pwid_settle_delay_ms must be >= 0",
        ),
        (
            scope.pwid_retry_delay_ms >= 0,
            "scope.pwid_retry_delay_ms must be >= 0",
        ),
        (
            1 <= scope.pwid_max_attempts <= 10,
            "scope.pwid_max_attempts must be between 1 and 10",
        ),
        (scope.visa_timeout_ms > 0, "scope.visa_timeout_ms must be > 0"),
        (scope.chunk_size_mb > 0, "scope.chunk_size_mb must be > 0"),
        (
            config.n9020a.visa_timeout_ms > 0,
            "n9020a.visa_timeout_ms must be > 0",
        ),
        (
            config.pwid_delay_test.samples_per_delay >= 1,
            "pwid_delay_test.samples_per_delay must be >= 1",
        ),
        (
            config.pwid_delay_test.inter_sample_delay_ms >= 0,
            "pwid_delay_test.inter_sample_delay_ms must be >= 0",
        ),
        (
            all(delay >= 0 for delay in config.pwid_delay_test.delays_ms),
            "pwid_delay_test.delays_ms values must be >= 0",
        ),
    )
    for valid, message in checks:
        if not valid:
            raise ConfigError(f"Invalid config in {path}: {message}")

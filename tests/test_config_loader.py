from __future__ import annotations

import json

import pytest

from config_loader import AppConfig, ConfigError, load_config


def test_config_loads_all_timing_values(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "scope": {
                    "single_timeout_sec": 12.5,
                    "trigger_poll_interval_ms": 25,
                    "pwid_settle_delay_ms": 150,
                    "pwid_retry_delay_ms": 300,
                    "pwid_max_attempts": 4,
                    "visa_timeout_ms": 45_000,
                    "chunk_size_mb": 8,
                },
                "n9020a": {"visa_timeout_ms": 7_000},
                "pwid_delay_test": {
                    "delays_ms": [0, 75.5],
                    "samples_per_delay": 12,
                    "inter_sample_delay_ms": 250,
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.scope.single_timeout_sec == 12.5
    assert config.scope.trigger_poll_interval_sec == pytest.approx(0.025)
    assert config.scope.pwid_settle_delay_sec == pytest.approx(0.15)
    assert config.scope.pwid_retry_delay_sec == pytest.approx(0.3)
    assert config.scope.pwid_max_attempts == 4
    assert config.scope.visa_timeout_ms == 45_000
    assert config.scope.chunk_size_bytes == 8 * 1024 * 1024
    assert config.n9020a.visa_timeout_ms == 7_000
    assert config.pwid_delay_test.delays_ms == [0.0, 75.5]
    assert config.pwid_delay_test.samples_per_delay == 12
    assert config.pwid_delay_test.inter_sample_delay_ms == 250


def test_missing_fields_use_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"scope": {"single_timeout_sec": 10}}', encoding="utf-8")

    config = load_config(path)

    defaults = AppConfig()
    assert config.scope.single_timeout_sec == 10
    assert config.scope.trigger_poll_interval_ms == defaults.scope.trigger_poll_interval_ms
    assert config.scope.pwid_max_attempts == 3
    assert config.n9020a == defaults.n9020a
    assert config.pwid_delay_test == defaults.pwid_delay_test


def test_missing_file_uses_defaults_and_can_generate_config(tmp_path) -> None:
    path = tmp_path / "config.json"

    config = load_config(path)

    assert config == AppConfig()
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["scope"]["pwid_max_attempts"] == 3


@pytest.mark.parametrize(
    "content, message",
    [
        ("{broken", "Invalid JSON"),
        ('{"scope": {"single_timeout_sec": 0}}', "single_timeout_sec"),
        ('{"scope": {"trigger_poll_interval_ms": 0}}', "trigger_poll_interval_ms"),
        ('{"scope": {"pwid_max_attempts": 11}}', "pwid_max_attempts"),
        ('{"pwid_delay_test": {"delays_ms": [-1]}}', "delays_ms"),
        ('{"pwid_delay_test": {"samples_per_delay": 0}}', "samples_per_delay"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)

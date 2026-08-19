from __future__ import annotations

import json

import pytest
import config_loader

from config_loader import AppConfig, ConfigError, load_config


def test_config_loads_all_timing_values(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "scope": {
                    "ip": "192.0.2.44",
                    "channel": "C2",
                    "single_timeout_sec": 12.5,
                    "trigger_poll_interval_ms": 25,
                    "delay_time_scale_sec": 5e-7,
                    "cycles_time_scale_sec": 1e-4,
                    "measurement_settle_delay_ms": 150,
                    "measurement_retry_delay_ms": 300,
                    "measurement_max_attempts": 4,
                    "visa_timeout_ms": 45_000,
                    "chunk_size_mb": 8,
                    "reconnect_enabled": True,
                    "reconnect_delay_sec": 3,
                    "reconnect_max_attempts": 6,
                },
                "n9020a": {
                    "visa_timeout_ms": 7_000,
                    "calibration_wait_sec": 12,
                    "disconnect_reconnect_delay_sec": 2,
                    "reconnect_max_attempts": 10,
                },
                "acquisition": {"max_sample_recovery_attempts": 7},
                "delay_measurement_test": {
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
    assert config.scope.ip == "192.0.2.44"
    assert config.scope.channel == "C2"
    assert config.scope.trigger_poll_interval_sec == pytest.approx(0.025)
    assert config.scope.delay_settle_delay_sec == pytest.approx(0.15)
    assert config.scope.delay_retry_delay_sec == pytest.approx(0.3)
    assert config.scope.delay_max_attempts == 4
    assert config.scope.measurement_max_attempts == 4
    assert config.scope.delay_time_scale_sec == pytest.approx(5e-7)
    assert config.scope.cycles_time_scale_sec == pytest.approx(1e-4)
    assert config.scope.visa_timeout_ms == 45_000
    assert config.scope.chunk_size_bytes == 8 * 1024 * 1024
    assert config.n9020a.visa_timeout_ms == 7_000
    assert config.scope.reconnect_delay_sec == 3
    assert config.n9020a.reconnect_delay_sec == 12
    assert config.n9020a.disconnect_reconnect_delay_sec == 2
    assert config.acquisition.max_sample_recovery_attempts == 7
    assert config.delay_measurement_test.delays_ms == [0.0, 75.5]
    assert config.delay_measurement_test.samples_per_delay == 12
    assert config.delay_measurement_test.inter_sample_delay_ms == 250


def test_missing_fields_use_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"scope": {"single_timeout_sec": 10}}', encoding="utf-8")

    config = load_config(path)

    defaults = AppConfig()
    assert config.scope.single_timeout_sec == 10
    assert config.scope.trigger_poll_interval_ms == defaults.scope.trigger_poll_interval_ms
    assert config.scope.delay_max_attempts == 3
    assert config.n9020a == defaults.n9020a
    assert config.delay_measurement_test == defaults.delay_measurement_test


def test_missing_file_uses_defaults_and_can_generate_config(tmp_path) -> None:
    path = tmp_path / "config.json"

    config = load_config(path)

    assert config == AppConfig()
    assert path.is_file()
    generated = json.loads(path.read_text(encoding="utf-8"))
    assert generated["scope"]["measurement_max_attempts"] == 3
    assert generated["n9020a"]["reconnect_max_attempts"] == 10
    assert "pwid_max_attempts" not in generated["scope"]


def test_packaged_app_prefers_config_beside_executable(monkeypatch, tmp_path) -> None:
    exe_dir = tmp_path / "release"
    working_dir = tmp_path / "working"
    exe_dir.mkdir()
    working_dir.mkdir()
    (exe_dir / "config.json").write_text(
        '{"scope": {"delay_settle_delay_ms": 111}}',
        encoding="utf-8",
    )
    (working_dir / "config.json").write_text(
        '{"scope": {"delay_settle_delay_ms": 222}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_loader.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config_loader.sys, "executable", str(exe_dir / "app.exe"))
    monkeypatch.chdir(working_dir)

    config = load_config()

    assert config.scope.delay_settle_delay_ms == 111
    assert config_loader.config_search_paths()[0] == (exe_dir / "config.json").resolve()


@pytest.mark.parametrize(
    "content, message",
    [
        ("{broken", "Invalid JSON"),
        ('{"scope": {"single_timeout_sec": 0}}', "single_timeout_sec"),
        ('{"scope": {"trigger_poll_interval_ms": 0}}', "trigger_poll_interval_ms"),
        ('{"scope": {"measurement_max_attempts": 11}}', "measurement_max_attempts"),
        ('{"scope": {"channel": "C5"}}', "scope.channel"),
        ('{"delay_measurement_test": {"delays_ms": [-1]}}', "delays_ms"),
        ('{"delay_measurement_test": {"samples_per_delay": 0}}', "samples_per_delay"),
    ],
)
def test_invalid_config_is_rejected(tmp_path, content: str, message: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_legacy_pwid_fields_map_to_delay_and_new_fields_win(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "scope": {
                    "pwid_settle_delay_ms": 999,
                    "delay_settle_delay_ms": 123,
                    "measurement_settle_delay_ms": 77,
                    "pwid_retry_delay_ms": 456,
                    "pwid_max_attempts": 4,
                },
                "pwid_delay_test": {"samples_per_delay": 9},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.scope.delay_settle_delay_ms == 77
    assert config.scope.delay_retry_delay_ms == 456
    assert config.scope.delay_max_attempts == 4
    assert config.delay_measurement_test.samples_per_delay == 9

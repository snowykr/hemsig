"""Tests for gateway configuration management."""

import os
import pytest
from unittest.mock import MagicMock, patch

from gateway.config import (
    GatewayConfig,
    HomeChannel,
    Platform,
    PlatformConfig,
    SessionResetPolicy,
    StreamingConfig,
    _apply_env_overrides,
    load_gateway_config,
)
from gateway.platform_registry import PlatformEntry, platform_registry


class TestHomeChannelRoundtrip:
    def test_bridges_telegram_disable_link_previews_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  disable_link_previews: true\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.platforms[Platform.TELEGRAM].extra["disable_link_previews"] is True

    def test_bridges_notice_delivery_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "slack:\n"
            "  notice_delivery: private\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert config.get_notice_delivery(Platform.SLACK) == "private"

    def test_bridges_telegram_proxy_url_from_config_yaml(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  proxy_url: socks5://127.0.0.1:1080\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

        load_gateway_config()

        import os
        assert os.environ.get("TELEGRAM_PROXY") == "socks5://127.0.0.1:1080"

    def test_telegram_proxy_env_takes_precedence_over_config(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "telegram:\n"
            "  proxy_url: http://from-config:8080\n",
            encoding="utf-8",
        )

        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("TELEGRAM_PROXY", "socks5://from-env:1080")

        load_gateway_config()

        import os
        assert os.environ.get("TELEGRAM_PROXY") == "socks5://from-env:1080"


class TestHomeChannelEnvOverrides:
    """Home channel env vars should apply even when the platform was already
    configured via config.yaml (not just when credential env vars create it)."""

    def test_existing_platform_configs_accept_home_channel_env_overrides(self):
        cases = [
            (
                Platform.SLACK,
                PlatformConfig(enabled=True, token="xoxb-from-config"),
                {"SLACK_HOME_CHANNEL": "C123", "SLACK_HOME_CHANNEL_NAME": "Ops"},
                ("C123", "Ops"),
            ),
            (
                Platform.SLACK,
                PlatformConfig(enabled=True),
                {
                    "SLACK_HOME_CHANNEL": "1234567890@lid",
                    "SLACK_HOME_CHANNEL_NAME": "Owner DM",
                },
                ("1234567890@lid", "Owner DM"),
            ),
            (
                Platform.SIGNAL,
                PlatformConfig(
                    enabled=True,
                    extra={"http_url": "http://localhost:9090", "account": "+15551234567"},
                ),
                {"SIGNAL_HOME_CHANNEL": "+1555000", "SIGNAL_HOME_CHANNEL_NAME": "Phone"},
                ("+1555000", "Phone"),
            ),
            (
                Platform.SLACK,
                PlatformConfig(
                    enabled=True,
                    token="mm-token",
                    extra={"url": "https://mm.example.com"},
                ),
                {"SLACK_HOME_CHANNEL": "ch_abc123", "SLACK_HOME_CHANNEL_NAME": "General"},
                ("ch_abc123", "General"),
            ),
            (
                Platform.TELEGRAM,
                PlatformConfig(
                    enabled=True,
                    token="syt_abc123",
                    extra={},
                ),
                {"TELEGRAM_HOME_CHANNEL": "!room123:example.org", "TELEGRAM_HOME_CHANNEL_NAME": "Bot Room"},
                ("!room123:example.org", "Bot Room"),
            ),
            (
                Platform.EMAIL,
                PlatformConfig(
                    enabled=True,
                    extra={
                        "address": "hermes@test.com",
                        "imap_host": "imap.test.com",
                        "smtp_host": "smtp.test.com",
                    },
                ),
                {"EMAIL_HOME_ADDRESS": "user@test.com", "EMAIL_HOME_ADDRESS_NAME": "Inbox"},
                ("user@test.com", "Inbox"),
            ),
            (
                Platform.EMAIL,
                PlatformConfig(enabled=True, api_key="token_abc"),
                {"EMAIL_HOME_ADDRESS": "+15559876543", "EMAIL_HOME_ADDRESS_NAME": "My Phone"},
                ("+15559876543", "My Phone"),
            ),
        ]

        for platform, platform_config, env, expected in cases:
            config = GatewayConfig(platforms={platform: platform_config})
            with patch.dict(os.environ, env, clear=True):
                _apply_env_overrides(config)

            home = config.platforms[platform].home_channel
            assert home is not None, f"{platform.value}: home_channel should not be None"
            assert (home.chat_id, home.name) == expected, platform.value


class TestPluginPlatformConfigLoading:
    def test_load_gateway_config_discovers_plugins_before_platform_parse(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  configchat:\n"
            "    enabled: true\n"
            "    extra:\n"
            "      server: chat.example.org\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        entry = PlatformEntry(
            name="configchat",
            label="Config Chat",
            adapter_factory=lambda cfg: MagicMock(),
            check_fn=lambda: True,
            validate_config=lambda cfg: bool(cfg.extra.get("server")),
            source="plugin",
        )

        def fake_discover_plugins(force: bool = False):
            del force
            platform_registry.register(entry)

        monkeypatch.setattr("hermes_cli.plugins.discover_plugins", fake_discover_plugins)

        try:
            config = load_gateway_config()
            platform = Platform("configchat")
            assert platform in config.platforms
            assert config.platforms[platform].enabled is True
            assert config.platforms[platform].extra["server"] == "chat.example.org"
        finally:
            platform_registry.unregister("configchat")


class TestUnknownPlatformLoading:
    def test_load_gateway_config_ignores_unknown_gateway_json_entries(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "gateway.json").write_text(
            '{'
            '"platforms": {"oldchat": {"enabled": true, "token": "legacy-token"}}, '
            '"legacyoffice": {"enabled": true}'
            '}',
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert not config.platforms
        saved = (hermes_home / "gateway.json").read_text(encoding="utf-8")
        assert "oldchat" in saved
        assert "legacyoffice" in saved

    def test_load_gateway_config_ignores_unknown_platform_entries(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "platforms:\n"
            "  legacychat:\n"
            "    enabled: true\n"
            "    token: legacy-token\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        with pytest.raises(ValueError):
            Platform("legacychat")
        assert not config.platforms

        saved = (hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "legacychat" in saved
        assert "legacy-token" in saved

    def test_load_gateway_config_ignores_unknown_reset_and_top_level_platform_entries(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "reset_by_platform:\n"
            "  legacychat:\n"
            "    mode: idle\n"
            "    idle_minutes: 60\n"
            "legacychat:\n"
            "  enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = load_gateway_config()

        assert not config.reset_by_platform
        saved = (hermes_home / "config.yaml").read_text(encoding="utf-8")
        assert "legacychat" in saved

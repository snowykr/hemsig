"""Tests for platform connected checker coverage."""

from gateway.config import GatewayConfig, Platform, PlatformConfig


def test_kept_checker_platforms_connect_with_required_fields():
    config = GatewayConfig(platforms={
        Platform.SIGNAL: PlatformConfig(enabled=True, extra={"http_url": "http://localhost"}),
        Platform.EMAIL: PlatformConfig(enabled=True, extra={"address": "bot@example.com"}),
        Platform.API_SERVER: PlatformConfig(enabled=True),
        Platform.WEBHOOK: PlatformConfig(enabled=True),
    })

    connected = set(config.get_connected_platforms())
    assert {Platform.SIGNAL, Platform.EMAIL, Platform.API_SERVER, Platform.WEBHOOK}.issubset(connected)


def test_token_platforms_still_use_generic_token_check():
    config = GatewayConfig(platforms={
        Platform.TELEGRAM: PlatformConfig(enabled=True, token="t"),
        Platform.DISCORD: PlatformConfig(enabled=True, token="d"),
        Platform.SLACK: PlatformConfig(enabled=True, token="s"),
    })

    connected = set(config.get_connected_platforms())
    assert {Platform.TELEGRAM, Platform.DISCORD, Platform.SLACK}.issubset(connected)

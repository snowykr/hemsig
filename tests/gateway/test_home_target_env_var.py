"""Regression tests for /sethome env-var resolution."""

from gateway.run import _home_target_env_var


def test_email_home_target_env_var_uses_home_address():
    assert _home_target_env_var("email") == "EMAIL_HOME_ADDRESS"


def test_telegram_home_target_env_var_uses_home_channel():
    assert _home_target_env_var("telegram") == "TELEGRAM_HOME_CHANNEL"


def test_discord_home_target_env_var_uses_home_channel():
    assert _home_target_env_var("discord") == "DISCORD_HOME_CHANNEL"


def test_unknown_platform_home_target_env_var_falls_back_to_home_channel():
    assert _home_target_env_var("custom") == "CUSTOM_HOME_CHANNEL"


def test_case_insensitive_platform_name():
    assert _home_target_env_var("Email") == "EMAIL_HOME_ADDRESS"

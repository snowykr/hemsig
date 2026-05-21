"""Regression tests for packaging metadata in pyproject.toml."""

from pathlib import Path
import tomllib


def _load_optional_dependencies():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return project["optional-dependencies"]


def _load_nix_python_overlay_text() -> str:
    nix_python_path = Path(__file__).resolve().parents[1] / "nix" / "python.nix"
    return nix_python_path.read_text(encoding="utf-8")


def test_removed_platform_extras_are_absent():
    optional_dependencies = _load_optional_dependencies()
    allowed_extras = {
        "modal", "daytona", "vercel", "dev", "messaging", "cron", "slack", "cli",
        "tts-premium", "voice", "pty", "honcho", "mcp", "homeassistant", "acp",
        "mistral", "bedrock", "termux", "google", "web", "rl", "yc-bench", "all",
    }

    assert set(optional_dependencies).issubset(allowed_extras)

    all_refs = [dep for dep in optional_dependencies["all"] if dep.startswith("hermes-agent[")]
    for dep in all_refs:
        extra = dep.removeprefix("hermes-agent[").removesuffix("]")
        assert extra in allowed_extras


def test_messaging_extra_keeps_supported_platform_dependencies():
    messaging_extra = _load_optional_dependencies()["messaging"]

    assert any(dep.startswith("python-telegram-bot") for dep in messaging_extra)
    assert any(dep.startswith("discord.py") for dep in messaging_extra)
    assert any(dep.startswith("slack-bolt") for dep in messaging_extra)


def test_nix_overlay_no_longer_keeps_removed_platform_build_hacks():
    nix_python = _load_nix_python_overlay_text()

    for removed_package in (
        "alibabacloud-credentials-api",
        "alibabacloud-endpoint-util",
        "alibabacloud-gateway-spi",
        "alibabacloud-tea",
    ):
        assert removed_package not in nix_python

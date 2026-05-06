import asyncio
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner, _persist_terminal_cwd_default, _reapply_terminal_cwd_from_config
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent, MessageType


def test_persist_terminal_cwd_default_updates_config_and_env(tmp_path, monkeypatch):
    config_home = tmp_path / ".hermes"
    config_home.mkdir()
    config_path = config_home / "config.yaml"
    env_path = config_home / ".env"
    config_path.write_text("terminal:\n  cwd: /old/path\n", encoding="utf-8")
    env_path.write_text("TERMINAL_CWD=/old/path\n", encoding="utf-8")

    import gateway.run as gateway_run_module

    monkeypatch.setattr(gateway_run_module, "_hermes_home", config_home)
    monkeypatch.setenv("TERMINAL_CWD", "/old/path")
    monkeypatch.setenv("HERMES_HOME", str(config_home))

    assert _persist_terminal_cwd_default("/home/snowy/coding") is True
    assert "cwd: /home/snowy/coding" in config_path.read_text(encoding="utf-8")
    assert "TERMINAL_CWD=/home/snowy/coding" in env_path.read_text(encoding="utf-8")
    assert gateway_run_module.os.environ["TERMINAL_CWD"] == "/home/snowy/coding"


def test_prepare_inbound_message_text_preserves_exact_cwd_directive_in_shared_thread():
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_name="thread",
        chat_type="thread",
        user_id="456",
        user_name="snowy",
        thread_id="789",
    )
    event = MessageEvent(
        text="cwd ->> /home/snowy/coding/",
        message_type=MessageType.TEXT,
        source=source,
    )

    result = asyncio.run(
        runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )
    )

    assert result == "cwd ->> /home/snowy/coding/"


def test_prepare_inbound_message_text_preserves_exact_where_cwd_query_in_shared_thread():
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(group_sessions_per_user=True, thread_sessions_per_user=False)

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_name="thread",
        chat_type="thread",
        user_id="456",
        user_name="snowy",
        thread_id="789",
    )
    event = MessageEvent(
        text="where cwd",
        message_type=MessageType.TEXT,
        source=source,
    )

    result = asyncio.run(
        runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )
    )

    assert result == "where cwd"


def test_reapply_terminal_cwd_from_config_overrides_stale_env(tmp_path, monkeypatch):
    config_home = tmp_path / ".hermes"
    config_home.mkdir()
    config_path = config_home / "config.yaml"
    config_path.write_text("terminal:\n  cwd: /home/snowy/coding/hemsig\n", encoding="utf-8")

    import gateway.run as gateway_run_module

    monkeypatch.setattr(gateway_run_module, "_hermes_home", config_home)
    monkeypatch.setenv("TERMINAL_CWD", "/home/snowy/coding/poolbot")

    _reapply_terminal_cwd_from_config()

    assert gateway_run_module.os.environ["TERMINAL_CWD"] == "/home/snowy/coding/hemsig"

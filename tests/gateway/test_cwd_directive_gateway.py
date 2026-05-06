import asyncio
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _persist_terminal_cwd_default, _reapply_terminal_cwd_from_config
from gateway.session import SessionContext, SessionSource, SessionStore
from gateway.session_context import get_effective_cwd
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


def test_prepare_inbound_message_text_preserves_exact_pdir_directive_in_shared_thread():
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
        text="pdir ->> /home/snowy/coding/project with spaces",
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

    assert result == "pdir ->> /home/snowy/coding/project with spaces"


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


def test_gateway_workspace_result_updates_session_store_not_global_config(tmp_path, monkeypatch):
    import gateway.run as gateway_run_module

    config_home = tmp_path / ".hermes"
    config_home.mkdir()
    config_path = config_home / "config.yaml"
    config_path.write_text("terminal:\n  cwd: /old/path\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(config_home))
    monkeypatch.setenv("TERMINAL_CWD", "/old/path")

    project = tmp_path / "project"
    project.mkdir()
    runner = object.__new__(GatewayRunner)
    runner.session_store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner.session_store._db = None
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread", thread_id="t1")
    entry = runner.session_store.get_or_create_session(source)
    context = SessionContext(source=source, connected_platforms=[], home_channels={}, session_key=entry.session_key)
    tokens = runner._set_session_env(context)

    try:
        updated, tokens = runner._apply_cwd_change_result(
            session_entry=entry,
            session_key=entry.session_key,
            agent_result={"pdir_changed_to": str(project), "cwd_changed_to": str(project)},
            context=context,
            session_env_tokens=tokens,
        )
        assert updated.project_dir == str(project)
        assert updated.working_dir == str(project)
        assert get_effective_cwd() == str(project)
    finally:
        runner._clear_session_env(tokens)

    assert "cwd: /old/path" in config_path.read_text(encoding="utf-8")
    assert gateway_run_module.os.environ["TERMINAL_CWD"] == "/old/path"


def test_rollback_uses_session_working_dir_instead_of_global_terminal_cwd(tmp_path, monkeypatch):
    import gateway.run as gateway_run_module
    import tools.checkpoint_manager as checkpoint_module

    config_home = tmp_path / ".hermes"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("checkpoints:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run_module, "_hermes_home", config_home)

    global_dir = tmp_path / "global"
    session_dir = tmp_path / "session"
    global_dir.mkdir()
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_dir))

    seen = {}

    class FakeCheckpointManager:
        def __init__(self, **kwargs):
            pass

        def list_checkpoints(self, cwd):
            seen["cwd"] = cwd
            return []

    monkeypatch.setattr(checkpoint_module, "CheckpointManager", FakeCheckpointManager)
    monkeypatch.setattr(
        checkpoint_module,
        "format_checkpoint_list",
        lambda checkpoints, cwd: f"listed {cwd}",
    )

    runner = object.__new__(GatewayRunner)
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread", thread_id="t1")
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="discord:123:t1",
        working_dir=str(session_dir),
    )
    event = MessageEvent(text="/rollback", message_type=MessageType.TEXT, source=source)
    tokens = runner._set_session_env(context)
    try:
        result = asyncio.run(runner._handle_rollback_command(event))
    finally:
        runner._clear_session_env(tokens)

    assert result == f"listed {session_dir}"
    assert seen["cwd"] == str(session_dir)


def test_rollback_dispatch_uses_persisted_session_working_dir_without_seeded_context(tmp_path, monkeypatch):
    import gateway.run as gateway_run_module
    import tools.checkpoint_manager as checkpoint_module

    config_home = tmp_path / ".hermes"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("checkpoints:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run_module, "_hermes_home", config_home)

    global_dir = tmp_path / "global"
    session_dir = tmp_path / "session"
    global_dir.mkdir()
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_dir))

    seen = {}

    class FakeCheckpointManager:
        def __init__(self, **kwargs):
            pass

        def list_checkpoints(self, cwd):
            seen["cwd"] = cwd
            return []

    monkeypatch.setattr(checkpoint_module, "CheckpointManager", FakeCheckpointManager)
    monkeypatch.setattr(
        checkpoint_module,
        "format_checkpoint_list",
        lambda checkpoints, cwd: f"listed {cwd}",
    )

    runner = object.__new__(GatewayRunner)
    runner.session_store = SessionStore(tmp_path / "sessions", GatewayConfig())
    runner.session_store._db = None
    runner._detect_stale_code = lambda: False
    runner._is_user_authorized = lambda source: True
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner.hooks = SimpleNamespace(emit_collect=lambda *args, **kwargs: _async_empty_list())

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="thread",
        thread_id="t1",
        user_id="456",
    )
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.update_workspace(entry.session_key, working_dir=str(session_dir))
    event = MessageEvent(text="/rollback", message_type=MessageType.TEXT, source=source)

    result = asyncio.run(runner._handle_message(event))

    assert result == f"listed {session_dir}"
    assert seen["cwd"] == str(session_dir)


async def _async_empty_list():
    return []

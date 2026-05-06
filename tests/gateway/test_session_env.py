import asyncio
import os
import sys
import types

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionContext, SessionSource
from gateway.session_context import (
    get_effective_cwd,
    get_session_env,
    set_session_vars,
    clear_session_vars,
    _VAR_MAP,
    _UNSET,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Reset all session contextvars to _UNSET between tests.

    In production each asyncio.Task gets a fresh context copy where the
    defaults are _UNSET.  In tests all functions share the same thread
    context, so a clear_session_vars() from test A (which sets vars to "")
    would leak into test B.  This fixture ensures each test starts clean.
    """
    yield
    for var in _VAR_MAP.values():
        # Can't use var.reset() without a token; just set back to sentinel.
        var.set(_UNSET)


def test_set_session_env_sets_contextvars(monkeypatch):
    """_set_session_env should populate contextvars, not os.environ."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    tokens = runner._set_session_env(context)

    # Values should be readable via get_session_env (contextvar path)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == "Group"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"
    assert get_session_env("HERMES_SESSION_USER_NAME") == "alice"
    assert get_session_env("HERMES_SESSION_THREAD_ID") == "17585"

    # os.environ should NOT be touched
    assert os.getenv("HERMES_SESSION_PLATFORM") is None
    assert os.getenv("HERMES_SESSION_THREAD_ID") is None

    # Clean up
    runner._clear_session_env(tokens)


def test_set_session_env_publishes_workspace_contextvars(tmp_path, monkeypatch):
    runner = object.__new__(GatewayRunner)
    project = tmp_path / "project"
    working = project / "pkg"
    working.mkdir(parents=True)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread")
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        project_dir=str(project),
        working_dir=str(working),
    )

    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_SESSION_PROJECT_DIR") == str(project)
        assert get_session_env("HERMES_SESSION_WORKING_DIR") == str(working)
        assert get_session_env("HERMES_EFFECTIVE_CWD") == str(working)
        assert get_effective_cwd() == str(working)
    finally:
        runner._clear_session_env(tokens)


def test_set_session_env_does_not_freeze_global_fallback_before_config_reapply(tmp_path, monkeypatch):
    runner = object.__new__(GatewayRunner)
    stale = tmp_path / "stale"
    configured = tmp_path / "configured"
    stale.mkdir()
    configured.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(stale))
    source = SessionSource(platform=Platform.DISCORD, chat_id="123", chat_type="thread")
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)
    try:
        assert get_session_env("HERMES_EFFECTIVE_CWD") == ""
        monkeypatch.setenv("TERMINAL_CWD", str(configured))
        assert get_effective_cwd() == str(configured)
    finally:
        runner._clear_session_env(tokens)


def test_get_effective_cwd_precedence_and_stale_fallback(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    working = tmp_path / "working"
    working.mkdir()
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))

    tokens = set_session_vars(project_dir=str(project), working_dir=str(working))
    try:
        assert get_effective_cwd() == str(working)
    finally:
        clear_session_vars(tokens)

    tokens = set_session_vars(project_dir=str(project), working_dir=str(tmp_path / "missing"))
    try:
        assert get_effective_cwd() == str(project)
    finally:
        clear_session_vars(tokens)

    tokens = set_session_vars(project_dir=str(tmp_path / "missing-project"), working_dir="")
    try:
        assert get_effective_cwd("/default") == str(fallback)
    finally:
        clear_session_vars(tokens)


def test_clear_session_env_restores_previous_state(monkeypatch):
    """_clear_session_env should restore contextvars to their pre-handler values."""
    runner = object.__new__(GatewayRunner)

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_NAME", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        user_id="123456",
        user_name="alice",
        thread_id="17585",
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_USER_ID") == "123456"

    runner._clear_session_env(tokens)

    # After clear, contextvars should return to defaults (empty)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_CHAT_ID") == ""
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_USER_ID") == ""
    assert get_session_env("HERMES_SESSION_USER_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""


def test_get_session_env_falls_back_to_os_environ(monkeypatch):
    """get_session_env should fall back to os.environ when contextvar is unset."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_PLATFORM") == "discord"

    # Now set a contextvar — should prefer it
    tokens = set_session_vars(platform="telegram")
    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"

    # After clear — should return "" (explicitly cleared), NOT fall back
    # to os.environ.  This is the fix for #10304: stale os.environ values
    # must not leak through after a gateway session is cleaned up.
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_PLATFORM") == ""


def test_get_session_env_default_when_nothing_set(monkeypatch):
    """get_session_env returns default when neither contextvar nor env is set."""
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    assert get_session_env("HERMES_SESSION_PLATFORM") == ""
    assert get_session_env("HERMES_SESSION_PLATFORM", "fallback") == "fallback"


def test_set_session_env_handles_missing_optional_fields():
    """_set_session_env should handle None chat_name and thread_id gracefully."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name=None,
        chat_type="private",
        thread_id=None,
    )
    context = SessionContext(source=source, connected_platforms=[], home_channels={})

    tokens = runner._set_session_env(context)

    assert get_session_env("HERMES_SESSION_PLATFORM") == "telegram"
    assert get_session_env("HERMES_SESSION_CHAT_ID") == "-1001"
    assert get_session_env("HERMES_SESSION_CHAT_NAME") == ""
    assert get_session_env("HERMES_SESSION_THREAD_ID") == ""

    runner._clear_session_env(tokens)


# ---------------------------------------------------------------------------
# SESSION_KEY contextvars tests
# ---------------------------------------------------------------------------


def test_session_key_set_via_contextvars(monkeypatch):
    """set_session_vars should set HERMES_SESSION_KEY via contextvars."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    tokens = set_session_vars(
        platform="telegram",
        chat_id="-1001",
        session_key="tg:-1001:17585",
    )
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"

    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_session_key_falls_back_to_os_environ(monkeypatch):
    """get_session_env for SESSION_KEY should fall back to os.environ."""
    monkeypatch.setenv("HERMES_SESSION_KEY", "env-session-123")

    # No contextvar set — should read from os.environ
    assert get_session_env("HERMES_SESSION_KEY") == "env-session-123"

    # Set contextvar — should prefer it
    tokens = set_session_vars(session_key="ctx-session-456")
    assert get_session_env("HERMES_SESSION_KEY") == "ctx-session-456"

    # After clear — should return "" (explicitly cleared), not os.environ (#10304)
    clear_session_vars(tokens)
    assert get_session_env("HERMES_SESSION_KEY") == ""


def test_set_session_env_includes_session_key():
    """_set_session_env should propagate session_key from SessionContext."""
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_name="Group",
        chat_type="group",
        thread_id="17585",
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="tg:-1001:17585",
    )

    # Capture baseline value before setting (may be non-empty from another
    # test in the same pytest-xdist worker sharing the context).
    tokens = runner._set_session_env(context)
    assert get_session_env("HERMES_SESSION_KEY") == "tg:-1001:17585"
    runner._clear_session_env(tokens)
    # After clearing, the session key must not retain the value we just set.
    # The exact post-clear value depends on context propagation from other
    # tests, so only check that our value was removed, not what replaced it.
    assert get_session_env("HERMES_SESSION_KEY") != "tg:-1001:17585"


def test_session_key_no_race_condition_with_contextvars(monkeypatch):
    """Prove contextvars isolates SESSION_KEY across concurrent async tasks.

    Two tasks set different session keys. With contextvars each task
    reads back its own value. With os.environ the second task would
    overwrite the first (the old bug).
    """
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)

    results = {}

    async def handler(key: str, delay: float):
        tokens = set_session_vars(session_key=key)
        try:
            await asyncio.sleep(delay)
            read_back = get_session_env("HERMES_SESSION_KEY")
            results[key] = read_back
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("session-A", 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("session-B", 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    # Both tasks must read back their own session key
    assert results["session-A"] == "session-A", (
        f"Session A got '{results['session-A']}' instead of 'session-A' — race condition!"
    )
    assert results["session-B"] == "session-B", (
        f"Session B got '{results['session-B']}' instead of 'session-B' — race condition!"
    )


@pytest.mark.asyncio
async def test_run_in_executor_with_context_preserves_session_env(monkeypatch):
    """Gateway executor work should inherit session contextvars for tool routing."""
    runner = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)

    project = os.getcwd()
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="2144471399",
        chat_type="dm",
        user_id="123456",
        user_name="alice",
        thread_id=None,
    )
    context = SessionContext(
        source=source,
        connected_platforms=[],
        home_channels={},
        session_key="agent:main:telegram:dm:2144471399",
        project_dir=project,
    )

    tokens = runner._set_session_env(context)
    try:
        result = await runner._run_in_executor_with_context(
            lambda: {
                "platform": get_session_env("HERMES_SESSION_PLATFORM"),
                "chat_id": get_session_env("HERMES_SESSION_CHAT_ID"),
                "user_id": get_session_env("HERMES_SESSION_USER_ID"),
                "session_key": get_session_env("HERMES_SESSION_KEY"),
                "effective_cwd": get_effective_cwd(),
            }
        )
    finally:
        runner._clear_session_env(tokens)

    assert result == {
        "platform": "telegram",
        "chat_id": "2144471399",
        "user_id": "123456",
        "session_key": "agent:main:telegram:dm:2144471399",
        "effective_cwd": project,
    }


def test_effective_cwd_no_race_condition_with_contextvars(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    results = {}

    async def handler(key: str, path, delay: float):
        tokens = set_session_vars(session_key=key, working_dir=str(path))
        try:
            await asyncio.sleep(delay)
            results[key] = get_effective_cwd()
        finally:
            clear_session_vars(tokens)

    async def run():
        task_a = asyncio.create_task(handler("session-A", first, 0.15))
        await asyncio.sleep(0.05)
        task_b = asyncio.create_task(handler("session-B", second, 0.05))
        await asyncio.gather(task_a, task_b)

    asyncio.run(run())

    assert results == {"session-A": str(first), "session-B": str(second)}


def test_restored_reset_and_resume_workspace_feeds_first_tool_execution(tmp_path, monkeypatch):
    """After reset/resume bookkeeping, the next tool default cwd is restored."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore, build_session_context
    from tools.terminal_tool import _get_env_config

    runner = object.__new__(GatewayRunner)
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    store._db = None
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="alice",
    )
    entry = store.get_or_create_session(source)
    project = tmp_path / "project"
    working = project / "app"
    working.mkdir(parents=True)
    store.update_workspace(entry.session_key, project_dir=str(project), working_dir=str(working))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    reset = store.reset_session(entry.session_key)
    assert reset is not None
    context = build_session_context(source, store.config, reset)
    tokens = runner._set_session_env(context)
    try:
        assert _get_env_config()["cwd"] == str(working)
    finally:
        runner._clear_session_env(tokens)

    switched = store.switch_session(entry.session_key, "resumed-session")
    assert switched is not None
    context = build_session_context(source, store.config, switched)
    tokens = runner._set_session_env(context)
    try:
        assert _get_env_config()["cwd"] == str(working)
    finally:
        runner._clear_session_env(tokens)


def test_cached_agent_reuse_and_recreation_refresh_workspace_each_turn(tmp_path, monkeypatch):
    """A reused or recreated agent sees the current turn's session workspace."""
    from tools.terminal_tool import _get_env_config

    runner = object.__new__(GatewayRunner)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    source = SessionSource(platform=Platform.DISCORD, chat_id="c", chat_type="thread", thread_id="t")
    context = SessionContext(source=source, connected_platforms=[], home_channels={}, working_dir=str(first))
    tokens = runner._set_session_env(context)
    try:
        assert _get_env_config()["cwd"] == str(first)
    finally:
        runner._clear_session_env(tokens)

    context = SessionContext(source=source, connected_platforms=[], home_channels={}, working_dir=str(second))
    tokens = runner._set_session_env(context)
    try:
        assert _get_env_config()["cwd"] == str(second)
    finally:
        runner._clear_session_env(tokens)


@pytest.mark.asyncio
async def test_background_task_seeds_persisted_workspace_before_agent_run(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_dir))

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["gateway_session_key"] = kwargs.get("gateway_session_key")

        def run_conversation(self, user_message, task_id=None):
            captured["effective_cwd"] = get_effective_cwd()
            return {"final_response": "done", "messages": []}

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    class FakeAdapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))

        def extract_media(self, response):
            return [], response

        def extract_images(self, response):
            return [], response

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    runner.session_store._db = None
    runner.adapters = {Platform.DISCORD: FakeAdapter()}
    runner._provider_routing = {}
    runner._session_db = None
    runner._fallback_model = None
    runner._cleanup_agent_resources = lambda agent: None
    runner._resolve_session_agent_runtime = lambda **kwargs: ("model", {"api_key": "key"})
    runner._resolve_session_reasoning_config = lambda **kwargs: {}
    runner._load_service_tier = lambda: None
    runner._resolve_turn_agent_config = lambda prompt, model, runtime: {
        "model": model,
        "runtime": runtime,
        "request_overrides": None,
    }

    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *args, **kwargs: set())
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"agent": {}})

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="alice",
    )
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.update_workspace(entry.session_key, project_dir=str(workspace), working_dir=str(workspace))

    await runner._run_background_task("pwd please", source, "bg-test")

    assert captured["effective_cwd"] == str(workspace)
    assert captured["gateway_session_key"] == entry.session_key
    assert "done" in runner.adapters[Platform.DISCORD].sent[0][1]


@pytest.mark.asyncio
async def test_compression_eviction_turn_keeps_workspace_for_recreated_agent(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(global_dir))

    class FakeHooks:
        async def emit(self, *args, **kwargs):
            return None

    class FakeCompressionAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None

        def _compress_context(self, messages, *_args, **_kwargs):
            self.session_id = "compressed-session"
            return ([{"role": "assistant", "content": "compressed"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeCompressionAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {
        "model": {"default": "model", "context_length": 1000},
        "compression": {"enabled": True, "hygiene_hard_message_limit": 4},
    })
    monkeypatch.setattr("gateway.run.get_model_context_length", lambda *args, **kwargs: 1000, raising=False)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = SessionStore(tmp_path / "sessions", runner.config)
    runner.session_store._db = None
    runner.hooks = FakeHooks()
    runner.adapters = {}
    runner._pending_native_image_paths_by_session = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._running_agents = {}
    runner._provider_routing = {}
    runner._show_reasoning = False
    runner._session_db = None
    runner._clear_restart_failure_count = lambda session_key: None
    runner._is_session_run_current = lambda session_key, generation: True
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._evict_cached_agent = lambda session_key: runner._agent_cache.pop(session_key, None)
    runner._resolve_session_agent_runtime = lambda **kwargs: ("model", {"api_key": "key"})
    runner._run_agent = lambda **kwargs: _async_agent_result(captured_cwd=get_effective_cwd())

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="alice",
    )
    entry = runner.session_store.get_or_create_session(source)
    runner.session_store.update_workspace(entry.session_key, project_dir=str(workspace), working_dir=str(workspace))
    for idx in range(4):
        runner.session_store.append_to_transcript(
            entry.session_id,
            {"role": "user" if idx % 2 == 0 else "assistant", "content": f"msg {idx}"},
        )

    event = MessageEvent(text="next turn", message_type=MessageType.TEXT, source=source)
    result = await runner._handle_message_with_agent(event, source, entry.session_key, 1)

    assert result == "ok"
    assert _async_agent_result.seen_cwd == str(workspace)
    assert runner.session_store.get_or_create_session(source).session_id == "compressed-session"


async def _async_agent_result(*, captured_cwd):
    _async_agent_result.seen_cwd = captured_cwd
    return {
        "final_response": "ok",
        "messages": [],
        "api_calls": 0,
        "last_prompt_tokens": 0,
        "history_offset": 1,
    }


@pytest.mark.asyncio
async def test_run_in_executor_with_context_forwards_args():
    """_run_in_executor_with_context should forward *args to the callable."""
    runner = object.__new__(GatewayRunner)

    def add(a, b):
        return a + b

    result = await runner._run_in_executor_with_context(add, 3, 7)
    assert result == 10


@pytest.mark.asyncio
async def test_run_in_executor_with_context_propagates_exceptions():
    """Exceptions inside the executor should propagate to the caller."""
    runner = object.__new__(GatewayRunner)

    def blow_up():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await runner._run_in_executor_with_context(blow_up)

"""Tests for _resolve_path() — TERMINAL_CWD-aware path resolution in file_tools."""

import os
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import tools.terminal_tool as terminal_tool


class TestResolvePath:
    """Verify _resolve_path respects TERMINAL_CWD for worktree isolation."""

    def test_relative_path_uses_terminal_cwd(self, monkeypatch, tmp_path):
        """Relative paths resolve against TERMINAL_CWD, not process CWD."""
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        from tools.file_tools import _resolve_path

        result = _resolve_path("foo/bar.py")
        assert result == (tmp_path / "foo" / "bar.py")

    def test_absolute_path_ignores_terminal_cwd(self, monkeypatch, tmp_path):
        """Absolute paths are unaffected by TERMINAL_CWD."""
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        from tools.file_tools import _resolve_path

        absolute = (tmp_path / "already-absolute.txt").resolve()
        result = _resolve_path(str(absolute))
        assert result == absolute

    def test_falls_back_to_cwd_without_terminal_cwd(self, monkeypatch):
        """Without TERMINAL_CWD, falls back to os.getcwd()."""
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        from tools.file_tools import _resolve_path

        result = _resolve_path("some_file.txt")
        assert result == Path(os.getcwd()) / "some_file.txt"

    def test_tilde_expansion(self, monkeypatch, tmp_path):
        """~ is expanded before TERMINAL_CWD join (already absolute)."""
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        from tools.file_tools import _resolve_path

        result = _resolve_path("~/notes.txt")
        # After expanduser, ~/notes.txt becomes absolute → TERMINAL_CWD ignored
        assert result == Path.home() / "notes.txt"

    def test_result_is_resolved(self, monkeypatch, tmp_path):
        """Output path has no '..' components."""
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        from tools.file_tools import _resolve_path

        result = _resolve_path("a/../b/file.txt")
        assert ".." not in str(result)
        assert result == (tmp_path / "b" / "file.txt")

    def test_relative_path_prefers_live_file_ops_cwd(self, monkeypatch, tmp_path):
        """Live env.cwd must win after the terminal session changes directory."""
        start_dir = tmp_path / "start"
        live_dir = tmp_path / "worktree"
        start_dir.mkdir()
        live_dir.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(start_dir))

        from tools import file_tools

        task_id = "live-cwd"
        fake_ops = SimpleNamespace(
            env=SimpleNamespace(cwd=str(live_dir)),
            cwd=str(start_dir),
        )

        with file_tools._file_ops_lock:
            previous = file_tools._file_ops_cache.get(task_id)
            file_tools._file_ops_cache[task_id] = fake_ops

        try:
            result = file_tools._resolve_path("nested/file.txt", task_id=task_id)
        finally:
            with file_tools._file_ops_lock:
                if previous is None:
                    file_tools._file_ops_cache.pop(task_id, None)
                else:
                    file_tools._file_ops_cache[task_id] = previous

        assert result == live_dir / "nested" / "file.txt"

    def test_relative_path_uses_session_effective_cwd_before_terminal_env(self, monkeypatch, tmp_path):
        """Session-local cwd is the default when no live env cwd is active."""
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools.file_tools import _resolve_path

        terminal_dir = tmp_path / "terminal"
        session_dir = tmp_path / "session"
        terminal_dir.mkdir()
        session_dir.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(terminal_dir))
        tokens = set_session_vars(working_dir=str(session_dir))
        try:
            result = _resolve_path("nested/file.txt", task_id="session-cwd")
        finally:
            clear_session_vars(tokens)

        assert result == session_dir / "nested" / "file.txt"

    def test_session_workspace_beats_stale_live_terminal_env(self, monkeypatch, tmp_path):
        """After pdir/cwd changes, file tools must not use stale env.cwd."""
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import file_tools

        stale_live_dir = tmp_path / "stale-live"
        session_dir = tmp_path / "session"
        stale_live_dir.mkdir()
        session_dir.mkdir()
        task_id = "stale-live-env"
        fake_env = SimpleNamespace(cwd=str(stale_live_dir))
        with file_tools._file_ops_lock:
            previous = file_tools._file_ops_cache.get(task_id)
            file_tools._file_ops_cache[task_id] = SimpleNamespace(env=fake_env, cwd=str(stale_live_dir))
        tokens = set_session_vars(working_dir=str(session_dir))
        try:
            result = file_tools._resolve_path("nested/file.txt", task_id=task_id)
        finally:
            clear_session_vars(tokens)
            with file_tools._file_ops_lock:
                if previous is None:
                    file_tools._file_ops_cache.pop(task_id, None)
                else:
                    file_tools._file_ops_cache[task_id] = previous

        assert result == session_dir / "nested" / "file.txt"

    def test_session_workspace_uses_docker_workspace_mapping_when_enabled(self, monkeypatch, tmp_path):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools.file_tools import _resolve_path

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "global"))
        monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")
        tokens = set_session_vars(working_dir=str(session_dir))
        try:
            result = _resolve_path("nested/file.txt", task_id="docker-session")
        finally:
            clear_session_vars(tokens)

        assert result == Path("/workspace/nested/file.txt")

    def test_read_file_tool_uses_session_workspace_not_stale_cached_file_ops_env(self, tmp_path):
        """Real read_file_tool execution must not fall back through stale env.cwd."""
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import file_tools
        from tools.file_operations import ShellFileOperations

        stale_live_dir = tmp_path / "stale-live"
        session_dir = tmp_path / "session"
        stale_live_dir.mkdir()
        session_dir.mkdir()
        (stale_live_dir / "foo.txt").write_text("STALE\n", encoding="utf-8")
        (session_dir / "foo.txt").write_text("SESSION\n", encoding="utf-8")

        class FakeEnv:
            def __init__(self):
                self.cwd = str(stale_live_dir)

            def execute(self, command, cwd=None, **_kwargs):
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd or self.cwd,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                return {"output": completed.stdout + completed.stderr, "returncode": completed.returncode}

        task_id = "stale-file-ops-read"
        with file_tools._file_ops_lock:
            previous = file_tools._file_ops_cache.get(task_id)
            file_tools._file_ops_cache[task_id] = ShellFileOperations(FakeEnv(), cwd=str(stale_live_dir))
        tokens = set_session_vars(working_dir=str(session_dir))
        try:
            result = json.loads(file_tools.read_file_tool("foo.txt", task_id=task_id))
        finally:
            clear_session_vars(tokens)
            with file_tools._file_ops_lock:
                if previous is None:
                    file_tools._file_ops_cache.pop(task_id, None)
                else:
                    file_tools._file_ops_cache[task_id] = previous

        assert "SESSION" in result["content"]
        assert "STALE" not in result["content"]

    def test_read_file_tool_blocks_resolved_internal_hub_path(self, monkeypatch, tmp_path):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import file_tools

        hermes_home = tmp_path / ".hermes"
        hub_dir = hermes_home / "skills" / ".hub"
        hub_dir.mkdir(parents=True)
        (hub_dir / "lock.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        tokens = set_session_vars(working_dir=str(hermes_home))
        try:
            result = json.loads(file_tools.read_file_tool("skills/.hub/lock.json"))
        finally:
            clear_session_vars(tokens)

        assert "internal Hermes cache file" in result["error"]

    def test_patch_tool_replace_uses_session_workspace_not_stale_cached_file_ops_env(self, tmp_path):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import file_tools
        from tools.file_operations import ShellFileOperations

        stale_live_dir = tmp_path / "stale-live"
        session_dir = tmp_path / "session"
        stale_live_dir.mkdir()
        session_dir.mkdir()
        (stale_live_dir / "foo.txt").write_text("STALE\n", encoding="utf-8")
        (session_dir / "foo.txt").write_text("SESSION\n", encoding="utf-8")

        class FakeEnv:
            def __init__(self):
                self.cwd = str(stale_live_dir)

            def execute(self, command, cwd=None, **kwargs):
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=cwd or self.cwd,
                    text=True,
                    input=kwargs.get("stdin_data"),
                    capture_output=True,
                    timeout=10,
                )
                return {"output": completed.stdout + completed.stderr, "returncode": completed.returncode}

        task_id = "stale-file-ops-patch"
        with file_tools._file_ops_lock:
            previous = file_tools._file_ops_cache.get(task_id)
            file_tools._file_ops_cache[task_id] = ShellFileOperations(FakeEnv(), cwd=str(stale_live_dir))
        tokens = set_session_vars(working_dir=str(session_dir))
        try:
            result = json.loads(file_tools.patch_tool(
                mode="replace",
                path="foo.txt",
                old_string="SESSION",
                new_string="UPDATED",
                task_id=task_id,
            ))
        finally:
            clear_session_vars(tokens)
            with file_tools._file_ops_lock:
                if previous is None:
                    file_tools._file_ops_cache.pop(task_id, None)
                else:
                    file_tools._file_ops_cache[task_id] = previous

        assert not result.get("error")
        assert (session_dir / "foo.txt").read_text(encoding="utf-8") == "UPDATED\n"
        assert (stale_live_dir / "foo.txt").read_text(encoding="utf-8") == "STALE\n"

    def test_get_file_ops_recreates_env_when_docker_session_workspace_changes(self, monkeypatch, tmp_path):
        from gateway.session_context import clear_session_vars, set_session_vars
        from tools import file_tools

        global_workspace = tmp_path / "global"
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        global_workspace.mkdir()
        workspace_a.mkdir()
        workspace_b.mkdir()

        monkeypatch.setenv("TERMINAL_ENV", "docker")
        monkeypatch.setenv("TERMINAL_CWD", str(global_workspace))
        monkeypatch.setenv("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE", "true")

        created = []

        class DummyEnv:
            def __init__(self, task_id, cwd, host_cwd):
                self.task_id = task_id
                self.cwd = cwd
                self.host_cwd = host_cwd
                self.env = {}

        def fake_create_environment(*, env_type, image, cwd, timeout, ssh_config=None,
                                    container_config=None, local_config=None,
                                    task_id="default", host_cwd=None):
            env = DummyEnv(task_id=task_id, cwd=cwd, host_cwd=host_cwd)
            created.append(env)
            return env

        monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
        monkeypatch.setattr(terminal_tool, "_active_environments", {})
        monkeypatch.setattr(terminal_tool, "_last_activity", {})
        monkeypatch.setattr(terminal_tool, "_creation_locks", {})
        monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

        with file_tools._file_ops_lock:
            previous_cache = dict(file_tools._file_ops_cache)
            file_tools._file_ops_cache.clear()

        tokens = set_session_vars(working_dir=str(workspace_a))
        try:
            ops_a = file_tools._get_file_ops("default")
        finally:
            clear_session_vars(tokens)

        tokens = set_session_vars(working_dir=str(workspace_b))
        try:
            ops_b = file_tools._get_file_ops("default")
        finally:
            clear_session_vars(tokens)
            with file_tools._file_ops_lock:
                file_tools._file_ops_cache.clear()
                file_tools._file_ops_cache.update(previous_cache)

        assert ops_a is not ops_b
        assert ops_a.env is not ops_b.env
        assert len(created) == 2
        assert created[0].host_cwd == str(workspace_a)
        assert created[1].host_cwd == str(workspace_b)
        assert created[0].task_id != created[1].task_id

import os


def test_resolve_cwd_directive_requires_exact_format(tmp_path) -> None:
    from run_agent import AIAgent

    target = tmp_path / "project"
    target.mkdir()

    agent = object.__new__(AIAgent)

    assert agent._resolve_cwd_directive_path(f"cwd ->> {target}") == str(target.resolve())
    assert agent._resolve_cwd_directive_path(f"please cwd ->> {target}") is None
    assert agent._resolve_cwd_directive_path(f"cwd ->> {target} and more") is None


def test_resolve_cwd_directive_accepts_paths_with_spaces(tmp_path) -> None:
    from run_agent import AIAgent

    target = tmp_path / "project with spaces"
    target.mkdir()
    agent = object.__new__(AIAgent)
    agent._gateway_session_key = None

    assert agent._resolve_cwd_directive_path(f"cwd ->> {target}") == str(target.resolve())


def test_run_conversation_handles_cwd_directive_without_llm(tmp_path, monkeypatch) -> None:
    from run_agent import AIAgent

    target = tmp_path / "project"
    target.mkdir()
    monkeypatch.delenv("TERMINAL_CWD", raising=False)

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation(f"cwd ->> {target}")

    assert result["api_calls"] == 0
    assert result["final_response"] == f"Working directory changed to {target.resolve()}"
    assert result["cwd_changed_to"] == str(target.resolve())
    assert os.environ["TERMINAL_CWD"] == str(target.resolve())
    assert result["messages"][-1]["content"] == result["final_response"]


def test_invalid_cwd_directive_rejects_without_llm(tmp_path, monkeypatch) -> None:
    from run_agent import AIAgent

    original = tmp_path / "original"
    original.mkdir()
    missing = tmp_path / "missing"
    monkeypatch.setenv("TERMINAL_CWD", str(original))
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation(f"cwd ->> {missing}")

    assert result["api_calls"] == 0
    assert result["final_response"] == f"Directory does not exist or is not a directory: {missing}"
    assert "cwd_changed_to" not in result
    assert os.environ["TERMINAL_CWD"] == str(original)


def test_apply_session_cwd_restores_last_valid_history_directive(tmp_path, monkeypatch) -> None:
    from run_agent import AIAgent

    original = tmp_path / "original"
    original.mkdir()
    target = tmp_path / "restored"
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))

    agent = object.__new__(AIAgent)
    agent._subdirectory_hints = None

    restored = agent._apply_session_cwd_from_messages(
        "hello",
        [
            {"role": "user", "content": "cwd ->> /does/not/exist"},
            {"role": "assistant", "content": "ignored"},
            {"role": "user", "content": f"cwd ->> {target}"},
        ],
    )

    assert restored == str(target.resolve())
    assert os.environ["TERMINAL_CWD"] == str(target.resolve())
    assert agent._subdirectory_hints.working_dir == target.resolve()


def test_run_conversation_answers_exact_where_cwd_without_terminal(monkeypatch, tmp_path) -> None:
    from run_agent import AIAgent

    target = tmp_path / "project"
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(target))

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation("where cwd")

    assert result["api_calls"] == 0
    assert result["final_response"] == f"Current working directory is {target.resolve()}"


def test_where_cwd_prefers_last_history_directive(tmp_path, monkeypatch) -> None:
    from run_agent import AIAgent

    original = tmp_path / "original"
    original.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation(
        "where cwd",
        conversation_history=[{"role": "user", "content": f"cwd ->> {target}"}],
    )

    assert result["final_response"] == f"Current working directory is {target.resolve()}"


def test_natural_language_cwd_question_is_not_deterministic_query(monkeypatch, tmp_path) -> None:
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)

    assert agent._is_cwd_query_text("지금 cwd 어디야?") is False
    assert agent._is_cwd_query_text("where cwd") is True


def test_gateway_session_where_cwd_ignores_history_directive(monkeypatch, tmp_path) -> None:
    from run_agent import AIAgent

    original = tmp_path / "original"
    original.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))

    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
        gateway_session_key="agent:main:discord:thread:123:123",
    )

    result = agent.run_conversation(
        "where cwd",
        conversation_history=[{"role": "user", "content": f"cwd ->> {target}"}],
    )

    assert result["final_response"] == f"Current working directory is {original.resolve()}"


def test_gateway_session_cwd_directive_does_not_mutate_terminal_env(tmp_path, monkeypatch) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from run_agent import AIAgent

    original = tmp_path / "original"
    target = tmp_path / "target"
    original.mkdir()
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))
    tokens = set_session_vars(working_dir=str(original), effective_cwd=str(original))
    try:
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.test/v1",
            provider="openai",
            model="gpt-test",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
            max_iterations=1,
            gateway_session_key="agent:main:discord:thread:123:123",
        )

        result = agent.run_conversation(f"cwd ->> {target}")
    finally:
        clear_session_vars(tokens)

    assert result["api_calls"] == 0
    assert result["cwd_changed_to"] == str(target.resolve())
    assert os.environ["TERMINAL_CWD"] == str(original)


def test_gateway_session_pdir_directive_returns_project_change_without_llm(tmp_path, monkeypatch) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from run_agent import AIAgent

    original = tmp_path / "original"
    target = tmp_path / "project"
    original.mkdir()
    target.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))
    tokens = set_session_vars(working_dir=str(original), effective_cwd=str(original))
    try:
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.test/v1",
            provider="openai",
            model="gpt-test",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
            max_iterations=1,
            gateway_session_key="agent:main:discord:thread:123:123",
        )

        result = agent.run_conversation(f"pdir ->> {target}")
    finally:
        clear_session_vars(tokens)

    assert result["api_calls"] == 0
    assert result["pdir_changed_to"] == str(target.resolve())
    assert result["cwd_changed_to"] == str(target.resolve())
    assert result["final_response"] == f"Project directory changed to {target.resolve()}"
    assert os.environ["TERMINAL_CWD"] == str(original)


def test_gateway_session_invalid_pdir_directive_rejects_without_llm(tmp_path, monkeypatch) -> None:
    from gateway.session_context import clear_session_vars, set_session_vars
    from run_agent import AIAgent

    original = tmp_path / "original"
    missing = tmp_path / "missing"
    original.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(original))
    tokens = set_session_vars(working_dir=str(original), effective_cwd=str(original))
    try:
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.test/v1",
            provider="openai",
            model="gpt-test",
            skip_memory=True,
            skip_context_files=True,
            quiet_mode=True,
            max_iterations=1,
            gateway_session_key="agent:main:discord:thread:123:123",
        )

        result = agent.run_conversation(f"pdir ->> {missing}")
    finally:
        clear_session_vars(tokens)

    assert result["api_calls"] == 0
    assert result["final_response"] == f"Directory does not exist or is not a directory: {missing}"
    assert "cwd_changed_to" not in result
    assert os.environ["TERMINAL_CWD"] == str(original)


def test_non_gateway_pdir_directive_is_deterministic_no_llm(tmp_path) -> None:
    from run_agent import AIAgent

    target = tmp_path / "project"
    target.mkdir()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation(f"pdir ->> {target}")

    assert result["api_calls"] == 0
    assert result["final_response"] == "Project directory directives are only supported in gateway sessions."


def test_non_gateway_missing_pdir_directive_is_deterministic_no_llm(tmp_path) -> None:
    from run_agent import AIAgent

    missing = tmp_path / "missing"
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.test/v1",
        provider="openai",
        model="gpt-test",
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
    )

    result = agent.run_conversation(f"pdir ->> {missing}")

    assert result["api_calls"] == 0
    assert result["final_response"] == "Project directory directives are only supported in gateway sessions."

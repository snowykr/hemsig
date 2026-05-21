"""Tests for gateway session management."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from gateway.config import Platform, HomeChannel, GatewayConfig, PlatformConfig
from gateway.session import (
    SessionEntry,
    SessionSource,
    SessionStore,
    build_session_context,
    build_session_context_prompt,
    build_session_key,
)
from datetime import datetime



class TestSessionWorkspaceState:
    def test_session_entry_from_dict_rejects_unknown_platforms(self):
        with pytest.raises(ValueError):
            SessionEntry.from_dict(
                {
                    "session_key": "legacy-unknown",
                    "session_id": "sid",
                    "created_at": "2025-01-01T00:00:00",
                    "updated_at": "2025-01-01T00:00:00",
                    "platform": "legacychat",
                    "origin": {
                        "platform": "legacychat",
                        "chat_id": "123",
                        "chat_type": "dm",
                        "user_id": "u1",
                    },
                }
            )

    def test_session_entry_workspace_roundtrip_and_legacy_defaults(self):
        entry = SessionEntry(
            session_key="key",
            session_id="sid",
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 2),
            project_dir="/tmp/project",
            working_dir="/tmp/project/src",
        )

        restored = SessionEntry.from_dict(entry.to_dict())

        assert restored.project_dir == "/tmp/project"
        assert restored.working_dir == "/tmp/project/src"

        legacy = SessionEntry.from_dict({
            "session_key": "legacy",
            "session_id": "sid",
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
        })
        assert legacy.project_dir is None
        assert legacy.working_dir is None

    def test_workspace_state_survives_reset_resume_pending_and_switch(self, tmp_path):
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        store._db = None
        store._loaded = True

        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            chat_type="thread",
            thread_id="thread-1",
            user_id="alice",
        )
        entry = store.get_or_create_session(source)
        entry.project_dir = "/tmp/project"
        entry.working_dir = "/tmp/project/app"
        store._save()

        assert store.mark_resume_pending(entry.session_key) is True
        resumed = store.get_or_create_session(source)
        assert resumed.session_id == entry.session_id
        assert resumed.project_dir == "/tmp/project"
        assert resumed.working_dir == "/tmp/project/app"

        reset = store.reset_session(entry.session_key)
        assert reset is not None
        assert reset.session_id != entry.session_id
        assert reset.project_dir == "/tmp/project"
        assert reset.working_dir == "/tmp/project/app"

        switched = store.switch_session(entry.session_key, "older-session")
        assert switched is not None
        assert switched.session_id == "older-session"
        assert switched.project_dir == "/tmp/project"
        assert switched.working_dir == "/tmp/project/app"

    def test_discord_thread_workspace_state_is_keyed_per_thread(self, tmp_path):
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
        store._db = None
        store._loaded = True

        thread_a = SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            chat_type="thread",
            thread_id="thread-a",
            user_id="alice",
        )
        thread_b = SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            chat_type="thread",
            thread_id="thread-b",
            user_id="alice",
        )

        entry_a = store.get_or_create_session(thread_a)
        entry_b = store.get_or_create_session(thread_b)
        store.update_workspace(entry_a.session_key, project_dir="/tmp/a", working_dir="/tmp/a/src")
        store.update_workspace(entry_b.session_key, project_dir="/tmp/b", working_dir="/tmp/b/src")

        assert store.get_or_create_session(thread_a).working_dir == "/tmp/a/src"
        assert store.get_or_create_session(thread_b).working_dir == "/tmp/b/src"
        assert entry_a.session_key != entry_b.session_key


class TestSessionKeyConsistency:
    """Regression: Session keys should remain stable across store helpers."""

    @pytest.fixture()
    def store(self, tmp_path):
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            s = SessionStore(sessions_dir=tmp_path, config=config)
        s._db = None
        s._loaded = True
        return s

    def test_store_delegates_to_build_session_key(self, store):
        """SessionStore._generate_session_key must produce the same result."""
        source = SessionSource(
            platform=Platform.SIGNAL,
            chat_id="+15551234567",
            chat_type="dm",
            user_name="Phone User",
        )
        assert store._generate_session_key(source) == build_session_key(source)

    def test_store_creates_distinct_group_sessions_per_user(self, store):
        first = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="alice",
            user_name="Alice",
        )
        second = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="bob",
            user_name="Bob",
        )

        first_entry = store.get_or_create_session(first)
        second_entry = store.get_or_create_session(second)

        assert first_entry.session_key == "agent:main:discord:group:guild-123:alice"
        assert second_entry.session_key == "agent:main:discord:group:guild-123:bob"
        assert first_entry.session_id != second_entry.session_id

    def test_store_shares_group_sessions_when_disabled_in_config(self, store):
        store.config.group_sessions_per_user = False

        first = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="alice",
            user_name="Alice",
        )
        second = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="bob",
            user_name="Bob",
        )

        first_entry = store.get_or_create_session(first)
        second_entry = store.get_or_create_session(second)

        assert first_entry.session_key == "agent:main:discord:group:guild-123"
        assert second_entry.session_key == "agent:main:discord:group:guild-123"
        assert first_entry.session_id == second_entry.session_id

    def test_telegram_dm_includes_chat_id(self):
        """DMs should include chat_id to separate users."""
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="99",
            chat_type="dm",
        )
        key = build_session_key(source)
        assert key == "agent:main:telegram:dm:99"

    def test_distinct_dm_chat_ids_get_distinct_session_keys(self):
        """Different DM chats must not collapse into one shared session."""
        first = SessionSource(platform=Platform.TELEGRAM, chat_id="99", chat_type="dm")
        second = SessionSource(platform=Platform.TELEGRAM, chat_id="100", chat_type="dm")

        assert build_session_key(first) == "agent:main:telegram:dm:99"
        assert build_session_key(second) == "agent:main:telegram:dm:100"
        assert build_session_key(first) != build_session_key(second)

    def test_discord_group_includes_chat_id(self):
        """Group/channel keys include chat_type and chat_id."""
        source = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
        )
        key = build_session_key(source)
        assert key == "agent:main:discord:group:guild-123"

    def test_group_sessions_are_isolated_per_user_when_user_id_present(self):
        first = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="alice",
        )
        second = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="bob",
        )

        assert build_session_key(first) == "agent:main:discord:group:guild-123:alice"
        assert build_session_key(second) == "agent:main:discord:group:guild-123:bob"
        assert build_session_key(first) != build_session_key(second)

    def test_group_sessions_can_be_shared_when_isolation_disabled(self):
        first = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="alice",
        )
        second = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="group",
            user_id="bob",
        )

        assert build_session_key(first, group_sessions_per_user=False) == "agent:main:discord:group:guild-123"
        assert build_session_key(second, group_sessions_per_user=False) == "agent:main:discord:group:guild-123"

    def test_store_loads_sessions_with_unknown_platforms(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = GatewayConfig()
        store = SessionStore(sessions_dir=hermes_home / "sessions", config=config)
        store._db = None

        store.sessions_dir.mkdir(parents=True, exist_ok=True)
        (store.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "legacy": {
                        "session_key": "agent:main:legacychat:dm:123",
                        "session_id": "sid",
                        "created_at": "2025-01-01T00:00:00",
                        "updated_at": "2025-01-01T00:00:00",
                        "platform": "legacychat",
                        "origin": {
                            "platform": "legacychat",
                            "chat_id": "123",
                            "chat_type": "dm",
                            "user_id": "u1",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        store._ensure_loaded()

        loaded = store._entries["legacy"]
        assert loaded.platform is None
        assert loaded.platform_name == "legacychat"
        assert loaded.origin is not None
        assert loaded.origin.platform is None
        assert loaded.origin.platform_name == "legacychat"

        store._save()
        saved = json.loads((store.sessions_dir / "sessions.json").read_text(encoding="utf-8"))
        assert saved["legacy"]["platform"] == "legacychat"
        assert saved["legacy"]["origin"]["platform"] == "legacychat"

    def test_store_still_skips_malformed_session_entries(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        config = GatewayConfig()
        store = SessionStore(sessions_dir=hermes_home / "sessions", config=config)
        store._db = None

        store.sessions_dir.mkdir(parents=True, exist_ok=True)
        (store.sessions_dir / "sessions.json").write_text(
            json.dumps(
                {
                    "legacy": {
                        "session_key": "agent:main:legacychat:dm:123",
                        "session_id": "sid",
                        "created_at": "2025-01-01T00:00:00",
                        "updated_at": "2025-01-01T00:00:00",
                        "platform": "legacychat",
                    },
                    "broken": {
                        "session_key": "broken",
                        "created_at": "2025-01-01T00:00:00",
                        "updated_at": "2025-01-01T00:00:00",
                    },
                }
            ),
            encoding="utf-8",
        )

        store._ensure_loaded()

        assert "legacy" in store._entries
        assert "broken" not in store._entries

    def test_group_thread_includes_thread_id(self):
        """Forum-style threads need a distinct session key within one group."""
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            thread_id="17585",
        )
        key = build_session_key(source)
        assert key == "agent:main:telegram:group:-1002285219667:17585"

    def test_group_thread_sessions_are_shared_by_default(self):
        """Threads default to shared sessions — user_id is NOT appended."""
        alice = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            thread_id="17585",
            user_id="alice",
        )
        bob = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            thread_id="17585",
            user_id="bob",
        )
        assert build_session_key(alice) == "agent:main:telegram:group:-1002285219667:17585"
        assert build_session_key(bob) == "agent:main:telegram:group:-1002285219667:17585"
        assert build_session_key(alice) == build_session_key(bob)

    def test_group_thread_sessions_can_be_isolated_per_user(self):
        """thread_sessions_per_user=True restores per-user isolation in threads."""
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            thread_id="17585",
            user_id="42",
        )
        key = build_session_key(source, thread_sessions_per_user=True)
        assert key == "agent:main:telegram:group:-1002285219667:17585:42"

    def test_non_thread_group_sessions_still_isolated_per_user(self):
        """Regular group messages (no thread_id) remain per-user by default."""
        alice = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            user_id="alice",
        )
        bob = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1002285219667",
            chat_type="group",
            user_id="bob",
        )
        assert build_session_key(alice) == "agent:main:telegram:group:-1002285219667:alice"
        assert build_session_key(bob) == "agent:main:telegram:group:-1002285219667:bob"
        assert build_session_key(alice) != build_session_key(bob)

    def test_discord_thread_sessions_shared_by_default(self):
        """Discord threads are shared across participants by default."""
        alice = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="thread",
            thread_id="thread-456",
            user_id="alice",
        )
        bob = SessionSource(
            platform=Platform.DISCORD,
            chat_id="guild-123",
            chat_type="thread",
            thread_id="thread-456",
            user_id="bob",
        )
        assert build_session_key(alice) == build_session_key(bob)
        assert "alice" not in build_session_key(alice)
        assert "bob" not in build_session_key(bob)

    def test_dm_thread_sessions_not_affected(self):
        """DM threads use their own keying logic and are not affected."""
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="99",
            chat_type="dm",
            thread_id="topic-1",
            user_id="42",
        )
        key = build_session_key(source)
        # DM logic: chat_id + thread_id, user_id never included
        assert key == "agent:main:telegram:dm:99:topic-1"


class TestSessionStoreEntriesAttribute:
    """Regression: /reset must access _entries, not _sessions."""

    def test_entries_attribute_exists(self):
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=Path("/tmp"), config=config)
        store._loaded = True
        assert hasattr(store, "_entries")
        assert not hasattr(store, "_sessions")


class TestHasAnySessions:
    """Tests for has_any_sessions() fix (issue #351)."""

    @pytest.fixture
    def store_with_mock_db(self, tmp_path):
        """SessionStore with a mocked database."""
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            s = SessionStore(sessions_dir=tmp_path, config=config)
        s._loaded = True
        s._entries = {}
        s._db = MagicMock()
        return s

    def test_uses_database_count_when_available(self, store_with_mock_db):
        """has_any_sessions should use database session_count, not len(_entries)."""
        store = store_with_mock_db
        # Simulate single-platform user with only 1 entry in memory
        store._entries = {"telegram:12345": MagicMock()}
        # But database has 3 sessions (current + 2 previous resets)
        store._db.session_count.return_value = 3

        assert store.has_any_sessions() is True
        store._db.session_count.assert_called_once()

    def test_first_session_ever_returns_false(self, store_with_mock_db):
        """First session ever should return False (only current session in DB)."""
        store = store_with_mock_db
        store._entries = {"telegram:12345": MagicMock()}
        # Database has exactly 1 session (the current one just created)
        store._db.session_count.return_value = 1

        assert store.has_any_sessions() is False

    def test_fallback_without_database(self, tmp_path):
        """Should fall back to len(_entries) when DB is not available."""
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._loaded = True
        store._db = None
        store._entries = {"key1": MagicMock(), "key2": MagicMock()}

        # > 1 entries means has sessions
        assert store.has_any_sessions() is True

        store._entries = {"key1": MagicMock()}
        assert store.has_any_sessions() is False


class TestLastPromptTokens:
    """Tests for the last_prompt_tokens field — actual API token tracking."""

    def test_session_entry_default(self):
        """New sessions should have last_prompt_tokens=0."""
        from gateway.session import SessionEntry
        from datetime import datetime
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        assert entry.last_prompt_tokens == 0

    def test_session_entry_roundtrip(self):
        """last_prompt_tokens should survive serialization/deserialization."""
        from gateway.session import SessionEntry
        from datetime import datetime
        entry = SessionEntry(
            session_key="test",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            last_prompt_tokens=42000,
        )
        d = entry.to_dict()
        assert d["last_prompt_tokens"] == 42000
        restored = SessionEntry.from_dict(d)
        assert restored.last_prompt_tokens == 42000

    def test_session_entry_from_old_data(self):
        """Old session data without last_prompt_tokens should default to 0."""
        from gateway.session import SessionEntry
        data = {
            "session_key": "test",
            "session_id": "s1",
            "created_at": "2025-01-01T00:00:00",
            "updated_at": "2025-01-01T00:00:00",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            # No last_prompt_tokens — old format
        }
        entry = SessionEntry.from_dict(data)
        assert entry.last_prompt_tokens == 0

    def test_update_session_sets_last_prompt_tokens(self, tmp_path):
        """update_session should store the actual prompt token count."""
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._loaded = True
        store._db = None
        store._save = MagicMock()

        from gateway.session import SessionEntry
        from datetime import datetime
        entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        store._entries = {"k1": entry}

        store.update_session("k1", last_prompt_tokens=85000)
        assert entry.last_prompt_tokens == 85000

    def test_update_session_none_does_not_change(self, tmp_path):
        """update_session with default (None) should not change last_prompt_tokens."""
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._loaded = True
        store._db = None
        store._save = MagicMock()

        from gateway.session import SessionEntry
        from datetime import datetime
        entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            last_prompt_tokens=50000,
        )
        store._entries = {"k1": entry}

        store.update_session("k1")  # No last_prompt_tokens arg
        assert entry.last_prompt_tokens == 50000  # unchanged

    def test_update_session_zero_resets(self, tmp_path):
        """update_session with last_prompt_tokens=0 should reset the field."""
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._loaded = True
        store._db = None
        store._save = MagicMock()

        from gateway.session import SessionEntry
        from datetime import datetime
        entry = SessionEntry(
            session_key="k1",
            session_id="s1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            last_prompt_tokens=85000,
        )
        store._entries = {"k1": entry}

        store.update_session("k1", last_prompt_tokens=0)
        assert entry.last_prompt_tokens == 0

class TestRewriteTranscriptPreservesReasoning:
    """rewrite_transcript must not drop reasoning fields from SQLite."""

    def test_reasoning_survives_rewrite(self, tmp_path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "test.db")
        session_id = "reasoning-test"
        db.create_session(session_id=session_id, source="cli")

        # Insert a message WITH all three reasoning fields
        db.append_message(
            session_id=session_id,
            role="assistant",
            content="The answer is 42.",
            reasoning="I need to think step by step.",
            reasoning_content="provider scratchpad",
            reasoning_details=[{"type": "summary", "text": "step by step"}],
            codex_reasoning_items=[{"id": "r1", "type": "reasoning"}],
        )

        # Verify all three were stored
        before = db.get_messages_as_conversation(session_id)
        assert before[0].get("reasoning") == "I need to think step by step."
        assert before[0].get("reasoning_content") == "provider scratchpad"
        assert before[0].get("reasoning_details") == [{"type": "summary", "text": "step by step"}]
        assert before[0].get("codex_reasoning_items") == [{"id": "r1", "type": "reasoning"}]

        # Now simulate /retry: build the SessionStore and call rewrite_transcript
        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        # rewrite_transcript receives the messages that load_transcript returned
        store.rewrite_transcript(session_id, before)

        # Load again — all three reasoning fields must survive
        after = db.get_messages_as_conversation(session_id)
        assert after[0].get("reasoning") == "I need to think step by step."
        assert after[0].get("reasoning_content") == "provider scratchpad"
        assert after[0].get("reasoning_details") == [{"type": "summary", "text": "step by step"}]
        assert after[0].get("codex_reasoning_items") == [{"id": "r1", "type": "reasoning"}]

    def test_db_rewrite_is_atomic_on_insert_failure(self, tmp_path, monkeypatch):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "test.db")
        session_id = "atomic-rewrite-test"
        db.create_session(session_id=session_id, source="cli")
        db.append_message(session_id=session_id, role="user", content="before user")
        db.append_message(session_id=session_id, role="assistant", content="before assistant")

        config = GatewayConfig()
        with patch("gateway.session.SessionStore._ensure_loaded"):
            store = SessionStore(sessions_dir=tmp_path, config=config)
        store._db = db
        store._loaded = True

        # Force the second insert inside replace_messages to fail, simulating
        # any storage-layer error that might abort a multi-row rewrite.
        real_encode = SessionDB._encode_content
        calls = {"n": 0}

        def flaky_encode(cls, content):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated storage failure")
            return real_encode.__func__(cls, content)

        monkeypatch.setattr(SessionDB, "_encode_content", classmethod(flaky_encode))

        replacement = [
            {"role": "user", "content": "after user"},
            {"role": "assistant", "content": "after assistant"},
        ]

        store.rewrite_transcript(session_id, replacement)

        # The rewrite must roll back atomically — original messages preserved.
        after = db.get_messages_as_conversation(session_id)
        assert [msg["content"] for msg in after] == [
            "before user",
            "before assistant",
        ]

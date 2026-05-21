"""Tests for tools/send_message_tool.py."""

import json
import asyncio
import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from gateway.platforms.base import SendResult
from tools.send_message_tool import _parse_target_ref, _send_to_platform, _send_with_connected_adapter, send_message_tool


class _AsyncAdapter:
    def __init__(self, result: SendResult):
        self.result = result
        self.calls = []

    async def send(self, *, chat_id: str, content: str, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return self.result


class _StandaloneAdapter:
    def __init__(self, result: SendResult, calls: list[str]):
        self.result = result
        self.calls = calls

    async def connect(self):
        self.calls.append("connect")
        return True

    async def send(self, *, chat_id: str, content: str, metadata=None):
        self.calls.append(f"send:{chat_id}:{content}:{metadata}")
        return self.result

    async def disconnect(self):
        self.calls.append("disconnect")


def _platform_config(enabled: bool = True, home_channel: str | None = "home-chat"):
    return SimpleNamespace(
        enabled=enabled,
        home_channel=SimpleNamespace(chat_id=home_channel) if home_channel else None,
    )


def _gateway_config(platform=Platform.TELEGRAM, pconfig=None):
    return SimpleNamespace(platforms={platform: pconfig or _platform_config()})



def test_send_message_config_errors_scrub_secret_patterns_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    secret_error = (
        "gateway failed for https://example.test/callback?token=query-secret&ok=1 "
        "with api_key=assignment-secret and signature = sig-secret"
    )

    with patch("gateway.config.load_gateway_config", side_effect=RuntimeError(secret_error)):
        result = json.loads(send_message_tool({"target": "telegram:123", "message": "hello"}))

    error = result["error"]
    assert "query-secret" not in error
    assert "assignment-secret" not in error
    assert "sig-secret" not in error
    assert "token=***" in error
    assert "api_key=***" in error
    assert "signature=***" in error
    assert "gateway failed" in error


def test_parse_target_ref_kept_platforms():
    assert _parse_target_ref("telegram", "-100123:456") == ("-100123", "456", True)
    assert _parse_target_ref("discord", "123456:789") == ("123456", "789", True)
    assert _parse_target_ref("slack", "C0B0QV5434G") == ("C0B0QV5434G", None, True)
    assert _parse_target_ref("signal", "+15551234567") == ("+15551234567", None, True)
    assert _parse_target_ref("email", "person@example.com") == ("person@example.com", None, True)


def test_parse_target_ref_rejects_unknown_phone_platforms():
    assert _parse_target_ref("legacyphone", "+15551234567")[2] is False
    assert _parse_target_ref("oldchat", "+15551234567")[2] is False


def test_send_uses_live_adapter_with_explicit_target():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-1"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner):
        result = json.loads(send_message_tool({"target": "telegram:-100123:456", "message": "hello"}))

    assert result == {
        "success": True,
        "platform": "telegram",
        "chat_id": "-100123",
        "message_id": "msg-1",
    }
    assert adapter.calls == [{"chat_id": "-100123", "content": "hello", "metadata": {"thread_id": "456"}}]


def test_send_success_mirrors_to_target_session():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-1"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram:-100123:456", "message": "hello"}))

    assert result["mirrored"] is True
    mirror_mock.assert_called_once_with(
        "telegram",
        "-100123",
        "hello",
        source_label="cli",
        thread_id="456",
    )


def test_send_success_mirror_passes_session_user_id():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-1"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    def _get_session_env(name, default=""):
        if name == "HERMES_SESSION_PLATFORM":
            return "telegram"
        if name == "HERMES_SESSION_USER_ID":
            return "alice"
        return default

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram:-100123:456", "message": "hello"}))

    assert result["mirrored"] is True
    mirror_mock.assert_called_once_with(
        "telegram",
        "-100123",
        "hello",
        source_label="telegram",
        thread_id="456",
        user_id="alice",
    )


def test_send_uses_home_channel_when_target_has_no_chat_id():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-home"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner):
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result["success"] is True
    assert result["chat_id"] == "home-chat"
    assert adapter.calls[0]["metadata"] is None


def test_send_errors_when_platform_unknown():
    result = json.loads(send_message_tool({"target": "oldchat:channel-1", "message": "hello"}))
    assert "error" in result
    assert "oldchat" in result["error"]


def test_send_falls_back_without_live_adapter():
    runner = SimpleNamespace(adapters={})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat"}
    send_mock.assert_awaited_once()


def test_standalone_send_success_mirrors_to_target_session():
    runner = SimpleNamespace(adapters={})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat", "mirrored": True}
    send_mock.assert_awaited_once()
    mirror_mock.assert_called_once_with(
        "telegram",
        "home-chat",
        "hello",
        source_label="cli",
        thread_id=None,
    )


def test_standalone_send_mirror_passes_session_user_id():
    runner = SimpleNamespace(adapters={})

    def _get_session_env(name, default=""):
        if name == "HERMES_SESSION_PLATFORM":
            return "telegram"
        if name == "HERMES_SESSION_USER_ID":
            return "alice"
        return default

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat", "mirrored": True}
    send_mock.assert_awaited_once()
    mirror_mock.assert_called_once_with(
        "telegram",
        "home-chat",
        "hello",
        source_label="telegram",
        thread_id=None,
        user_id="alice",
    )


def test_send_message_skips_duplicate_cron_auto_delivery_target():
    runner = SimpleNamespace(adapters={})

    def _get_session_env(name, default=""):
        mapping = {
            "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "home-chat",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID": "",
        }
        return mapping.get(name, default)

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=AssertionError("send path should not be used"))):
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result == {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": "telegram:home-chat",
        "note": (
            "Skipped send_message to telegram:home-chat. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


def test_send_message_does_not_skip_non_matching_cron_auto_delivery_target():
    runner = SimpleNamespace(adapters={})

    def _get_session_env(name, default=""):
        mapping = {
            "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "different-chat",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID": "",
        }
        return mapping.get(name, default)

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat"}
    send_mock.assert_awaited_once()


def test_send_message_skips_duplicate_cron_auto_delivery_target_with_thread_id():
    runner = SimpleNamespace(adapters={})

    def _get_session_env(name, default=""):
        mapping = {
            "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "-100123",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID": "456",
        }
        return mapping.get(name, default)

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config(pconfig=_platform_config(enabled=True, home_channel=None))), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=AssertionError("send path should not be used"))):
        result = json.loads(send_message_tool({"target": "telegram:-100123:456", "message": "hello"}))

    assert result == {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": "telegram:-100123:456",
        "note": (
            "Skipped send_message to telegram:-100123:456. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


def test_send_message_skips_duplicate_cron_auto_delivery_secondary_target():
    runner = SimpleNamespace(adapters={})

    def _get_session_env(name, default=""):
        mapping = {
            "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
            "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "home-chat",
            "HERMES_CRON_AUTO_DELIVER_THREAD_ID": "",
            "HERMES_CRON_AUTO_DELIVER_TARGETS": json.dumps([
                {"platform": "telegram", "chat_id": "home-chat", "thread_id": None},
                {"platform": "discord", "chat_id": "222", "thread_id": None},
            ]),
        }
        return mapping.get(name, default)

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config(platform=Platform.DISCORD, pconfig=_platform_config(enabled=True, home_channel=None))), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.session_context.get_session_env", side_effect=_get_session_env), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=AssertionError("send path should not be used"))):
        result = json.loads(send_message_tool({"target": "discord:222", "message": "hello"}))

    assert result == {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": "discord:222",
        "note": (
            "Skipped send_message to discord:222. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


def test_send_media_message_uses_send_to_platform_with_cleaned_text_and_media_files():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-raw"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock:
        result = json.loads(
            send_message_tool({"target": "telegram", "message": "hello\nMEDIA:/tmp/chart.png"})
        )

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat"}
    assert adapter.calls == []
    send_mock.assert_awaited_once_with(
        Platform.TELEGRAM,
        _gateway_config().platforms[Platform.TELEGRAM],
        "home-chat",
        "hello",
        thread_id=None,
        media_files=[("/tmp/chart.png", False)],
    )


def test_send_media_only_message_uses_send_to_platform_with_empty_cleaned_text():
    adapter = _AsyncAdapter(SendResult(success=True, message_id="msg-raw"))
    runner = SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "MEDIA:/tmp/chart.png"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat"}
    assert adapter.calls == []
    send_mock.assert_awaited_once_with(
        Platform.TELEGRAM,
        _gateway_config().platforms[Platform.TELEGRAM],
        "home-chat",
        "",
        thread_id=None,
        media_files=[("/tmp/chart.png", False)],
    )


def test_send_media_only_success_mirrors_attachment_summary():
    runner = SimpleNamespace(adapters={})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "MEDIA:/tmp/chart.png"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat", "mirrored": True}
    mirror_mock.assert_called_once_with(
        "telegram",
        "home-chat",
        "Sent attachment: chart.png",
        source_label="cli",
        thread_id=None,
    )


def test_send_multi_media_only_success_mirrors_attachment_summary():
    runner = SimpleNamespace(adapters={})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(
            send_message_tool({
                "target": "telegram",
                "message": "MEDIA:/tmp/chart.png\nMEDIA:/tmp/report.pdf",
            })
        )

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat", "mirrored": True}
    send_mock.assert_awaited_once_with(
        Platform.TELEGRAM,
        _gateway_config().platforms[Platform.TELEGRAM],
        "home-chat",
        "",
        thread_id=None,
        media_files=[("/tmp/chart.png", False), ("/tmp/report.pdf", False)],
    )
    mirror_mock.assert_called_once_with(
        "telegram",
        "home-chat",
        "Sent 2 attachments: chart.png, report.pdf",
        source_label="cli",
        thread_id=None,
    )


def test_send_text_plus_media_success_mirrors_cleaned_text_only():
    runner = SimpleNamespace(adapters={})

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(send_message_tool({"target": "telegram", "message": "hello\nMEDIA:/tmp/chart.png"}))

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat", "mirrored": True}
    mirror_mock.assert_called_once_with(
        "telegram",
        "home-chat",
        "hello",
        source_label="cli",
        thread_id=None,
    )


def test_send_signal_media_uses_live_adapter_when_gateway_connected(tmp_path):
    img_path = tmp_path / "chart.png"
    img_path.write_bytes(b"\x89PNG")
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _LiveSignalAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, *, chat_id: str, content: str, metadata=None):
            self.calls.append(("send", chat_id, content, metadata))
            return SendResult(success=True, message_id="msg-1")

        async def send_multiple_images(self, *, chat_id: str, images, metadata=None):
            self.calls.append(("send_multiple_images", chat_id, images, metadata))
            return SendResult(success=True)

        async def send_document(self, *, chat_id: str, file_path: str, metadata=None):
            self.calls.append(("send_document", chat_id, file_path, metadata))
            return SendResult(success=True, message_id="doc-1")

    adapter = _LiveSignalAdapter()
    runner = SimpleNamespace(adapters={Platform.SIGNAL: adapter})
    pconfig = SimpleNamespace(enabled=True, extra={}, home_channel=SimpleNamespace(chat_id="+15551234567"))
    config = SimpleNamespace(platforms={Platform.SIGNAL: pconfig})

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock, \
         patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=AssertionError("standalone path should not be used"))):
        result = json.loads(
            send_message_tool({
                "target": "signal",
                "message": f"hello\nMEDIA:{img_path}\nMEDIA:{pdf_path}",
            })
        )

    assert result["success"] is True
    assert result["mirrored"] is True
    assert adapter.calls == [
        ("send", "+15551234567", "hello", None),
        ("send_multiple_images", "+15551234567", [(img_path.as_uri(), "")], None),
        ("send_document", "+15551234567", str(pdf_path), None),
    ]
    mirror_mock.assert_called_once_with(
        "signal",
        "+15551234567",
        "hello",
        source_label="cli",
        thread_id=None,
    )


def test_send_to_platform_allows_media_only_for_telegram():
    with patch("tools.send_message_tool._send_telegram", new=AsyncMock(return_value={"success": True, "platform": "telegram", "chat_id": "home-chat"})) as send_mock:
        result = asyncio.run(
            _send_to_platform(
                Platform.TELEGRAM,
                _gateway_config(),
                "home-chat",
                "",
                media_files=[("/tmp/chart.png", False)],
            )
        )

    assert result == {"success": True, "platform": "telegram", "chat_id": "home-chat"}
    send_mock.assert_awaited_once_with(
        _gateway_config().platforms[Platform.TELEGRAM],
        "home-chat",
        "",
        thread_id=None,
        media_files=[("/tmp/chart.png", False)],
    )


def test_send_to_platform_allows_media_only_for_discord():
    with patch("tools.send_message_tool._send_discord", new=AsyncMock(return_value={"success": True, "platform": "discord", "chat_id": "home-chat"})) as send_mock:
        result = asyncio.run(
            _send_to_platform(
                Platform.DISCORD,
                _gateway_config(platform=Platform.DISCORD),
                "home-chat",
                "",
                media_files=[("/tmp/chart.png", False)],
            )
        )

    assert result == {"success": True, "platform": "discord", "chat_id": "home-chat"}
    send_mock.assert_awaited_once_with(
        _gateway_config(platform=Platform.DISCORD).platforms[Platform.DISCORD],
        "home-chat",
        "",
        thread_id=None,
        media_files=[("/tmp/chart.png", False)],
    )


def test_send_with_connected_adapter_connects_and_disconnects_for_standalone_delivery():
    calls = []

    def adapter_cls(_pconfig):
        return _StandaloneAdapter(SendResult(success=True, message_id="msg-2"), calls)

    result = asyncio.run(
        _send_with_connected_adapter(
            adapter_cls,
            _platform_config(),
            Platform.TELEGRAM,
            "-100123",
            "hello",
            thread_id="456",
        )
    )

    assert result == {
        "success": True,
        "platform": "telegram",
        "chat_id": "-100123",
        "message_id": "msg-2",
    }
    assert calls == [
        "connect",
        "send:-100123:hello:{'thread_id': '456'}",
        "disconnect",
    ]


def test_send_to_platform_supports_registered_plugin_platforms():
    from gateway.platform_registry import PlatformEntry, platform_registry

    calls = []
    entry = PlatformEntry(
        name="cronchat",
        label="Cron Chat",
        adapter_factory=lambda _pconfig: _StandaloneAdapter(SendResult(success=True, message_id="plugin-msg"), calls),
        check_fn=lambda: True,
        source="plugin",
    )
    platform_registry.register(entry)
    try:
        plugin_platform = Platform("cronchat")
        result = asyncio.run(
            _send_to_platform(
                plugin_platform,
                _gateway_config(platform=plugin_platform),
                "room-1",
                "hello plugin",
                thread_id="thread-9",
            )
        )
    finally:
        platform_registry.unregister("cronchat")

    assert result == {
        "success": True,
        "platform": "cronchat",
        "chat_id": "room-1",
        "message_id": "plugin-msg",
    }
    assert calls == [
        "connect",
        "send:room-1:hello plugin:{'thread_id': 'thread-9'}",
        "disconnect",
    ]


def test_send_message_tool_allows_registered_plugin_explicit_targets():
    from gateway.platform_registry import PlatformEntry, platform_registry

    entry = PlatformEntry(
        name="directchat",
        label="Direct Chat",
        adapter_factory=lambda _pconfig: None,
        check_fn=lambda: True,
        source="plugin",
    )
    platform_registry.register(entry)
    try:
        plugin_platform = Platform("directchat")
        config = SimpleNamespace(platforms={plugin_platform: _platform_config(enabled=True, home_channel=None)})
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("gateway.run._gateway_runner_ref", return_value=SimpleNamespace(adapters={})), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "directchat", "chat_id": "room-1"})) as send_mock:
            result = json.loads(send_message_tool({"target": "directchat:room-1", "message": "hello"}))
    finally:
        platform_registry.unregister("directchat")

    assert result == {"success": True, "platform": "directchat", "chat_id": "room-1"}
    send_mock.assert_awaited_once_with(
        plugin_platform,
        config.platforms[plugin_platform],
        "room-1",
        "hello",
        thread_id=None,
        media_files=[],
    )


def test_send_message_tool_discovers_plugin_platforms_before_target_resolution():
    from gateway.platform_registry import PlatformEntry, platform_registry

    load_calls = []

    def _load_config_after_plugin_discovery():
        load_calls.append(True)
        entry = PlatformEntry(
            name="latechat",
            label="Late Chat",
            adapter_factory=lambda _pconfig: None,
            check_fn=lambda: True,
            source="plugin",
        )
        platform_registry.register(entry)
        plugin_platform = Platform("latechat")
        return SimpleNamespace(
            platforms={plugin_platform: _platform_config(enabled=True, home_channel=None)}
        )

    try:
        with patch("gateway.config.load_gateway_config", side_effect=_load_config_after_plugin_discovery), \
             patch("gateway.channel_directory.resolve_channel_name", return_value=None), \
             patch("gateway.run._gateway_runner_ref", return_value=SimpleNamespace(adapters={})), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True, "platform": "latechat", "chat_id": "room-1"})) as send_mock:
            result = json.loads(send_message_tool({"target": "latechat:room-1", "message": "hello"}))
    finally:
        platform_registry.unregister("latechat")

    assert load_calls == [True]
    assert result == {"success": True, "platform": "latechat", "chat_id": "room-1"}
    send_mock.assert_awaited_once()
    platform, pconfig, chat_id, message = send_mock.await_args.args
    assert platform.value == "latechat"
    assert pconfig.enabled is True
    assert chat_id == "room-1"
    assert message == "hello"
    assert send_mock.await_args.kwargs == {"thread_id": None, "media_files": []}


def test_list_action_returns_formatted_directory():
    with patch("gateway.channel_directory.format_directory_for_display", return_value="telegram: home"):
        result = json.loads(send_message_tool({"action": "list"}))

    assert result == {"targets": "telegram: home"}


def _install_fake_telegram_modules(monkeypatch):
    telegram_mod = ModuleType("telegram")
    telegram_constants_mod = ModuleType("telegram.constants")

    class Bot:
        def __init__(self, token):
            self.token = token

    class ParseMode:
        HTML = "HTML"
        MARKDOWN_V2 = "MARKDOWN_V2"

    setattr(telegram_mod, "Bot", Bot)
    setattr(telegram_constants_mod, "ParseMode", ParseMode)
    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.constants", telegram_constants_mod)


def _install_fake_aiohttp(monkeypatch, capture, *, get_payload=None):
    aiohttp_mod = ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class FormData:
        def __init__(self):
            self.fields = []

        def add_field(self, name, value, **kwargs):
            self.fields.append((name, value, kwargs))

    class _Response:
        def __init__(self, payload=None, status=200):
            self.status = status
            self._payload = payload or {"id": f"msg-{len(capture)}", "ok": True, "ts": f"ts-{len(capture)}"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return self._payload

        async def text(self):
            return json.dumps(self._payload)

    class ClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def post(self, url, headers=None, json=None, data=None, **kwargs):
            capture.append({
                "method": "post",
                "url": url,
                "headers": headers,
                "json": json,
                "data": data,
                "kwargs": kwargs,
            })
            if url.endswith("/chat.postMessage"):
                payload = {"ok": True, "ts": f"ts-{len(capture)}"}
            else:
                payload = {"id": f"msg-{len(capture)}"}
            return _Response(payload=payload)

        def get(self, url, headers=None, **kwargs):
            capture.append({
                "method": "get",
                "url": url,
                "headers": headers,
                "json": None,
                "data": None,
                "kwargs": kwargs,
            })
            payload = get_payload if get_payload is not None else {}
            return _Response(payload=payload)

    setattr(aiohttp_mod, "ClientTimeout", ClientTimeout)
    setattr(aiohttp_mod, "ClientSession", ClientSession)
    setattr(aiohttp_mod, "FormData", FormData)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp_mod)


def test_send_to_platform_splits_long_telegram_messages(monkeypatch):
    _install_fake_telegram_modules(monkeypatch)
    sent_texts = []

    async def _fake_send(bot, **kwargs):
        sent_texts.append(kwargs["text"])
        return SimpleNamespace(message_id=len(sent_texts))

    monkeypatch.setattr("tools.send_message_tool._send_telegram_message_with_retry", _fake_send)

    result = asyncio.run(
        _send_to_platform(
            Platform.TELEGRAM,
            SimpleNamespace(enabled=True, token="token", extra={}),
            "-100123",
            "x" * 5000,
        )
    )

    assert result["success"] is True
    assert len(sent_texts) >= 2
    assert all(text for text in sent_texts)


def test_send_to_platform_routes_wav_to_telegram_document(monkeypatch, tmp_path):
    sent_audio = []
    sent_documents = []

    telegram_mod = ModuleType("telegram")
    telegram_constants_mod = ModuleType("telegram.constants")

    class Bot:
        def __init__(self, token):
            self.token = token

        async def send_document(self, **kwargs):
            sent_documents.append(kwargs)
            return SimpleNamespace(message_id=1)

        async def send_audio(self, **kwargs):
            sent_audio.append(kwargs)
            return SimpleNamespace(message_id=2)

    class ParseMode:
        HTML = "HTML"
        MARKDOWN_V2 = "MARKDOWN_V2"

    setattr(telegram_mod, "Bot", Bot)
    setattr(telegram_constants_mod, "ParseMode", ParseMode)
    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.constants", telegram_constants_mod)

    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(b"RIFF" + b"\x00" * 32)

    result = asyncio.run(
        _send_to_platform(
            Platform.TELEGRAM,
            SimpleNamespace(enabled=True, token="token", extra={}),
            "-100123",
            "",
            media_files=[(str(wav_path), False)],
        )
    )

    assert result["success"] is True
    assert sent_audio == []
    assert len(sent_documents) == 1


def test_send_to_platform_splits_long_discord_messages(monkeypatch):
    capture = []
    _install_fake_aiohttp(monkeypatch, capture)
    monkeypatch.setattr("gateway.channel_directory.lookup_channel_type", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        _send_to_platform(
            Platform.DISCORD,
            SimpleNamespace(enabled=True, token="token"),
            "123456",
            "x" * 2500,
        )
    )

    json_posts = [item["json"] for item in capture if item["json"] is not None]
    assert result["success"] is True
    assert len(json_posts) >= 2
    assert all(len(post["content"]) <= 2000 for post in json_posts)


def test_send_to_platform_probes_unknown_discord_channel_type_for_forum(monkeypatch):
    capture = []
    _install_fake_aiohttp(monkeypatch, capture, get_payload={"type": 15})
    monkeypatch.setattr("gateway.channel_directory.lookup_channel_type", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        _send_to_platform(
            Platform.DISCORD,
            SimpleNamespace(enabled=True, token="token"),
            "123456",
            "Hello forum!",
        )
    )

    assert result["success"] is True
    assert capture[0]["method"] == "get"
    assert capture[0]["url"] == "https://discord.com/api/v10/channels/123456"
    assert capture[1]["method"] == "post"
    assert capture[1]["url"] == "https://discord.com/api/v10/channels/123456/threads"


def test_send_to_platform_splits_long_slack_messages(monkeypatch):
    capture = []
    _install_fake_aiohttp(monkeypatch, capture)

    result = asyncio.run(
        _send_to_platform(
            Platform.SLACK,
            SimpleNamespace(enabled=True, token="token"),
            "C12345678",
            "x" * 50000,
            thread_id="thread-1",
        )
    )

    json_posts = [item["json"] for item in capture if item["json"] is not None]
    assert result["success"] is True
    assert len(json_posts) >= 2
    assert all(post["thread_ts"] == "thread-1" for post in json_posts)


def test_send_to_platform_rejects_unsupported_outbound_targets():
    for platform in (Platform.WEBHOOK, Platform.API_SERVER):
        result = asyncio.run(
            _send_to_platform(
                platform,
                SimpleNamespace(enabled=True),
                "target",
                "hello",
            )
        )
        assert result == {
            "error": f"Platform {platform.value} is not supported for send_message outbound delivery"
        }


def test_send_message_tool_rejects_unsupported_outbound_targets_before_live_adapter():
    for platform in (Platform.WEBHOOK, Platform.API_SERVER):
        runner = SimpleNamespace(adapters={platform: AsyncMock()})
        config = SimpleNamespace(
            platforms={platform: SimpleNamespace(enabled=True, home_channel=SimpleNamespace(chat_id="target"))}
        )

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("gateway.run._gateway_runner_ref", return_value=runner):
            result = json.loads(send_message_tool({"target": f"{platform.value}:target", "message": "hello"}))

        assert result == {
            "error": f"Platform {platform.value} is not supported for send_message outbound delivery"
        }

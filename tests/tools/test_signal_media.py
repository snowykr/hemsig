"""Tests for Signal media delivery in send_message_tool.py."""

import asyncio
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from gateway.config import Platform
from tools.send_message_tool import _send_to_platform


def _make_httpx_mock():
    """Create a mock httpx module with proper sync json()."""

    class AsyncBaseTransport:
        pass

    class Proxy:
        pass

    class MockResp:
        status_code = 200
        def json(self):
            return {"timestamp": 1234567890}
        def raise_for_status(self):
            pass

    class MockClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def post(self, *args, **kwargs):
            return MockResp()

    httpx_mock = ModuleType("httpx")
    setattr(httpx_mock, "AsyncClient", lambda timeout=None: MockClient())
    setattr(httpx_mock, "AsyncBaseTransport", AsyncBaseTransport)  # Needed by Telegram adapter
    setattr(httpx_mock, "Proxy", Proxy)  # Needed by telegram-bot library
    return httpx_mock


@pytest.fixture(autouse=True)
def inject_httpx(monkeypatch):
    """Inject mock httpx into sys.modules before imports."""
    monkeypatch.setitem(sys.modules, "httpx", _make_httpx_mock())


class TestSendSignalMediaFiles:
    """Test that _send_signal correctly handles media_files parameter."""

    def test_send_signal_basic_text_without_media(self):
        """Backward compatibility: text-only signal messages work."""
        from tools.send_message_tool import _send_signal

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}

        result = asyncio.run(_send_signal(extra, "+155****9999", "Hello world"))

        assert result["success"] is True
        assert result["platform"] == "signal"
        assert result["chat_id"] == "+155****9999"

    def test_send_signal_with_attachments(self, tmp_path):
        """Signal messages with media_files include attachments in JSON-RPC."""
        from tools.send_message_tool import _send_signal

        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG")

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}

        result = asyncio.run(
            _send_signal(extra, "+155****9999", "Check this out", media_files=[(str(img_path), False)])
        )

        assert result["success"] is True
        assert result["platform"] == "signal"

    def test_send_signal_with_missing_media_file(self):
        """Missing media files should generate warnings but not fail."""
        from tools.send_message_tool import _send_signal

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}

        result = asyncio.run(
            _send_signal(extra, "+155****9999", "File missing?", media_files=[("/nonexistent.png", False)])
        )

        assert result["success"] is True  # Should succeed despite missing file
        assert "warnings" in result
        assert "Some media files were skipped" in str(result["warnings"])

    def test_send_signal_uses_json_rpc_endpoint_and_payload(self):
        from tools.send_message_tool import _send_signal

        captured = {}

        class MockResp:
            def json(self):
                return {"result": {"timestamp": 987654321}}

            def raise_for_status(self):
                pass

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json):
                captured["url"] = url
                captured["json"] = json
                return MockResp()

        httpx_mock = ModuleType("httpx")
        setattr(httpx_mock, "AsyncClient", lambda timeout=None: MockClient())
        setattr(httpx_mock, "AsyncBaseTransport", type("AsyncBaseTransport", (), {}))
        setattr(httpx_mock, "Proxy", type("Proxy", (), {}))
        sys.modules["httpx"] = httpx_mock

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}
        result = asyncio.run(_send_signal(extra, "+155****9999", "Hello world"))

        assert result == {
            "success": True,
            "platform": "signal",
            "chat_id": "+155****9999",
            "message_id": 987654321,
        }
        assert captured["url"] == "http://localhost:8080/api/v1/rpc"
        assert captured["json"] == {
            "jsonrpc": "2.0",
            "method": "send",
            "params": {
                "account": "+155****4567",
                "message": "Hello world",
                "recipient": ["+155****9999"],
            },
            "id": "send_message_tool_signal",
        }

    def test_send_signal_returns_error_for_json_rpc_error(self):
        from tools.send_message_tool import _send_signal

        class MockResp:
            def json(self):
                return {"error": {"message": "rate limited"}}

            def raise_for_status(self):
                pass

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, json):
                return MockResp()

        httpx_mock = ModuleType("httpx")
        setattr(httpx_mock, "AsyncClient", lambda timeout=None: MockClient())
        setattr(httpx_mock, "AsyncBaseTransport", type("AsyncBaseTransport", (), {}))
        setattr(httpx_mock, "Proxy", type("Proxy", (), {}))
        sys.modules["httpx"] = httpx_mock

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}
        result = asyncio.run(_send_signal(extra, "+155****9999", "Hello world"))

        assert result == {"error": "Signal RPC error: {'message': 'rate limited'}"}


class TestSendSignalMediaRestrictions:
    """Test that the restriction block handles Signal media correctly."""

    def test_signal_allows_text_only_media_via_send_to_platform(self):
        """Signal should accept text-only media files (no message) via _send_to_platform."""
        import httpx
        if not hasattr(httpx, 'Proxy') or not hasattr(httpx, 'URL'):
            pytest.skip("httpx type annotations incompatible with telegram library")
        from tools.send_message_tool import _send_to_platform

        mock_result = {"success": True, "platform": "signal"}
        with patch("tools.send_message_tool._send_signal", new=AsyncMock(return_value=mock_result)):
            config = MagicMock()
            config.platforms = {Platform.SIGNAL: MagicMock(enabled=True)}
            config.get_home_channel.return_value = None

            result = asyncio.run(
                _send_to_platform(
                    Platform.SIGNAL,
                    config,
                    "+155****9999",
                    "",  # Empty message - media is the message
                    media_files=[("/tmp/test.png", False)]
                )
            )

            assert result["success"] is True

    def test_non_media_platforms_reject_text_only_media(self):
        """Slack should reject text-only media (no MESSAGE content)."""
        import httpx
        if not hasattr(httpx, 'Proxy') or not hasattr(httpx, 'URL'):
            pytest.skip("httpx type annotations incompatible with telegram library")
        from tools.send_message_tool import _send_to_platform

        config = MagicMock()
        config.platforms = {Platform.SLACK: MagicMock(enabled=True)}
        config.get_home_channel.return_value = None

        # Empty message with media_files should trigger restriction block
        result = asyncio.run(
            _send_to_platform(
                Platform.SLACK,
                config,
                "C012AB3CD",
                "",  # Empty message - media is the only content
                media_files=[("/tmp/test.png", False)]
            )
        )

        assert "error" in result
        error = result["error"]
        assert isinstance(error, str)
        assert "only supported for" in error


class TestSendSignalMediaWarningMessages:
    """Test warning messages are updated to include signal."""

    def test_warning_includes_signal_when_media_omitted(self):
        """Non-media platforms should show a warning mentioning signal in the supported list."""
        import httpx
        if not hasattr(httpx, 'Proxy') or not hasattr(httpx, 'URL'):
            pytest.skip("httpx type annotations incompatible with telegram library")
        from tools.send_message_tool import _send_to_platform

        config = MagicMock()
        config.platforms = {Platform.SLACK: MagicMock(enabled=True)}
        config.get_home_channel.return_value = None

        # Mock _send_slack so it succeeds -> then warning gets attached to result
        with patch("tools.send_message_tool._send_slack", new=AsyncMock(return_value={"success": True})):
            result = asyncio.run(
                _send_to_platform(
                    Platform.SLACK,
                    config,
                    "C012AB3CD",
                    "Test message with media",
                    media_files=[("/tmp/test.png", False)]
                )
            )

        assert result.get("warnings") is not None
        # Check that the warning mentions signal as supported
        warnings = result["warnings"]
        assert isinstance(warnings, list)
        found = any("signal" in str(w).lower() for w in warnings)
        assert found, f"Expected 'signal' in warnings but got: {result.get('warnings')}"


class TestSendSignalGroupChats:
    """Test that _send_signal handles group chats correctly."""

    def test_send_signal_group_with_attachments(self, tmp_path):
        """Group chat messages with attachments should use groupId parameter."""
        from tools.send_message_tool import _send_signal

        img_path = tmp_path / "test_attachment.pdf"
        img_path.write_bytes(b"%PDF-1.4")

        extra = {"http_url": "http://localhost:8080", "account": "+155****4567"}

        result = asyncio.run(
            _send_signal(extra, "group:abc123==", "Group file", media_files=[(str(img_path), False)])
        )

        assert result["success"] is True


class TestSendSignalConfigLoading:
    """Verify Signal config loading works."""

    def test_signal_platform_exists(self):
        """Platform.SIGNAL should be a valid platform."""
        assert hasattr(Platform, "SIGNAL")
        assert Platform.SIGNAL.value == "signal"


class TestSendSignalStandaloneAdapterRouting:
    def test_send_to_platform_routes_signal_media_through_adapter(self, tmp_path, monkeypatch):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG")
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4")
        voice_path = tmp_path / "voice.ogg"
        voice_path.write_bytes(b"OggS")

        calls = []

        class FakeSignalAdapter:
            def __init__(self, _config):
                pass

            async def send(self, chat_id, content, metadata=None):
                calls.append(("send", chat_id, content, metadata))
                return MagicMock(success=True, message_id="msg-1")

            async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
                calls.append(("send_multiple_images", chat_id, images, metadata, human_delay))

            async def send_document(self, chat_id, file_path, metadata=None):
                calls.append(("send_document", chat_id, file_path, metadata))
                return MagicMock(success=True, message_id="doc-1")

            async def send_voice(self, chat_id, audio_path, metadata=None):
                calls.append(("send_voice", chat_id, audio_path, metadata))
                return MagicMock(success=True, message_id="voice-1")

        monkeypatch.setattr("gateway.platforms.signal.SignalAdapter", FakeSignalAdapter)

        result = asyncio.run(
            _send_to_platform(
                Platform.SIGNAL,
                MagicMock(enabled=True, extra={}),
                "+155****9999",
                "Hello world",
                media_files=[
                    (str(img_path), False),
                    (str(pdf_path), False),
                    (str(voice_path), True),
                ],
            )
        )

        assert result == {
            "success": True,
            "platform": "signal",
            "chat_id": "+155****9999",
            "message_id": "voice-1",
        }
        assert calls[0] == ("send", "+155****9999", "Hello world", None)
        assert calls[1][0] == "send_multiple_images"
        assert calls[1][1] == "+155****9999"
        assert calls[1][2] == [(img_path.as_uri(), "")]
        assert calls[2] == ("send_document", "+155****9999", str(pdf_path), None)
        assert calls[3] == ("send_voice", "+155****9999", str(voice_path), None)

    def test_send_to_platform_skips_missing_signal_media_with_warning(self, tmp_path, monkeypatch):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG")
        missing_path = tmp_path / "missing.png"

        calls = []

        class FakeSignalAdapter:
            def __init__(self, _config):
                pass

            async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
                calls.append(("send_multiple_images", chat_id, images, metadata, human_delay))

        monkeypatch.setattr("gateway.platforms.signal.SignalAdapter", FakeSignalAdapter)

        result = asyncio.run(
            _send_to_platform(
                Platform.SIGNAL,
                MagicMock(enabled=True, extra={}),
                "+155****9999",
                "",
                media_files=[(str(missing_path), False), (str(img_path), False)],
            )
        )

        assert result["success"] is True
        assert result["chat_id"] == "+155****9999"
        assert result["warnings"] == [f"Media file not found, skipping: {missing_path}"]
        assert calls[0][0] == "send_multiple_images"
        assert calls[0][2] == [(img_path.as_uri(), "")]

    def test_send_to_platform_resolves_relative_signal_image_path(self, tmp_path, monkeypatch):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG")

        calls = []

        class FakeSignalAdapter:
            def __init__(self, _config):
                pass

            async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
                calls.append(("send_multiple_images", chat_id, images, metadata, human_delay))

        monkeypatch.setattr("gateway.platforms.signal.SignalAdapter", FakeSignalAdapter)
        monkeypatch.chdir(tmp_path)

        result = asyncio.run(
            _send_to_platform(
                Platform.SIGNAL,
                MagicMock(enabled=True, extra={}),
                "+155****9999",
                "",
                media_files=[("test.png", False)],
            )
        )

        assert result["success"] is True
        assert calls[0][2] == [(img_path.resolve().as_uri(), "")]

    def test_send_to_platform_propagates_signal_image_batch_failure(self, tmp_path, monkeypatch):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG")

        class FakeSignalAdapter:
            def __init__(self, _config):
                pass

            async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
                return MagicMock(success=False, error="Signal image batches were not delivered")

        monkeypatch.setattr("gateway.platforms.signal.SignalAdapter", FakeSignalAdapter)

        result = asyncio.run(
            _send_to_platform(
                Platform.SIGNAL,
                MagicMock(enabled=True, extra={}),
                "+155****9999",
                "",
                media_files=[(str(img_path), False)],
            )
        )

        assert result == {"error": "Signal image batches were not delivered"}

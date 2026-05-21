"""Send Message Tool -- cross-channel messaging via connected gateway adapters."""

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import ssl
from typing import cast

from agent.redact import redact_sensitive_text
from tools.registry import registry

logger = logging.getLogger(__name__)

_TELEGRAM_TOPIC_TARGET_RE = re.compile(r"^\s*(-?\d+)(?::(\d+))?\s*$")
_SLACK_TARGET_RE = re.compile(r"^\s*([CGD][A-Z0-9]{8,})\s*$")
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")
_PHONE_PLATFORMS = frozenset({"signal"})
_MEDIA_NATIVE_PLATFORMS = frozenset({"telegram", "discord", "signal"})
_MEDIA_TEXT_ONLY_PLATFORMS = _MEDIA_NATIVE_PLATFORMS
_UNSUPPORTED_SEND_MESSAGE_OUTBOUND_PLATFORMS = frozenset({"webhook", "api_server"})
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a"}
_VOICE_EXTS = {".ogg", ".opus"}
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)

SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to a connected messaging platform, or list available targets. "
        "Supported built-in targets are telegram, discord, slack, signal, email, "
        "and homeassistant."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "list"]},
            "target": {
                "type": "string",
                "description": (
                    "Format: platform, platform:chat_id, or platform:chat_id:thread_id. "
                    "Examples: telegram, telegram:-100123:17585, discord:999888, "
                    "slack:#engineering, signal:+15551234567, email:person@example.com."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Message text to send. To send an image or file, include MEDIA:<local_path> "
                    "in the message and Hermes will deliver it as a native attachment when "
                    "the target platform supports it."
                ),
            },
        },
        "required": [],
    },
}


def _error(message: str) -> dict[str, object]:
    return {"error": _sanitize_error_text(message)}


def _sanitize_error_text(text) -> str:
    redacted = redact_sensitive_text(str(text), force=True)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _discord_allowed_mentions_payload() -> dict[str, object]:
    def _flag(name: str, default: bool) -> bool:
        raw = os.getenv(name, "").strip().lower()
        if not raw:
            return default
        return raw in ("true", "1", "yes", "on")

    parse = []
    if _flag("DISCORD_ALLOW_MENTION_EVERYONE", False):
        parse.append("everyone")
    if _flag("DISCORD_ALLOW_MENTION_ROLES", False):
        parse.append("roles")
    if _flag("DISCORD_ALLOW_MENTION_USERS", True):
        parse.append("users")
    return {
        "parse": parse,
        "replied_user": _flag("DISCORD_ALLOW_MENTION_REPLIED_USER", True),
    }


def tool_error(message: str) -> str:
    return json.dumps(_error(message))


def _unsupported_outbound_error(platform_name: str) -> dict[str, object]:
    return _error(
        f"Platform {platform_name} is not supported for send_message outbound delivery"
    )


def _chunk_message(content: str, max_length: int, *, len_fn=None) -> list[str]:
    from gateway.platforms.base import BasePlatformAdapter

    return BasePlatformAdapter.truncate_message(content, max_length, len_fn=len_fn)


def _image_file_uri(media_path: str) -> str:
    return Path(os.path.expanduser(media_path)).resolve().as_uri()


def _mirror_successful_send(
    platform_name: str,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
) -> bool:
    try:
        from gateway.mirror import mirror_to_session
        from gateway.session_context import get_session_env

        source_label = get_session_env("HERMES_SESSION_PLATFORM", "cli") or "cli"
        user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip() or None
        if user_id is not None:
            return bool(
                mirror_to_session(
                    platform_name,
                    chat_id,
                    message,
                    source_label=source_label,
                    thread_id=thread_id,
                    user_id=user_id,
                )
            )
        return bool(
            mirror_to_session(
                platform_name,
                chat_id,
                message,
                source_label=source_label,
                thread_id=thread_id,
            )
        )
    except Exception:
        return False


def _mirror_text_for_successful_send(
    cleaned_message: str,
    media_files: list[tuple[str, bool]],
) -> str:
    if cleaned_message.strip() or not media_files:
        return cleaned_message

    filenames = []
    for media_path, _is_voice in media_files:
        name = os.path.basename(str(media_path).rstrip(os.sep)) or "attachment"
        filenames.append(name)

    if len(filenames) == 1:
        return f"Sent attachment: {filenames[0]}"
    return f"Sent {len(filenames)} attachments: {', '.join(filenames)}"


def send_message_tool(args, **kw):
    action = args.get("action", "send")
    if action == "list":
        return _handle_list()
    return _handle_send(args)


def _handle_list():
    try:
        from gateway.channel_directory import format_directory_for_display
        return json.dumps({"targets": format_directory_for_display()})
    except Exception as exc:
        return json.dumps(_error(f"Failed to load channel directory: {exc}"))


def _parse_target_ref(platform_name: str, target_ref: str):
    if platform_name == "telegram":
        match = _TELEGRAM_TOPIC_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "discord":
        match = _TELEGRAM_TOPIC_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), match.group(2), True
    if platform_name == "slack":
        match = _SLACK_TARGET_RE.fullmatch(target_ref)
        if match:
            return match.group(1), None, True
    if platform_name in _PHONE_PLATFORMS:
        match = _E164_TARGET_RE.fullmatch(target_ref)
        if match:
            return target_ref.strip(), None, True
    if "@" in target_ref and platform_name == "email":
        return target_ref.strip(), None, True
    if target_ref.lstrip("-").isdigit():
        return target_ref, None, True
    return None, None, False


def _home_channel_for(platform, pconfig):
    if getattr(pconfig, "home_channel", None):
        return pconfig.home_channel.chat_id
    return None


def _get_cron_auto_delivery_target():
    """Return the cron scheduler's auto-delivery target for the current run, if any."""
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _get_cron_auto_delivery_targets() -> list[dict[str, object]]:
    """Return all cron auto-delivery targets for the current run, if any."""
    from gateway.session_context import get_session_env

    raw_targets = get_session_env("HERMES_CRON_AUTO_DELIVER_TARGETS", "").strip()
    if raw_targets:
        try:
            decoded = json.loads(raw_targets)
        except Exception:
            decoded = None
        if isinstance(decoded, list):
            targets: list[dict[str, object]] = []
            for item in decoded:
                if not isinstance(item, dict):
                    continue
                platform = str(item.get("platform") or "").strip().lower()
                chat_id = str(item.get("chat_id") or "").strip()
                if not platform or not chat_id:
                    continue
                thread_id_value = item.get("thread_id")
                thread_id = None if thread_id_value is None or str(thread_id_value).strip() == "" else str(thread_id_value)
                targets.append({
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                })
            if targets:
                return targets

    single_target = _get_cron_auto_delivery_target()
    return [single_target] if single_target else []


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    """Skip redundant cron send_message calls when the scheduler will auto-deliver there."""
    auto_targets = _get_cron_auto_delivery_targets()
    if not auto_targets:
        return None

    same_target = any(
        target["platform"] == platform_name
        and str(target["chat_id"]) == str(chat_id)
        and target.get("thread_id") == thread_id
        for target in auto_targets
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"

    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already auto-deliver "
            "its final response to that same target. Put the intended user-facing content in "
            "your final response instead, or use a different target if you want an additional message."
        ),
    }


def _platform_config(platform, config_or_pconfig):
    platforms = getattr(config_or_pconfig, "platforms", None)
    if isinstance(platforms, dict):
        return platforms.get(platform)
    return config_or_pconfig


def _send_result_dict(platform, chat_id, result):
    if result is None:
        return {"success": True, "platform": platform.value, "chat_id": chat_id}
    if isinstance(result, dict):
        return result
    if getattr(result, "success", False):
        data = {"success": True, "platform": platform.value, "chat_id": chat_id}
        message_id = getattr(result, "message_id", None)
        if message_id is not None:
            data["message_id"] = message_id
        return data
    return _error(getattr(result, "error", None) or "send failed")


async def _send_with_connected_adapter_instance(adapter, platform, chat_id, message, *, thread_id=None):
    connected = False
    try:
        connected = await adapter.connect()
        if not connected:
            return _error(f"{platform.value} connect failed")
        metadata = {"thread_id": thread_id} if thread_id else None
        result = await adapter.send(chat_id=chat_id, content=message, metadata=metadata)
        return _send_result_dict(platform, chat_id, result)
    except Exception as exc:
        return _error(f"{platform.value} send failed: {exc}")
    finally:
        if connected:
            try:
                await adapter.disconnect()
            except Exception:
                pass


async def _send_with_adapter(adapter_cls, pconfig, platform, chat_id, message, *, thread_id=None):
    adapter = adapter_cls(pconfig)
    metadata = {"thread_id": thread_id} if thread_id else None
    result = await adapter.send(chat_id=chat_id, content=message, metadata=metadata)
    return _send_result_dict(platform, chat_id, result)


def _telegram_retry_delay(exc: Exception, attempt: int) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.0)
        except (TypeError, ValueError):
            return 1.0

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return None
    if (
        "bad gateway" in text
        or "502" in text
        or "too many requests" in text
        or "429" in text
        or "service unavailable" in text
        or "503" in text
        or "gateway timeout" in text
        or "504" in text
    ):
        return float(2 ** attempt)
    return None


async def _send_telegram_message_with_retry(bot, *, attempts: int = 3, **kwargs):
    for attempt in range(attempts):
        try:
            return await bot.send_message(**kwargs)
        except Exception as exc:
            delay = _telegram_retry_delay(exc, attempt)
            if delay is None or attempt >= attempts - 1:
                raise
            logger.warning(
                "Transient Telegram send failure (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                _sanitize_error_text(exc),
            )
            await asyncio.sleep(delay)


async def _send_with_connected_adapter(adapter_cls, pconfig, platform, chat_id, message, *, thread_id=None):
    adapter = adapter_cls(pconfig)
    return await _send_with_connected_adapter_instance(
        adapter,
        platform,
        chat_id,
        message,
        thread_id=thread_id,
    )


async def _send_telegram(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    try:
        Bot = __import__("telegram", fromlist=["Bot"]).Bot
        ParseMode = __import__("telegram.constants", fromlist=["ParseMode"]).ParseMode
        from gateway.platforms.base import should_send_media_as_audio, utf16_len
        from gateway.platforms.telegram import (
            TelegramAdapter,
            _escape_chunk_indicators_for_markdown_v2,
            _strip_mdv2,
        )

        token = getattr(pconfig, "token", None) or ""
        if not token:
            return _error("Telegram bot token is not configured")

        _has_html = bool(re.search(r'<[a-zA-Z/][^>]*>', message))
        if _has_html:
            formatted = message
            send_parse_mode = ParseMode.HTML
        else:
            _adapter = TelegramAdapter.__new__(TelegramAdapter)
            formatted = _adapter.format_message(message)
            send_parse_mode = ParseMode.MARKDOWN_V2

        text_chunks = []
        if formatted.strip():
            text_chunks = _chunk_message(
                formatted,
                TelegramAdapter.MAX_MESSAGE_LENGTH,
                len_fn=utf16_len,
            )
            if send_parse_mode == ParseMode.MARKDOWN_V2 and len(text_chunks) > 1:
                text_chunks = _escape_chunk_indicators_for_markdown_v2(
                    text_chunks,
                    TelegramAdapter.MAX_MESSAGE_LENGTH,
                )

        bot = Bot(token=token)
        int_chat_id = int(chat_id)
        media_files = media_files or []
        thread_kwargs = {}
        if thread_id is not None:
            thread_kwargs["message_thread_id"] = int(thread_id)
        extra = getattr(pconfig, "extra", {}) or {}
        if extra.get("disable_link_previews"):
            thread_kwargs["disable_web_page_preview"] = True

        last_msg = None
        warnings = []

        for chunk in text_chunks:
            try:
                last_msg = await _send_telegram_message_with_retry(
                    bot,
                    chat_id=int_chat_id,
                    text=chunk,
                    parse_mode=send_parse_mode,
                    **thread_kwargs,
                )
            except Exception as md_error:
                if "parse" in str(md_error).lower() or "markdown" in str(md_error).lower() or "html" in str(md_error).lower():
                    plain = message if _has_html else _strip_mdv2(chunk)
                    last_msg = await _send_telegram_message_with_retry(
                        bot,
                        chat_id=int_chat_id,
                        text=plain,
                        parse_mode=None,
                        **thread_kwargs,
                    )
                else:
                    raise

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                warning = f"Media file not found, skipping: {media_path}"
                logger.warning(warning)
                warnings.append(warning)
                continue
            ext = os.path.splitext(media_path)[1].lower()
            try:
                with open(media_path, "rb") as handle:
                    if ext in _IMAGE_EXTS:
                        last_msg = await bot.send_photo(chat_id=int_chat_id, photo=handle, **thread_kwargs)
                    elif ext in _VIDEO_EXTS:
                        last_msg = await bot.send_video(chat_id=int_chat_id, video=handle, **thread_kwargs)
                    elif ext in _VOICE_EXTS and is_voice:
                        last_msg = await bot.send_voice(chat_id=int_chat_id, voice=handle, **thread_kwargs)
                    elif should_send_media_as_audio("telegram", ext, is_voice=is_voice):
                        last_msg = await bot.send_audio(chat_id=int_chat_id, audio=handle, **thread_kwargs)
                    else:
                        last_msg = await bot.send_document(chat_id=int_chat_id, document=handle, **thread_kwargs)
            except Exception as exc:
                warnings.append(_sanitize_error_text(f"Failed to send media {media_path}: {exc}"))

        if last_msg is None:
            error = "No deliverable text or media remained after processing MEDIA tags"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": str(last_msg.message_id)}
        if warnings:
            result["warnings"] = warnings
        return result
    except ImportError:
        return _error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
    except Exception as exc:
        return _error(f"Telegram send failed: {exc}")


async def _send_discord(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    try:
        import aiohttp
    except ImportError:
        return _error("aiohttp not installed. Run: pip install aiohttp")

    try:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        token = getattr(pconfig, "token", None) or ""
        if not token:
            return _error("Discord bot token is not configured")

        _proxy = resolve_proxy_url(platform_env_var="DISCORD_PROXY")
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        auth_headers = {"Authorization": f"Bot {token}"}
        json_headers = {**auth_headers, "Content-Type": "application/json"}
        allowed_mentions = _discord_allowed_mentions_payload()
        media_files = media_files or []
        last_data = None
        warnings = []
        chunks = []
        if message.strip():
            chunks = _chunk_message(message, 2000)

        if thread_id:
            url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
        else:
            _channel_type = None
            try:
                from gateway.channel_directory import lookup_channel_type
                _channel_type = lookup_channel_type("discord", chat_id)
            except Exception:
                pass

            if _channel_type == "forum":
                is_forum = True
            elif _channel_type is not None:
                is_forum = False
            else:
                is_forum = False
                try:
                    info_url = f"https://discord.com/api/v10/channels/{chat_id}"
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), **_sess_kw) as info_sess:
                        async with info_sess.get(info_url, headers=json_headers, **_req_kw) as info_resp:
                            if info_resp.status == 200:
                                info = await info_resp.json()
                                is_forum = info.get("type") == 15
                except Exception:
                    logger.debug("Failed to probe Discord channel type for %s", chat_id, exc_info=True)

            if is_forum:
                thread_name = _derive_forum_thread_name(message)
                thread_url = f"https://discord.com/api/v10/channels/{chat_id}/threads"
                valid_media = []
                for media_path, _is_voice in media_files:
                    if not os.path.exists(media_path):
                        warnings.append(f"Media file not found, skipping: {media_path}")
                        continue
                    valid_media.append(media_path)

                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), **_sess_kw) as session:
                    if valid_media:
                        attachments_meta = [{"id": str(idx), "filename": os.path.basename(path)} for idx, path in enumerate(valid_media)]
                        starter_message = {
                            "content": chunks[0] if chunks else message,
                            "attachments": attachments_meta,
                            "allowed_mentions": allowed_mentions,
                        }
                        payload_json = json.dumps({"name": thread_name, "message": starter_message})
                        form = aiohttp.FormData()
                        form.add_field("payload_json", payload_json, content_type="application/json")
                        for idx, media_path in enumerate(valid_media):
                            with open(media_path, "rb") as handle:
                                form.add_field(f"files[{idx}]", handle.read(), filename=os.path.basename(media_path))
                        async with session.post(thread_url, headers=auth_headers, data=form, **_req_kw) as resp:
                            if resp.status not in (200, 201):
                                return _error(f"Discord forum thread creation error ({resp.status}): {await resp.text()}")
                            data = await resp.json()
                    else:
                        async with session.post(thread_url, headers=json_headers, json={"name": thread_name, "message": {"content": chunks[0] if chunks else message, "allowed_mentions": allowed_mentions}}, **_req_kw) as resp:
                            if resp.status not in (200, 201):
                                return _error(f"Discord forum thread creation error ({resp.status}): {await resp.text()}")
                            data = await resp.json()

                thread_id_created = data.get("id")
                starter_msg_id = (data.get("message") or {}).get("id", thread_id_created)
                result = {"success": True, "platform": "discord", "chat_id": chat_id, "thread_id": thread_id_created, "message_id": starter_msg_id}
                if len(chunks) > 1:
                    followup_url = f"https://discord.com/api/v10/channels/{thread_id_created}/messages"
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), **_sess_kw) as session:
                        for chunk in chunks[1:]:
                            async with session.post(followup_url, headers=json_headers, json={"content": chunk, "allowed_mentions": allowed_mentions}, **_req_kw) as resp:
                                if resp.status not in (200, 201):
                                    warnings.append(_sanitize_error_text(f"Failed to send forum follow-up chunk: Discord API error ({resp.status}): {await resp.text()}"))
                                    continue
                if warnings:
                    result["warnings"] = warnings
                return result

            url = f"https://discord.com/api/v10/channels/{chat_id}/messages"

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            if chunks or not media_files:
                text_chunks = chunks or [message]
                for chunk in text_chunks:
                    async with session.post(url, headers=json_headers, json={"content": chunk, "allowed_mentions": allowed_mentions}, **_req_kw) as resp:
                        if resp.status not in (200, 201):
                            return _error(f"Discord API error ({resp.status}): {await resp.text()}")
                        last_data = await resp.json()

            for media_path, _is_voice in media_files:
                if not os.path.exists(media_path):
                    warnings.append(f"Media file not found, skipping: {media_path}")
                    continue
                form = aiohttp.FormData()
                with open(media_path, "rb") as handle:
                    form.add_field("files[0]", handle, filename=os.path.basename(media_path))
                    async with session.post(url, headers=auth_headers, data=form, **_req_kw) as resp:
                        if resp.status not in (200, 201):
                            warnings.append(_sanitize_error_text(f"Failed to send media {media_path}: Discord API error ({resp.status}): {await resp.text()}"))
                            continue
                        last_data = await resp.json()

        if last_data is None:
            error = "No deliverable text or media remained after processing"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {"success": True, "platform": "discord", "chat_id": chat_id, "message_id": last_data.get("id")}
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as exc:
        return _error(f"Discord send failed: {exc}")


async def _send_slack(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    try:
        import aiohttp
    except ImportError:
        return _error("aiohttp not installed. Run: pip install aiohttp")

    try:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url
        from gateway.platforms.slack import SlackAdapter

        token = getattr(pconfig, "token", None) or ""
        if not token:
            return _error("Slack bot token is not configured")

        _proxy = resolve_proxy_url()
        _sess_kw, _req_kw = proxy_kwargs_for_aiohttp(_proxy)
        url = "https://slack.com/api/chat.postMessage"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        _adapter = SlackAdapter.__new__(SlackAdapter)
        formatted = _adapter.format_message(message)
        chunks = _chunk_message(formatted, SlackAdapter.MAX_MESSAGE_LENGTH)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **_sess_kw) as session:
            last_data = None
            for chunk in chunks:
                payload = {"channel": chat_id, "text": chunk, "mrkdwn": True}
                if thread_id:
                    payload["thread_ts"] = thread_id
                async with session.post(url, headers=headers, json=payload, **_req_kw) as resp:
                    data = await resp.json()
                    if not data.get("ok"):
                        return _error(f"Slack API error: {data.get('error', 'unknown')}")
                    last_data = data
            return {"success": True, "platform": "slack", "chat_id": chat_id, "message_id": last_data.get("ts") if last_data else None}
    except Exception as exc:
        return _error(f"Slack send failed: {exc}")


async def _send_email(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.utils import formatdate

    extra = pconfig if isinstance(pconfig, dict) else getattr(pconfig, "extra", {}) or {}
    address = extra.get("address") or getattr(pconfig, "token", None) or ""
    if not address:
        address = __import__("os").getenv("EMAIL_ADDRESS", "")
    password = __import__("os").getenv("EMAIL_PASSWORD", "")
    smtp_host = extra.get("smtp_host") or __import__("os").getenv("EMAIL_SMTP_HOST", "")
    try:
        smtp_port = int(__import__("os").getenv("EMAIL_SMTP_PORT", "587"))
    except (ValueError, TypeError):
        smtp_port = 587

    if not all([address, password, smtp_host]):
        return {"error": "Email not configured (EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_SMTP_HOST required)"}

    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = address
        msg["To"] = chat_id
        msg["Subject"] = "Hermes Agent"
        msg["Date"] = formatdate(localtime=True)

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls(context=ssl.create_default_context())
        server.login(address, password)
        server.send_message(msg)
        server.quit()
        return {"success": True, "platform": "email", "chat_id": chat_id}
    except Exception as exc:
        return _error(f"Email send failed: {exc}")


async def _send_homeassistant(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    from gateway.platforms.homeassistant import HomeAssistantAdapter
    return await _send_with_adapter(HomeAssistantAdapter, pconfig, __import__("gateway.config", fromlist=["Platform"]).Platform.HOMEASSISTANT, chat_id, message, thread_id=thread_id)


async def _send_webhook(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    from gateway.platforms.webhook import WebhookAdapter
    return await _send_with_adapter(WebhookAdapter, pconfig, __import__("gateway.config", fromlist=["Platform"]).Platform.WEBHOOK, chat_id, message, thread_id=thread_id)


async def _send_api_server(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    from gateway.platforms.api_server import APIServerAdapter
    return await _send_with_adapter(APIServerAdapter, pconfig, __import__("gateway.config", fromlist=["Platform"]).Platform.API_SERVER, chat_id, message, thread_id=thread_id)


async def _send_signal(extra, chat_id, message, media_files=None):
    """Standalone Signal send helper used by cron and regression tests."""
    import httpx

    media_files = media_files or []
    warnings = []
    attachments = []
    for item in media_files:
        path = item[0] if isinstance(item, (tuple, list)) else item
        if not path or not Path(str(path)).exists():
            warnings.append(f"Some media files were skipped because they do not exist: {path}")
            continue
        attachments.append(str(path))

    if not isinstance(extra, dict):
        extra = getattr(extra, "extra", {}) or {}
    http_url = (extra.get("http_url") or extra.get("url") or "").rstrip("/")
    account = extra.get("account") or extra.get("number") or ""
    if not http_url:
        return _error("Signal HTTP URL is not configured")
    if not account:
        return _error("Signal account is not configured")

    params = {"account": account, "message": message or ""}
    if str(chat_id).startswith("group:"):
        params["groupId"] = str(chat_id)[len("group:"):]
    else:
        params["recipient"] = [chat_id]
    if attachments:
        params["attachments"] = attachments

    payload = {
        "jsonrpc": "2.0",
        "method": "send",
        "params": params,
        "id": "send_message_tool_signal",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{http_url}/api/v1/rpc", json=payload)
        response.raise_for_status()
        body = response.json() if hasattr(response, "json") else {}

    if not isinstance(body, dict):
        return _error("Signal RPC returned an invalid response body")
    if body.get("error") is not None:
        return _error(f"Signal RPC error: {body.get('error')}")

    result = {"success": True, "platform": "signal", "chat_id": chat_id}
    rpc_result = body.get("result")
    if isinstance(rpc_result, dict) and rpc_result.get("timestamp") is not None:
        result["message_id"] = rpc_result.get("timestamp")
    elif body.get("timestamp") is not None:
        result["message_id"] = body.get("timestamp")
    elif rpc_result is None:
        return _error("Signal RPC returned no result")
    if warnings:
        result["warnings"] = warnings
    return result


async def _send_signal_via_adapter(pconfig, chat_id, message, *, media_files=None):
    from gateway.platforms.base import should_send_media_as_audio
    from gateway.platforms.signal import SignalAdapter
    import httpx

    adapter = SignalAdapter(pconfig)
    media_files = media_files or []
    warnings = []
    image_files = []
    attachment_files = []
    last_message_id = None
    adapter.client = httpx.AsyncClient(timeout=30.0)

    try:
        if (message or "").strip():
            text_result = await adapter.send(chat_id, message)
            if not getattr(text_result, "success", False):
                return _error(getattr(text_result, "error", None) or "signal send failed")
            last_message_id = getattr(text_result, "message_id", None)

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                warnings.append(f"Media file not found, skipping: {media_path}")
                continue
            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                image_files.append((_image_file_uri(media_path), ""))
            else:
                attachment_files.append((media_path, is_voice))

        if image_files:
            image_result = await adapter.send_multiple_images(chat_id=chat_id, images=image_files)
            if image_result is not None and not getattr(image_result, "success", False):
                result = _error(getattr(image_result, "error", None) or "Signal image batches were not delivered")
                if warnings:
                    result["warnings"] = warnings
                return result
            image_message_id = getattr(image_result, "message_id", None) if image_result is not None else None
            if image_message_id is not None:
                last_message_id = image_message_id

        for media_path, is_voice in attachment_files:
            ext = os.path.splitext(media_path)[1].lower()
            if should_send_media_as_audio("signal", ext, is_voice=is_voice):
                media_result = await adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=None)
            elif ext in _VIDEO_EXTS:
                media_result = await adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=None)
            else:
                media_result = await adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=None)

            if not getattr(media_result, "success", False):
                error = getattr(media_result, "error", None) or f"Failed to send media {media_path}"
                result = _error(error)
                if warnings:
                    result["warnings"] = warnings
                return result
            media_message_id = getattr(media_result, "message_id", None)
            if media_message_id is not None:
                last_message_id = media_message_id

        if not (message or "").strip() and not image_files and not attachment_files:
            error = "No deliverable text or media remained after processing MEDIA tags"
            if warnings:
                return {"error": error, "warnings": warnings}
            return {"error": error}

        result = {"success": True, "platform": "signal", "chat_id": chat_id}
        if last_message_id is not None:
            result["message_id"] = last_message_id
        if warnings:
            result["warnings"] = warnings
        return result
    except Exception as exc:
        return _error(f"signal send failed: {exc}")
    finally:
        client = getattr(adapter, "client", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
            adapter.client = None


async def _send_media_via_live_adapter(adapter, platform_name: str, chat_id: str, message: str, *, thread_id=None, media_files=None):
    from gateway.platforms.base import should_send_media_as_audio

    media_files = media_files or []
    warnings = []
    image_files = []
    attachment_files = []
    last_message_id = None
    metadata = {"thread_id": thread_id} if thread_id else None

    if (message or "").strip():
        text_result = await adapter.send(chat_id=chat_id, content=message, metadata=metadata)
        if not getattr(text_result, "success", False):
            return _error(getattr(text_result, "error", None) or f"{platform_name} send failed")
        last_message_id = getattr(text_result, "message_id", None)

    for media_path, is_voice in media_files:
        if not os.path.exists(media_path):
            warnings.append(f"Media file not found, skipping: {media_path}")
            continue
        ext = os.path.splitext(media_path)[1].lower()
        if ext in _IMAGE_EXTS:
            image_files.append((_image_file_uri(media_path), ""))
        else:
            attachment_files.append((media_path, is_voice))

    if image_files:
        image_result = await adapter.send_multiple_images(
            chat_id=chat_id,
            images=image_files,
            metadata=metadata,
        )
        if image_result is not None and not getattr(image_result, "success", False):
            result = _error(getattr(image_result, "error", None) or f"{platform_name} image batches were not delivered")
            if warnings:
                result["warnings"] = warnings
            return result
        image_message_id = getattr(image_result, "message_id", None) if image_result is not None else None
        if image_message_id is not None:
            last_message_id = image_message_id

    for media_path, is_voice in attachment_files:
        ext = os.path.splitext(media_path)[1].lower()
        if should_send_media_as_audio(platform_name, ext, is_voice=is_voice):
            media_result = await adapter.send_voice(chat_id=chat_id, audio_path=media_path, metadata=metadata)
        elif ext in _VIDEO_EXTS:
            media_result = await adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=metadata)
        else:
            media_result = await adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=metadata)

        if not getattr(media_result, "success", False):
            error = getattr(media_result, "error", None) or f"Failed to send media {media_path}"
            result = _error(error)
            if warnings:
                result["warnings"] = warnings
            return result
        media_message_id = getattr(media_result, "message_id", None)
        if media_message_id is not None:
            last_message_id = media_message_id

    if not (message or "").strip() and not image_files and not attachment_files:
        error = "No deliverable text or media remained after processing MEDIA tags"
        if warnings:
            return {"error": error, "warnings": warnings}
        return {"error": error}

    result: dict[str, object] = {"success": True, "platform": platform_name, "chat_id": chat_id}
    if last_message_id is not None:
        result["message_id"] = last_message_id
    if warnings:
        result["warnings"] = warnings
    return result


def _derive_forum_thread_name(message: str) -> str:
    """Derive a Discord forum thread title from message content."""
    first_line = (message or "").strip().split("\n", 1)[0].strip()
    first_line = first_line.lstrip("#").strip()
    if not first_line:
        first_line = "New Post"
    return first_line[:100]


async def _send_to_platform(platform, config_or_pconfig, chat_id, message, *, thread_id=None, media_files=None):
    """Send to a retained platform using the legacy async helper API."""
    media_files = media_files or []
    platform_name = platform.value
    if platform_name in _UNSUPPORTED_SEND_MESSAGE_OUTBOUND_PLATFORMS:
        return _unsupported_outbound_error(platform_name)
    pconfig = _platform_config(platform, config_or_pconfig)
    if not pconfig or not getattr(pconfig, "enabled", True):
        return _error(f"Platform {platform_name} is not configured or enabled")

    if media_files and not (message or "").strip() and platform_name not in _MEDIA_TEXT_ONLY_PLATFORMS:
        supported = ", ".join(sorted(_MEDIA_TEXT_ONLY_PLATFORMS))
        return _error(f"MEDIA-only delivery is only supported for {supported}")

    warning = None
    if media_files and platform_name not in _MEDIA_NATIVE_PLATFORMS:
        warning = "Media attachments are only supported for telegram, discord, and signal; files were omitted."

    try:
        from gateway.platform_registry import platform_registry

        plugin_adapter = platform_registry.create_adapter(platform_name, pconfig)
    except Exception:
        plugin_adapter = None

    if plugin_adapter is not None:
        result = await _send_with_connected_adapter_instance(
            plugin_adapter,
            platform,
            chat_id,
            message,
            thread_id=thread_id,
        )
        if warning and isinstance(result, dict) and result.get("success"):
            result_dict = cast(dict[str, object], result)
            warning_list = result_dict.get("warnings")
            if not isinstance(warning_list, list):
                warning_list = []
                result_dict["warnings"] = warning_list
            warning_list.append(warning)
        return result

    if platform_name == "signal":
        if media_files:
            result = await _send_signal_via_adapter(pconfig, chat_id, message, media_files=media_files)
        else:
            extra = getattr(pconfig, "extra", {}) or {}
            result = await _send_signal(extra, chat_id, message, media_files=[])
    elif platform_name == "telegram":
        result = await _send_telegram(pconfig, chat_id, message, thread_id=thread_id, media_files=media_files)
    elif platform_name == "discord":
        result = await _send_discord(pconfig, chat_id, message, thread_id=thread_id, media_files=media_files)
    elif platform_name == "slack":
        result = await _send_slack(pconfig, chat_id, message, thread_id=thread_id, media_files=[])
    elif platform_name == "email":
        result = await _send_email(pconfig, chat_id, message, thread_id=thread_id, media_files=[])
    elif platform_name == "homeassistant":
        result = await _send_homeassistant(pconfig, chat_id, message, thread_id=thread_id, media_files=[])
    elif platform_name == "webhook":
        result = await _send_webhook(pconfig, chat_id, message, thread_id=thread_id, media_files=[])
    elif platform_name == "api_server":
        result = await _send_api_server(pconfig, chat_id, message, thread_id=thread_id, media_files=[])
    else:
        return _error(f"Unsupported platform {platform_name}")

    if warning and isinstance(result, dict) and result.get("success"):
        result_dict = cast(dict[str, object], result)
        warning_list = result_dict.get("warnings")
        if not isinstance(warning_list, list):
            warning_list = []
            result_dict["warnings"] = warning_list
        warning_list.append(warning)
    return result


def _handle_send(args):
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    if platform_name in _UNSUPPORTED_SEND_MESSAGE_OUTBOUND_PLATFORMS:
        return tool_error(
            f"Platform {platform_name} is not supported for send_message outbound delivery"
        )
    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    thread_id = None
    is_explicit = False

    try:
        from gateway.config import Platform, load_gateway_config
        config = load_gateway_config()
        platform = Platform(platform_name)
    except Exception as exc:
        return json.dumps(_error(f"Failed to load gateway config: {exc}"))

    if target_ref:
        chat_id, thread_id, is_explicit = _parse_target_ref(platform_name, target_ref)

    if target_ref and not is_explicit:
        try:
            from gateway.platform_registry import platform_registry
            from gateway.channel_directory import resolve_channel_name

            resolved = resolve_channel_name(platform_name, target_ref)
            if resolved:
                chat_id, thread_id, _ = _parse_target_ref(platform_name, resolved)
            elif platform_registry.is_registered(platform_name):
                chat_id = target_ref
                thread_id = None
            else:
                return json.dumps(_error(f"Could not resolve '{target_ref}' on {platform_name}. Use send_message(action='list') first."))
        except Exception as exc:
            return json.dumps(_error(f"Could not resolve '{target_ref}' on {platform_name}: {exc}"))

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return tool_error(f"Platform {platform_name} is not configured or enabled")

    from gateway.platforms.base import BasePlatformAdapter

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    if not cleaned_message.strip() and not media_files:
        return tool_error("No deliverable text or media remained after processing MEDIA tags")
    mirror_message = _mirror_text_for_successful_send(cleaned_message, media_files)

    if not chat_id:
        chat_id = _home_channel_for(platform, pconfig)
    if not chat_id:
        return tool_error(f"No chat_id provided and no home channel configured for {platform_name}")

    cron_skip = _maybe_skip_cron_duplicate_send(platform_name, chat_id, thread_id)
    if cron_skip is not None:
        return json.dumps(cron_skip)

    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        adapter = runner.adapters.get(platform) if runner else None
        if adapter and not media_files:
            import asyncio
            async def _send():
                return await adapter.send(chat_id=chat_id, content=cleaned_message, metadata={"thread_id": thread_id} if thread_id else None)
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(_send())
            else:
                raise RuntimeError("send_message cannot synchronously send while an event loop is already running")
            if result.success:
                response: dict[str, object] = {"success": True, "platform": platform_name, "chat_id": chat_id, "message_id": result.message_id}
                if _mirror_successful_send(platform_name, chat_id, mirror_message, thread_id=thread_id):
                    response["mirrored"] = True
                return json.dumps(response)
            return json.dumps(_error(result.error or "send failed"))
        if adapter and media_files and platform_name == "signal":
            import asyncio

            async def _send_live_media():
                return await _send_media_via_live_adapter(
                    adapter,
                    platform_name,
                    chat_id,
                    cleaned_message,
                    thread_id=thread_id,
                    media_files=media_files,
                )

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                result = asyncio.run(_send_live_media())
            else:
                raise RuntimeError("send_message cannot synchronously send while an event loop is already running")
            if isinstance(result, dict) and result.get("success"):
                result_dict = cast(dict[str, object], result)
                if _mirror_successful_send(platform_name, chat_id, mirror_message, thread_id=thread_id):
                    result_dict["mirrored"] = True
            return json.dumps(result)
    except Exception as exc:
        return json.dumps(_error(f"Gateway adapter send failed: {exc}"))

    try:
        import asyncio

        async def _send_standalone():
            return await _send_to_platform(
                platform,
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
            )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(_send_standalone())
        else:
            raise RuntimeError("send_message cannot synchronously send while an event loop is already running")
        if isinstance(result, dict) and result.get("success"):
            result_dict = cast(dict[str, object], result)
            if _mirror_successful_send(platform_name, chat_id, mirror_message, thread_id=thread_id):
                result_dict["mirrored"] = True
        return json.dumps(result)
    except Exception as exc:
        return json.dumps(_error(f"Standalone send failed: {exc}"))


def _check_send_message():
    from gateway.session_context import get_session_env
    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if platform and platform != "local":
        return True
    try:
        from gateway.status import is_gateway_running
        return is_gateway_running()
    except Exception:
        return False


registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=lambda args, **kw: send_message_tool(args, **kw),
    check_fn=_check_send_message,
)

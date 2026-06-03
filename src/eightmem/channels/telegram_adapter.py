from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from eightmem.services.telegram_service import process_telegram_message


class TelegramConfigError(RuntimeError):
    pass


class TelegramSendError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    webhook_secret: str | None = None

    @property
    def api_base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"


@dataclass(frozen=True)
class TelegramIncomingMessage:
    update_id: int
    chat_id: str
    user_id: str
    text: str
    callback_query_id: str | None = None
    source_message_id: int | None = None


@dataclass(frozen=True)
class TelegramOutgoingMessage:
    chat_id: str
    text: str
    user_id: str
    update_id: int
    action: str
    memory_updated: bool = False
    reply_keyboard: list[list[str | dict[str, str]]] | None = None
    remove_keyboard: bool = False
    callback_query_id: str | None = None
    source_message_id: int | None = None
    photo_path: str | None = None


def load_telegram_config() -> TelegramConfig:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramConfigError("Missing TELEGRAM_BOT_TOKEN")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
    return TelegramConfig(bot_token=token, webhook_secret=secret)


def parse_telegram_update(payload: dict[str, Any]) -> TelegramIncomingMessage | None:
    callback_query = payload.get("callback_query")
    if isinstance(callback_query, dict):
        from_user = callback_query.get("from")
        message = callback_query.get("message")
        data = callback_query.get("data")
        callback_id = callback_query.get("id")
        update_id = payload.get("update_id")
        if (
            isinstance(from_user, dict)
            and isinstance(message, dict)
            and isinstance(data, str)
            and isinstance(callback_id, str)
            and isinstance(update_id, int)
        ):
            chat = message.get("chat")
            if isinstance(chat, dict) and chat.get("id") is not None and from_user.get("id") is not None:
                return TelegramIncomingMessage(
                    update_id=update_id,
                    chat_id=str(chat.get("id")),
                    user_id=str(from_user.get("id")),
                    text=data.strip(),
                    callback_query_id=callback_id,
                    source_message_id=message.get("message_id") if isinstance(message.get("message_id"), int) else None,
                )

    message = payload.get("message")
    if not isinstance(message, dict):
        return None

    chat = message.get("chat")
    from_user = message.get("from")
    text = message.get("text")
    update_id = payload.get("update_id")

    if not isinstance(chat, dict) or not isinstance(from_user, dict):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(update_id, int):
        return None

    chat_id = chat.get("id")
    user_id = from_user.get("id")
    if chat_id is None or user_id is None:
        return None

    return TelegramIncomingMessage(
        update_id=update_id,
        chat_id=str(chat_id),
        user_id=str(user_id),
        text=text.strip(),
    )


def process_telegram_update(payload: dict[str, Any], *, model: str | None = None) -> TelegramOutgoingMessage | None:
    incoming = parse_telegram_update(payload)
    if incoming is None:
        return None

    reply = process_telegram_message(user_id=incoming.user_id, text=incoming.text, model=model)
    return TelegramOutgoingMessage(
        chat_id=incoming.chat_id,
        text=reply.message,
        user_id=incoming.user_id,
        update_id=incoming.update_id,
        action=reply.action,
        memory_updated=reply.memory_updated,
        reply_keyboard=reply.reply_keyboard,
        remove_keyboard=reply.remove_keyboard,
        callback_query_id=incoming.callback_query_id,
        source_message_id=incoming.source_message_id,
        photo_path=str(reply.photo_path) if reply.photo_path else None,
    )


def build_send_message_request(config: TelegramConfig, outgoing: TelegramOutgoingMessage) -> tuple[str, bytes, dict[str, str]]:
    url = f"{config.api_base_url}/sendMessage"
    reply_keyboard = getattr(outgoing, "reply_keyboard", None)
    remove_keyboard = getattr(outgoing, "remove_keyboard", False)
    payload: dict[str, Any] = {
        "chat_id": outgoing.chat_id,
        "text": outgoing.text,
    }
    if reply_keyboard:
        payload["reply_markup"] = {"inline_keyboard": _build_inline_keyboard(reply_keyboard)}
    elif remove_keyboard:
        payload["reply_markup"] = {
            "remove_keyboard": True,
        }
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    return url, body, headers


def _post_telegram_api(
    config: TelegramConfig,
    method: str,
    payload: dict[str, Any],
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    url = f"{config.api_base_url}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError:
        return _post_telegram_api_via_curl(url, payload, timeout_seconds=timeout_seconds)


def _post_telegram_api_via_curl(url: str, payload: dict[str, Any], *, timeout_seconds: int = 15) -> dict[str, Any]:
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            str(timeout_seconds),
            "-X",
            "POST",
            url,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TelegramSendError(f"Telegram curl send failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TelegramSendError("Telegram returned non-JSON response") from exc


def normalize_telegram_webhook_url(forward_url: str) -> str:
    base = forward_url.strip().rstrip("/")
    if not base:
        raise TelegramConfigError("Missing TELEGRAM_FORWARD_URL")
    if not base.startswith(("https://", "http://")):
        raise TelegramConfigError("TELEGRAM_FORWARD_URL must start with https:// or http://")
    suffix = "/channels/telegram/webhook"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def set_telegram_webhook(
    config: TelegramConfig,
    *,
    forward_url: str,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    webhook_url = normalize_telegram_webhook_url(forward_url)
    payload: dict[str, Any] = {"url": webhook_url}
    if config.webhook_secret:
        payload["secret_token"] = config.webhook_secret
    return _post_telegram_api(config, "setWebhook", payload, timeout_seconds=timeout_seconds)


def get_telegram_webhook_info(config: TelegramConfig, *, timeout_seconds: int = 10) -> dict[str, Any]:
    return _post_telegram_api(config, "getWebhookInfo", {}, timeout_seconds=timeout_seconds)


def _post_telegram_photo_via_curl(
    config: TelegramConfig,
    *,
    chat_id: str,
    photo_path: str,
    caption: str,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    path = Path(photo_path)
    if not path.exists():
        raise TelegramSendError(f"Telegram photo file missing: {photo_path}")
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--max-time",
            str(timeout_seconds),
            "-X",
            "POST",
            f"{config.api_base_url}/sendPhoto",
            "-F",
            f"chat_id={chat_id}",
            "-F",
            f"caption={caption}",
            "-F",
            f"photo=@{photo_path}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise TelegramSendError(f"Telegram photo send failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TelegramSendError("Telegram returned non-JSON photo response") from exc


def verify_webhook_secret(config: TelegramConfig, provided_secret: str | None) -> bool:
    if not config.webhook_secret:
        return True
    return provided_secret == config.webhook_secret


def send_telegram_message(config: TelegramConfig, outgoing: TelegramOutgoingMessage, timeout_seconds: int = 15) -> dict[str, Any]:
    try:
        if outgoing.callback_query_id:
            _post_telegram_api(
                config,
                "answerCallbackQuery",
                {"callback_query_id": outgoing.callback_query_id},
                timeout_seconds=timeout_seconds,
            )
        payload = {
            "chat_id": outgoing.chat_id,
            "text": outgoing.text,
        }
        reply_keyboard = getattr(outgoing, "reply_keyboard", None)
        remove_keyboard = getattr(outgoing, "remove_keyboard", False)
        if reply_keyboard:
            payload["reply_markup"] = {"inline_keyboard": _build_inline_keyboard(reply_keyboard)}
        elif remove_keyboard:
            payload["reply_markup"] = {
                "remove_keyboard": True,
            }
        message_result = _post_telegram_api(
            config,
            "sendMessage",
            payload,
            timeout_seconds=timeout_seconds,
        )
        photo_path = getattr(outgoing, "photo_path", None)
        if not photo_path:
            return message_result
        try:
            photo_result = _post_telegram_photo_via_curl(
                config,
                chat_id=outgoing.chat_id,
                photo_path=photo_path,
                caption="8mem Compare share card",
                timeout_seconds=timeout_seconds,
            )
        except TelegramSendError as exc:
            return {"ok": message_result.get("ok", False), "result": message_result.get("result"), "photo_error": str(exc)}
        return {"ok": bool(message_result.get("ok")) and bool(photo_result.get("ok")), "result": message_result.get("result"), "photo": photo_result}
    except URLError as exc:
        raise TelegramSendError(f"Telegram send failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TelegramSendError("Telegram returned non-JSON response") from exc


def _build_inline_keyboard(reply_keyboard: list[list[str | dict[str, str]]]) -> list[list[dict[str, str]]]:
    keyboard: list[list[dict[str, str]]] = []
    for row in reply_keyboard:
        keyboard_row: list[dict[str, str]] = []
        for item in row:
            if isinstance(item, dict):
                text = item.get("text", "").strip()
                callback_data = item.get("callback_data", text).strip()
                if text and callback_data:
                    keyboard_row.append({"text": text, "callback_data": callback_data})
            else:
                keyboard_row.append({"text": item, "callback_data": item})
        if keyboard_row:
            keyboard.append(keyboard_row)
    return keyboard

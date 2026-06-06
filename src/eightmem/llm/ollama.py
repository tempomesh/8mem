from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:1.7b"


class OllamaError(RuntimeError):
    pass


@dataclass
class OllamaResult:
    model: str
    text: str
    text_source: str
    done: bool | None
    done_reason: str | None
    eval_count: int | None
    eval_duration: int | None
    raw: dict[str, Any]


def resolve_base_url(base_url: str | None = None) -> str:
    return (base_url or os.getenv("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def resolve_default_model(model: str | None = None) -> str:
    return model or os.getenv("DEFAULT_LLM_FALLBACK_MODEL") or DEFAULT_MODEL


def check_backend(
    *,
    base_url: str | None = None,
    timeout_seconds: int = 2,
) -> tuple[bool, str | None]:
    endpoint = f"{resolve_base_url(base_url)}/api/tags"
    try:
        req = Request(endpoint, headers={"Accept": "application/json"}, method="GET")
        with urlopen(req, timeout=timeout_seconds) as response:
            json.loads(response.read().decode("utf-8"))
        return True, None
    except ValueError as exc:
        return False, f"Invalid Ollama base URL: {exc}"
    except URLError as exc:
        return False, f"Ollama request failed: {exc}"
    except json.JSONDecodeError:
        return False, "Ollama returned non-JSON response"


def _extract_message_text(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def extract_text(payload: dict[str, Any]) -> tuple[str, str]:
    response = payload.get("response")
    if isinstance(response, str) and response.strip():
        return response.strip(), "response"

    message_text = _extract_message_text(payload.get("message"))
    if message_text:
        return message_text, "message.content"

    thinking = payload.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip(), "thinking"

    return "", "none"


def generate_text(
    *,
    model: str,
    prompt: str,
    base_url: str | None = None,
    timeout_seconds: int = 300,
    temperature: float = 0.2,
    num_predict: int = 256,
) -> OllamaResult:
    endpoint = f"{resolve_base_url(base_url)}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": num_predict,
        },
    }
    try:
        req = Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except ValueError as exc:
        raise OllamaError(f"Invalid Ollama base URL: {exc}") from exc
    except URLError as exc:
        raise OllamaError(f"Ollama request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OllamaError("Ollama returned non-JSON response") from exc

    text, source = extract_text(data)
    if not text:
        raise OllamaError("Ollama returned empty output (response/message/thinking all empty)")

    return OllamaResult(
        model=model,
        text=text,
        text_source=source,
        done=data.get("done"),
        done_reason=data.get("done_reason"),
        eval_count=data.get("eval_count"),
        eval_duration=data.get("eval_duration"),
        raw=data,
    )

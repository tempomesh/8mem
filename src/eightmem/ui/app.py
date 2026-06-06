from __future__ import annotations

import json
import logging
import os
import hashlib
import importlib.resources as importlib_resources
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eightmem.channels.telegram_adapter import (
    TelegramConfig,
    TelegramOutgoingMessage,
    TelegramConfigError,
    TelegramSendError,
    load_telegram_config,
    process_telegram_update,
    send_telegram_message,
    verify_webhook_secret,
)
from eightmem.core.env import load_project_env
from eightmem.core.paths import ensure_cache_dir
from eightmem.core.governance import (
    create_approval_request,
    get_approval_request,
    list_approval_requests,
    resolve_approval_request,
)
from eightmem.core.pipeline import analyze_chat_export
from eightmem.llm.ollama import (
    OllamaError,
    check_backend as check_ollama_backend,
    generate_text,
    resolve_base_url,
    resolve_default_model,
)
from eightmem.services.memory_service import (
    apply_memory_updates,
    apply_trusted_memory_change,
    build_base_context,
    build_engram_context,
    build_passport_summary,
    build_recent_changes_payload,
    commit_memory_proposal,
    create_memory_proposal,
    find_forget_candidates,
    export_context_text,
    forget_memory_entries,
    get_file_editor_payload,
    get_files_overview,
    get_memory_inbox_payload,
    get_mirror_payload,
    infer_memory_category,
    initialize_memory_runtime,
    load_dashboard_payload,
    normalize_correction_text,
    normalize_preference_text,
    propose_natural_memory_candidate,
    resolve_memory_inbox_item,
    resolve_public_memory_file_name,
    runtime_memory_id,
    save_file_content,
    undo_last_memory_change,
    write_canonical_memory,
)
from eightmem.services.compare_card import CompareCardContent, parse_passport_summary, render_compare_card, render_passport_card
from eightmem.services.telegram_service import build_telegram_approval_message
from eightmem.services.webhook_service import (
    deregister_connector,
    list_connectors,
    notify_connectors,
    public_connector,
    register_connector,
)

logger = logging.getLogger("eightmem.telegram")
TELEGRAM_RATE_LIMIT_WINDOW_SECONDS = 60.0
TELEGRAM_RATE_LIMIT_MAX_REQUESTS = 20
MEMORY_WRITE_RATE_LIMIT_WINDOW_SECONDS = 60.0
MEMORY_WRITE_RATE_LIMIT_MAX_REQUESTS = 120


def create_app() -> FastAPI:
    load_project_env()
    app = FastAPI(title="8mem Local UI")
    telegram_seen_updates: dict[str, float] = {}
    telegram_user_windows: dict[str, list[float]] = {}
    memory_write_windows: dict[str, list[float]] = {}
    ui_dir = Path(__file__).parent
    templates = Jinja2Templates(directory=str(ui_dir / "templates"))
    app.mount("/static", StaticFiles(directory=str(ui_dir / "static")), name="static")

    @app.get("/overview", response_class=HTMLResponse)
    def overview(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = build_base_context("overview", user_id=user_id)
        return templates.TemplateResponse(request, "overview.html", context)

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = build_base_context("chat", user_id=user_id)
        context.update(
            {
                "question": "",
                "answer": None,
                "error": None,
                "model": resolve_default_model(),
                "base_url": resolve_base_url(),
            }
        )
        return templates.TemplateResponse(request, "chat.html", context)

    @app.head("/chat")
    def chat_head() -> HTMLResponse:
        return HTMLResponse(status_code=200)

    def _memory_write_rate_limit_error(user_id: str, action: str) -> JSONResponse | None:
        now = time.time()
        window_start = now - MEMORY_WRITE_RATE_LIMIT_WINDOW_SECONDS
        key = f"{user_id}:{action}"
        recent = [stamp for stamp in memory_write_windows.get(key, []) if stamp >= window_start]
        if len(recent) >= MEMORY_WRITE_RATE_LIMIT_MAX_REQUESTS:
            memory_write_windows[key] = recent
            return JSONResponse(
                {
                    "ok": False,
                    "status": "rate_limited",
                    "error": "Too many memory write requests. Wait a minute and retry.",
                },
                status_code=429,
            )
        recent.append(now)
        memory_write_windows[key] = recent
        return None

    @app.post("/chat", response_class=HTMLResponse)
    def chat_submit(
        request: Request,
        question: str = Form(default=""),
        user_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        context = build_base_context("chat", user_id=user_id)
        question = question.strip()
        answer: str | None = None
        error: str | None = None
        model = resolve_default_model()
        base_url = resolve_base_url()
        if not question:
            error = "Enter a message to test 8mem memory."
        else:
            try:
                memory_context = export_context_text(user_id=user_id)
                prompt = (
                    "You are answering inside the local 8mem Test Chat. "
                    "Use the memory context if relevant. Keep the answer concise.\n\n"
                    f"{memory_context}\n\n"
                    f"User question: {question}"
                )
                result = generate_text(
                    model=model,
                    prompt=prompt,
                    base_url=base_url,
                    timeout_seconds=180,
                    num_predict=240,
                )
                answer = result.text
            except OllamaError as exc:
                error = str(exc)
        context.update(
            {
                "question": question,
                "answer": answer,
                "error": error,
                "model": model,
                "base_url": base_url,
            }
        )
        return templates.TemplateResponse(request, "chat.html", context)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "status": "healthy"})

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            mem = initialize_memory_runtime()
            files = sorted(path.name for path in mem.glob("*.md"))
        except Exception as exc:  # pragma: no cover - defensive readiness guard
            return JSONResponse(
                {"ok": False, "status": "not_ready", "error": str(exc)},
                status_code=503,
            )

        try:
            load_telegram_config()
            telegram_configured = True
        except TelegramConfigError:
            telegram_configured = False

        llm_backend_ok, llm_backend_error = check_ollama_backend()

        return JSONResponse(
            {
                "ok": True,
                "status": "ready",
                "memory_dir": str(mem),
                "memory_files": files,
                "telegram_configured": telegram_configured,
                "llm_backend_available": llm_backend_ok,
                "llm_backend_error": llm_backend_error,
                "llm_base_url": resolve_base_url(),
                "llm_default_model": resolve_default_model(),
            }
        )

    @app.get("/v1/context")
    def runtime_context(
        request: Request,
        user_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, user_id)
        if auth_error is not None:
            return auth_error

        issuer = os.getenv("EIGHTMEM_CONTEXT_ISSUER")
        if not issuer:
            host = request.headers.get("host") or "localhost:8787"
            scheme = request.url.scheme or "http"
            issuer = f"{scheme}://{host}"

        return JSONResponse(build_engram_context(user_id=resolved_user_id, issuer=issuer))

    @app.get("/.well-known/engram")
    def engram_discovery(request: Request) -> JSONResponse:
        issuer = os.getenv("EIGHTMEM_CONTEXT_ISSUER")
        if not issuer:
            host = request.headers.get("host") or "localhost:8787"
            scheme = request.url.scheme or "http"
            issuer = f"{scheme}://{host}"
        return JSONResponse(
            {
                "engram_version": "0.1",
                "issuer": issuer,
                "context_endpoint": f"{issuer}/v1/context",
                "correction_endpoint": f"{issuer}/v1/context/correct",
                "keys_endpoint": f"{issuer}/.well-known/engram-keys",
                "auth": {"type": "bearer"},
                "signature": {
                    "algorithm": "Ed25519",
                    "mode": "unsigned-v1",
                    "verification": "disabled",
                },
                "capabilities": {
                    "context": True,
                    "correction_alias": True,
                    "memory_write": True,
                    "connectors": True,
                },
            }
        )

    @app.get("/.well-known/engram-keys")
    def engram_keys(request: Request) -> JSONResponse:
        issuer = os.getenv("EIGHTMEM_CONTEXT_ISSUER")
        if not issuer:
            host = request.headers.get("host") or "localhost:8787"
            scheme = request.url.scheme or "http"
            issuer = f"{scheme}://{host}"
        return JSONResponse(
            {
                "issuer": issuer,
                "keys": [
                    {
                        "kid": "key-1",
                        "kty": "OKP",
                        "crv": "Ed25519",
                        "alg": "Ed25519",
                        "use": "sig",
                        "status": "unsigned-v1",
                        "public_key": None,
                        "note": "8mem reference implementation currently returns signature.value=unsigned-v1; cryptographic verification is not enabled.",
                    }
                ],
            }
        )

    @app.get("/v1/passport/card")
    def runtime_passport_card(
        user_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> Response:
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, user_id)
        if auth_error is not None:
            return auth_error
        summary = build_passport_summary(resolved_user_id)
        content = parse_passport_summary(summary)
        digest = hashlib.sha256(summary.encode("utf-8")).hexdigest()[:16]
        card_dir = ensure_cache_dir() / "api_cards"
        card_dir.mkdir(parents=True, exist_ok=True)
        path = card_dir / f"passport_{resolved_user_id or 'default'}_{digest}.png"
        render_passport_card(content, path)
        return Response(content=path.read_bytes(), media_type="image/png")

    @app.post("/v1/compare/card")
    async def runtime_compare_card(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload
        auth_error, _resolved_user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        topic = _payload_text(payload, "topic")
        generic_answer = _payload_text(payload, "generic_answer")
        memory_answer = _payload_text(payload, "memory_answer")
        used_beliefs = payload.get("used_beliefs", [])
        if not topic:
            return JSONResponse({"ok": False, "error": "Missing required string field: topic"}, status_code=400)
        if not generic_answer:
            return JSONResponse({"ok": False, "error": "Missing required string field: generic_answer"}, status_code=400)
        if not memory_answer:
            return JSONResponse({"ok": False, "error": "Missing required string field: memory_answer"}, status_code=400)
        if not isinstance(used_beliefs, list):
            return JSONResponse({"ok": False, "error": "used_beliefs must be a list when provided"}, status_code=400)
        basis = [str(item).strip() for item in used_beliefs if str(item).strip()]
        content = CompareCardContent(
            prompt=topic,
            standard_answer=generic_answer,
            memory_answer=memory_answer,
            basis=basis,
            weak_signal=False,
        )
        digest_payload = json.dumps(
            {
                "topic": topic,
                "generic_answer": generic_answer,
                "memory_answer": memory_answer,
                "used_beliefs": basis,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()[:16]
        card_dir = ensure_cache_dir() / "api_cards"
        card_dir.mkdir(parents=True, exist_ok=True)
        path = card_dir / f"compare_{digest}.png"
        render_compare_card(content, path)
        return Response(content=path.read_bytes(), media_type="image/png")

    @app.get("/v1/changes")
    def runtime_changes(
        user_id: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, user_id)
        if auth_error is not None:
            return auth_error
        return JSONResponse(build_recent_changes_payload(user_id=resolved_user_id, limit=limit))

    @app.get("/v1/connectors")
    def runtime_connectors_list(
        user_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, user_id)
        if auth_error is not None:
            return auth_error
        connectors = [public_connector(connector) for connector in list_connectors(user_id=resolved_user_id)]
        return JSONResponse({"ok": True, "connectors": connectors, "count": len(connectors)})

    @app.post("/v1/connectors")
    @app.post("/v1/connectors/register")
    async def runtime_connector_register(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error

        connector_id = _payload_text(payload, "id") or _payload_text(payload, "label")
        url = _payload_text(payload, "url")
        description = _payload_optional_text(payload, "description") or _payload_optional_text(payload, "label")
        secret = _payload_optional_text(payload, "secret")
        if not connector_id:
            return JSONResponse({"ok": False, "error": "Missing required string field: id"}, status_code=400)
        if not url:
            return JSONResponse({"ok": False, "error": "Missing required string field: url"}, status_code=400)
        connector, error = register_connector(
            connector_id=connector_id,
            url=url,
            description=description,
            user_id=resolved_user_id,
            secret=secret,
        )
        if connector is None:
            return JSONResponse({"ok": False, "error": error or "Connector registration failed"}, status_code=400)
        return JSONResponse({"ok": True, "connector": public_connector(connector), "secret": connector.secret}, status_code=201)

    @app.delete("/v1/connectors/{connector_id}")
    def runtime_connector_delete(
        connector_id: str,
        user_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ):
        auth_error, resolved_user_id = _resolve_runtime_user_id(authorization, user_id)
        if auth_error is not None:
            return auth_error
        removed = deregister_connector(connector_id, user_id=resolved_user_id)
        if not removed:
            return JSONResponse({"ok": False, "error": "Connector not found"}, status_code=404)
        return Response(status_code=204)

    @app.post("/v1/memory")
    async def runtime_memory_write(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        value = _payload_text(payload, "value") or _payload_text(payload, "text")
        if not value:
            return JSONResponse({"ok": False, "error": "Missing required string field: value or text"}, status_code=400)
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        rate_limited = _memory_write_rate_limit_error(user_id, "memory")
        if rate_limited is not None:
            return rate_limited
        file_name = _runtime_memory_file(payload)
        if file_name is None:
            return JSONResponse({"ok": False, "error": "Unsupported memory file/category"}, status_code=400)
        normalized = _normalize_runtime_memory_value(file_name, value)

        source = _payload_optional_text(payload, "source")
        confidence = _payload_optional_text(payload, "confidence")
        result = write_canonical_memory(
            user_id=user_id,
            file_name=file_name,
            value=normalized,
            source=source,
            confidence=confidence,
        )
        if result.get("status") == "written":
            result["connector_notifications"] = _notify_connectors_safely("memory.created", user_id=user_id)
            return JSONResponse(result, status_code=201)
        if result.get("status") in {"duplicate", "refinement_pending", "conflict"}:
            if result.get("status") in {"refinement_pending", "conflict"}:
                result["conflicting_belief"] = _conflicting_belief_payload(user_id, result.get("conflict"))
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    @app.post("/v1/memory/propose")
    async def runtime_memory_propose(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        value = _payload_text(payload, "value") or _payload_text(payload, "text")
        if not value:
            return JSONResponse({"ok": False, "error": "Missing required string field: value or text"}, status_code=400)
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        rate_limited = _memory_write_rate_limit_error(user_id, "memory_propose")
        if rate_limited is not None:
            return rate_limited
        file_name = _runtime_memory_file(payload)
        if file_name is None:
            return JSONResponse({"ok": False, "error": "Unsupported memory file/category"}, status_code=400)
        normalized = _normalize_runtime_memory_value(file_name, value)
        result = create_memory_proposal(
            user_id=user_id,
            file_name=file_name,
            value=normalized,
            source=_payload_optional_text(payload, "source"),
            confidence=_payload_optional_text(payload, "confidence"),
            source_message=_payload_optional_text(payload, "source_message") or value,
        )
        if result.get("status") in {"duplicate", "refinement_pending", "conflict"}:
            return JSONResponse(result, status_code=409)
        return JSONResponse(result, status_code=201 if result.get("status") == "proposed" else 200)

    @app.post("/v1/memory/candidate")
    async def runtime_memory_candidate(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        message = _payload_text(payload, "message") or _payload_text(payload, "text")
        if not message:
            return JSONResponse({"ok": False, "error": "Missing required string field: message or text"}, status_code=400)
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        rate_limited = _memory_write_rate_limit_error(user_id, "memory_candidate")
        if rate_limited is not None:
            return rate_limited
        result = propose_natural_memory_candidate(
            user_id=user_id,
            message=message,
            source=_payload_optional_text(payload, "source"),
        )
        telegram_chat_id = _candidate_telegram_chat_id(payload)
        if result.get("status") in {"proposed", "pending"} and telegram_chat_id:
            result["telegram_notification"] = _try_send_memory_candidate_to_telegram(
                result.get("proposal"),
                chat_id=telegram_chat_id,
                user_id=user_id,
            )
        if result.get("status") in {"duplicate", "refinement_pending", "conflict"}:
            return JSONResponse(result, status_code=409)
        return JSONResponse(result, status_code=201 if result.get("status") == "proposed" else 200)

    @app.post("/v1/memory/commit")
    async def runtime_memory_commit(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        proposal_id = _payload_text(payload, "proposal_id") or _payload_text(payload, "id")
        if not proposal_id:
            return JSONResponse({"ok": False, "error": "Missing required string field: proposal_id or id"}, status_code=400)
        decision = _payload_text(payload, "decision") or _payload_text(payload, "status") or "approved"
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        rate_limited = _memory_write_rate_limit_error(user_id, "memory_commit")
        if rate_limited is not None:
            return rate_limited
        result = commit_memory_proposal(user_id=user_id, proposal_id=proposal_id, decision=decision)
        if result.get("status") == "written":
            result["connector_notifications"] = _notify_connectors_safely("memory.created", user_id=user_id)
            return JSONResponse(result, status_code=201)
        if result.get("status") in {"duplicate", "refinement_pending", "conflict"}:
            return JSONResponse(result, status_code=409)
        if result.get("status") in {"not_found", "not_pending"}:
            return JSONResponse(result, status_code=404)
        if result.get("status") == "invalid_decision":
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    @app.post("/v1/correction")
    async def runtime_correction_write(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        return await _runtime_correction_write(request, authorization)

    @app.post("/v1/context/correct")
    async def runtime_context_correct_alias(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        return await _runtime_correction_write(request, authorization)

    async def _runtime_correction_write(
        request: Request,
        authorization: str | None,
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        value = _payload_text(payload, "value") or _payload_text(payload, "text")
        if not value:
            return JSONResponse({"ok": False, "error": "Missing required string field: value or text"}, status_code=400)
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        rate_limited = _memory_write_rate_limit_error(user_id, "correction")
        if rate_limited is not None:
            return rate_limited
        file_name = _runtime_correction_file(payload)
        normalized = _normalize_runtime_memory_value(file_name, value)
        event = apply_trusted_memory_change(
            user_id=user_id,
            file_name=file_name,
            new_value=normalized,
            trigger="correction",
            source_message=_payload_optional_text(payload, "source_message") or value,
            old_value=_payload_optional_text(payload, "old_value") or _payload_optional_text(payload, "old_text"),
            category=_payload_optional_text(payload, "category") or infer_memory_category(normalized),
        )
        connector_notifications = _notify_connectors_safely("memory.corrected", user_id=user_id)
        return JSONResponse(
            {
                "ok": True,
                "status": "corrected",
                "user_id": user_id,
                "correction": event,
                "connector_notifications": connector_notifications,
            },
            status_code=201,
        )

    @app.post("/v1/forget")
    async def runtime_forget(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        query = _payload_text(payload, "query") or _payload_text(payload, "text")
        if not query:
            return JSONResponse({"ok": False, "error": "Missing required string field: query or text"}, status_code=400)
        auth_error, user_id = _resolve_runtime_user_id(authorization, _payload_optional_text(payload, "user_id"))
        if auth_error is not None:
            return auth_error
        if _payload_bool(payload, "confirm", default=False):
            rate_limited = _memory_write_rate_limit_error(user_id, "forget")
            if rate_limited is not None:
                return rate_limited
        file_name = _runtime_optional_memory_file(payload)
        if file_name is None and (
            _payload_optional_text(payload, "file") is not None
            or _payload_optional_text(payload, "file_name") is not None
            or _payload_optional_text(payload, "category") is not None
        ):
            return JSONResponse({"ok": False, "error": "Unsupported memory file/category"}, status_code=400)

        candidates = find_forget_candidates(query, user_id=user_id, file_name=file_name)
        normalized_candidates = [_runtime_memory_candidate(user_id, item) for item in candidates]
        if not _payload_bool(payload, "confirm", default=False):
            return JSONResponse(
                {
                    "ok": True,
                    "status": "needs_confirmation" if candidates else "not_found",
                    "user_id": user_id,
                    "query": query,
                    "candidates": normalized_candidates,
                    "count": len(normalized_candidates),
                    "message": "Set confirm=true after the user confirms deletion.",
                }
            )

        deleted = forget_memory_entries(query, user_id=user_id, file_name=file_name)
        connector_notifications = _notify_connectors_safely("memory.forgotten", user_id=user_id) if deleted else 0
        normalized_deleted = [_runtime_memory_candidate(user_id, item) for item in deleted]
        return JSONResponse(
            {
                "ok": True,
                "status": "deleted" if deleted else "not_found",
                "user_id": user_id,
                "query": query,
                "deleted": normalized_deleted,
                "count": len(normalized_deleted),
                "connector_notifications": connector_notifications,
            }
        )

    @app.post("/v1/approval")
    async def runtime_approval_create(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        unauthorized = _runtime_auth_error(authorization)
        if unauthorized is not None:
            return unauthorized
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        action_type = _payload_text(payload, "action_type") or _payload_text(payload, "action")
        summary = _payload_text(payload, "summary")
        if not action_type:
            return JSONResponse({"ok": False, "error": "Missing required string field: action_type"}, status_code=400)
        if not summary:
            return JSONResponse({"ok": False, "error": "Missing required string field: summary"}, status_code=400)
        request_payload = payload.get("payload")
        if request_payload is not None and not isinstance(request_payload, dict):
            return JSONResponse({"ok": False, "error": "payload must be an object when provided"}, status_code=400)
        ttl_seconds = _payload_optional_int(payload, "ttl_seconds")
        approval = create_approval_request(
            action_type=action_type,
            summary=summary,
            payload=request_payload if isinstance(request_payload, dict) else {},
            user_id=_payload_optional_text(payload, "user_id"),
            ttl_seconds=ttl_seconds,
        )
        telegram_notification = _try_send_approval_to_telegram(approval)
        return JSONResponse(
            {
                "ok": True,
                "approval": approval.__dict__,
                "telegram_notification": telegram_notification,
            },
            status_code=201,
        )

    @app.get("/v1/approvals")
    def runtime_approvals_list(
        status: str | None = Query(default=None),
        since: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        unauthorized = _runtime_auth_error(authorization)
        if unauthorized is not None:
            return unauthorized
        allowed_statuses = {"pending", "approved", "denied", "expired"}
        if status is not None and status not in allowed_statuses:
            return JSONResponse({"ok": False, "error": "status must be pending, approved, denied, or expired"}, status_code=400)
        since_dt = _parse_optional_iso_datetime(since)
        if since is not None and since_dt is None:
            return JSONResponse({"ok": False, "error": "since must be ISO8601 datetime"}, status_code=400)
        approvals = list_approval_requests(user_id=user_id, status=status if status in allowed_statuses else None)
        if since_dt is not None:
            approvals = [
                approval
                for approval in approvals
                if datetime.fromisoformat(approval.created_at) >= since_dt
                or (approval.resolved_at is not None and datetime.fromisoformat(approval.resolved_at) >= since_dt)
            ]
        total = len(approvals)
        return JSONResponse(
            {
                "ok": True,
                "approvals": [approval.__dict__ for approval in approvals[:limit]],
                "count": min(total, limit),
                "total": total,
                "limit": limit,
            }
        )

    @app.get("/v1/approval/{approval_id}")
    def runtime_approval_get(
        approval_id: str,
        user_id: str | None = Query(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        unauthorized = _runtime_auth_error(authorization)
        if unauthorized is not None:
            return unauthorized
        approval = get_approval_request(approval_id, user_id=user_id)
        if approval is None:
            return JSONResponse({"ok": False, "error": "Approval not found"}, status_code=404)
        return JSONResponse({"ok": True, "approval": approval.__dict__})

    @app.post("/v1/approval/{approval_id}")
    async def runtime_approval_resolve(
        approval_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        unauthorized = _runtime_auth_error(authorization)
        if unauthorized is not None:
            return unauthorized
        payload = await _read_json_payload(request)
        if isinstance(payload, JSONResponse):
            return payload

        decision = _payload_text(payload, "decision").lower()
        if decision not in {"approved", "denied", "approve", "deny", "edit"}:
            return JSONResponse({"ok": False, "error": "decision must be approved, denied, or edit"}, status_code=400)
        normalized_decision = "approved" if decision in {"approved", "approve"} else "denied"
        reason = _payload_optional_text(payload, "reason")
        if decision == "edit" and reason is None:
            reason = "User requested edit"
        approval, error = resolve_approval_request(
            approval_id,
            decision=normalized_decision,
            user_id=_payload_optional_text(payload, "user_id"),
            resolved_by=_payload_optional_text(payload, "resolved_by"),
            reason=reason,
        )
        if error == "not_found":
            return JSONResponse({"ok": False, "error": "Approval not found"}, status_code=404)
        if error == "already_resolved":
            return JSONResponse(
                {"ok": False, "error": "Approval is already resolved", "approval": approval.__dict__ if approval else None},
                status_code=409,
            )
        return JSONResponse({"ok": True, "approval": approval.__dict__ if approval else None})

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard_alias() -> RedirectResponse:
        return RedirectResponse(url="/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        status: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        context = load_dashboard_payload(user_id=user_id)
        context["status"] = status
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.post("/memory/remember")
    def browser_memory_remember(
        value: str = Form(default=""),
        user_id: str | None = Query(default=None),
    ) -> RedirectResponse:
        cleaned = " ".join(value.strip().split())
        if not cleaned:
            return _redirect_dashboard_status("remember-empty", user_id=user_id)

        payload = {"value": cleaned, "category": infer_memory_category(cleaned)}
        file_name = _runtime_memory_file(payload) or "BELIEFS.md"
        normalized = _normalize_runtime_memory_value(file_name, cleaned)
        try:
            result = write_canonical_memory(
                user_id=user_id,
                file_name=file_name,
                value=normalized,
                source="browser",
            )
        except Exception as exc:  # pragma: no cover - defensive UI guard
            logger.warning("browser memory write failed: %s", exc)
            return _redirect_dashboard_status("remember-error", user_id=user_id)

        result_status = result.get("status")
        if result_status == "written":
            _notify_connectors_safely("memory.created", user_id=user_id)
            return _redirect_dashboard_status("remembered", user_id=user_id)
        if result_status == "duplicate":
            return _redirect_dashboard_status("remember-duplicate", user_id=user_id)
        if result_status in {"refinement_pending", "conflict"}:
            return _redirect_dashboard_status("remember-review", user_id=user_id)
        return _redirect_dashboard_status("remember-error", user_id=user_id)

    @app.get("/import", response_class=HTMLResponse)
    def import_page(
        request: Request,
        status: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
    ) -> HTMLResponse:
        context = build_base_context("import", user_id=user_id)
        return templates.TemplateResponse(request, "import.html", {**context, "status": status})

    @app.post("/import-sample")
    def import_sample() -> RedirectResponse:
        mem = initialize_memory_runtime()
        sample_ref = importlib_resources.files("eightmem.resources").joinpath("sample_data/chat_export_sample.json")
        try:
            with importlib_resources.as_file(sample_ref) as sample:
                if sample.exists():
                    analyze_chat_export(sample, mem)
                    return RedirectResponse(url="/import?status=sample-loaded", status_code=303)
        except FileNotFoundError:
            pass
        return RedirectResponse(url="/import?status=sample-missing", status_code=303)

    @app.get("/mirror", response_class=HTMLResponse)
    def mirror_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = get_mirror_payload(user_id=user_id)
        return templates.TemplateResponse(request, "mirror.html", context)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = get_memory_inbox_payload(user_id=user_id)
        return templates.TemplateResponse(request, "inbox.html", context)

    @app.get("/files", response_class=HTMLResponse)
    def files_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = get_files_overview(user_id=user_id)
        return templates.TemplateResponse(request, "files.html", context)

    @app.get("/editor", response_class=HTMLResponse)
    def editor_page(name: str = Query(default="IDENTITY.md")) -> RedirectResponse:
        resolved_name = resolve_public_memory_file_name(name)
        editor_context = get_file_editor_payload(resolved_name)
        slug = str(editor_context.get("slug") or resolved_name) if editor_context else resolved_name
        return RedirectResponse(url=f"/files/{slug}", status_code=303)

    @app.get("/files/{name}", response_class=HTMLResponse)
    def file_editor(request: Request, name: str, user_id: str | None = Query(default=None)) -> HTMLResponse:
        name = resolve_public_memory_file_name(name)
        context = get_file_editor_payload(name, user_id=user_id)
        if context is None:
            return RedirectResponse(url="/files", status_code=303)
        return templates.TemplateResponse(request, "edit.html", context)

    @app.post("/files/{name}")
    async def save_file(
        name: str,
        content: str = Form(...),
        user_id: str | None = Query(default=None),
    ) -> RedirectResponse:
        name = resolve_public_memory_file_name(name)
        if not save_file_content(name, content, user_id=user_id):
            return RedirectResponse(url="/files", status_code=303)
        editor_context = get_file_editor_payload(name, user_id=user_id)
        slug = str(editor_context.get("slug") or name) if editor_context else name
        location = f"/files/{slug}"
        if user_id:
            location = f"{location}?user_id={user_id}"
        return RedirectResponse(url=location, status_code=303)

    @app.post("/inbox/resolve")
    async def resolve_inbox_item(
        item_id: str = Form(...),
        decision: str = Form(...),
        user_id: str | None = Query(default=None),
    ) -> RedirectResponse:
        resolved = resolve_memory_inbox_item(item_id, decision, user_id=user_id)
        if resolved and item_id.startswith("memory_proposal:") and decision in {"save", "approved"}:
            _notify_connectors_safely("memory.created", user_id=user_id)
        location = "/inbox"
        if user_id:
            location = f"{location}?user_id={user_id}"
        return RedirectResponse(url=location, status_code=303)

    @app.post("/memory/undo")
    async def undo_memory_change(user_id: str | None = Query(default=None)) -> RedirectResponse:
        undo_last_memory_change(user_id=user_id)
        location = "/"
        if user_id:
            location = f"{location}?user_id={user_id}"
        return RedirectResponse(url=location, status_code=303)

    @app.post("/analyze-upload")
    async def analyze_upload(file: UploadFile = File(...)) -> RedirectResponse:
        mem = initialize_memory_runtime()
        upload_name = Path(file.filename or "chat.txt").name or "chat.txt"
        temp = mem / f"_upload_{upload_name}"
        data = await file.read()
        temp.write_bytes(data)
        try:
            analyze_chat_export(temp, mem)
        finally:
            temp.unlink(missing_ok=True)
        return RedirectResponse(url="/import?status=analyzed", status_code=303)

    @app.get("/export", response_class=HTMLResponse)
    def export_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = build_base_context("export", user_id=user_id)
        return templates.TemplateResponse(
            request,
            "export.html",
            {**context, "compiled_context": export_context_text(user_id=user_id)},
        )

    @app.get("/export/download", response_class=PlainTextResponse)
    def export_download(user_id: str | None = Query(default=None)) -> PlainTextResponse:
        compiled = export_context_text(user_id=user_id)
        response = PlainTextResponse(compiled)
        response.headers["Content-Disposition"] = 'attachment; filename="8mem_context.txt"'
        return response

    @app.get("/everywhere", response_class=HTMLResponse)
    def everywhere_page(request: Request, user_id: str | None = Query(default=None)) -> HTMLResponse:
        context = build_base_context("everywhere", user_id=user_id)
        return templates.TemplateResponse(request, "everywhere.html", context)

    @app.post("/channels/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> JSONResponse:
        client_host = request.client.host if request.client else None
        try:
            config = load_telegram_config()
        except TelegramConfigError as exc:
            _log_telegram_event(
                "telegram_config_error",
                status="rejected",
                client_host=client_host,
                error=str(exc),
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

        if not verify_webhook_secret(config, x_telegram_bot_api_secret_token):
            _log_telegram_event(
                "telegram_bad_secret",
                status="rejected",
                client_host=client_host,
            )
            return JSONResponse({"ok": False, "error": "Invalid Telegram webhook secret"}, status_code=403)

        payload = await request.json()
        outgoing = process_telegram_update(payload)
        if outgoing is None:
            _log_telegram_event(
                "telegram_ignored_update",
                status="ignored",
                client_host=client_host,
                update_id=payload.get("update_id"),
            )
            return JSONResponse({"ok": True, "ignored": True})

        duplicate = _mark_duplicate_update(telegram_seen_updates, outgoing.user_id, outgoing.update_id)
        if duplicate:
            _log_telegram_event(
                "telegram_duplicate_update",
                status="ignored",
                client_host=client_host,
                update_id=outgoing.update_id,
                user_id=outgoing.user_id,
                chat_id=outgoing.chat_id,
            )
            return JSONResponse({"ok": True, "ignored": True, "duplicate": True})

        limited = _check_rate_limit(telegram_user_windows, outgoing.user_id)
        if limited:
            _log_telegram_event(
                "telegram_rate_limited",
                status="rejected",
                client_host=client_host,
                update_id=outgoing.update_id,
                user_id=outgoing.user_id,
                chat_id=outgoing.chat_id,
            )
            return JSONResponse({"ok": False, "error": "Rate limit exceeded"}, status_code=429)

        try:
            send_result = send_telegram_message(config, outgoing)
        except TelegramSendError as exc:
            _log_telegram_event(
                "telegram_send_failed",
                status="error",
                client_host=client_host,
                update_id=outgoing.update_id,
                user_id=outgoing.user_id,
                chat_id=outgoing.chat_id,
                action=outgoing.action,
                memory_updated=outgoing.memory_updated,
                error=str(exc),
            )
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

        _log_telegram_event(
            "telegram_message_processed",
            status="ok",
            client_host=client_host,
            update_id=outgoing.update_id,
            user_id=outgoing.user_id,
            chat_id=outgoing.chat_id,
            action=outgoing.action,
            memory_updated=outgoing.memory_updated,
            response_length=len(outgoing.text),
            telegram_ok=send_result.get("ok"),
        )
        return JSONResponse({"ok": True, "telegram": send_result, "chat_id": outgoing.chat_id})

    return app


def _log_telegram_event(event: str, **fields: object) -> None:
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, sort_keys=True, default=str))


def _try_send_approval_to_telegram(approval: object) -> dict[str, object]:
    user_id = getattr(approval, "user_id", None)
    approval_id = getattr(approval, "approval_id", "")
    chat_id = _approval_telegram_chat_id(approval)
    if not chat_id:
        return {"attempted": False, "reason": "missing_telegram_chat_id"}
    try:
        config = _load_approval_telegram_config(approval)
    except TelegramConfigError as exc:
        return {"attempted": False, "reason": str(exc)}

    keyboard = [
        [
            {"text": "Approve", "callback_data": f"approval:approved:{approval_id}"},
            {"text": "Deny", "callback_data": f"approval:denied:{approval_id}"},
            {"text": "Edit", "callback_data": f"approval:edit:{approval_id}"},
        ],
        [{"text": "Show pending approvals", "callback_data": "/approvals"}],
    ]
    outgoing = TelegramOutgoingMessage(
        chat_id=chat_id,
        text=build_telegram_approval_message(approval),  # type: ignore[arg-type]
        user_id=user_id if isinstance(user_id, str) and user_id.strip() else chat_id,
        update_id=0,
        action="approval_notification",
        reply_keyboard=keyboard,
    )
    try:
        result = send_telegram_message(config, outgoing)
    except TelegramSendError as exc:
        return {"attempted": True, "ok": False, "chat_id": chat_id, "bot_token_prefix": _token_prefix(config.bot_token), "error": str(exc)}
    response: dict[str, object] = {"attempted": True, "ok": bool(result.get("ok")), "chat_id": chat_id, "result": result.get("result")}
    if not result.get("ok"):
        response["error"] = result.get("description") or result
        response["bot_token_prefix"] = _token_prefix(config.bot_token)
        response["telegram_error"] = result
    return response


def _try_send_memory_candidate_to_telegram(
    proposal: object,
    *,
    chat_id: str,
    user_id: str | None,
) -> dict[str, object]:
    if not isinstance(proposal, dict):
        return {"attempted": False, "reason": "missing_proposal"}
    proposal_id = proposal.get("id")
    value = proposal.get("value")
    if not isinstance(proposal_id, str) or not proposal_id.strip() or not isinstance(value, str) or not value.strip():
        return {"attempted": False, "reason": "invalid_proposal"}
    try:
        config = load_telegram_config()
    except TelegramConfigError as exc:
        return {"attempted": False, "reason": str(exc)}

    outgoing = TelegramOutgoingMessage(
        chat_id=chat_id,
        text="\n".join(
            [
                "Possible memory",
                "",
                value.strip(),
                "",
                "Save this to memory?",
            ]
        ),
        user_id=user_id or chat_id,
        update_id=0,
        action="memory_candidate_notification",
        reply_keyboard=[
            [
                {"text": "Save", "callback_data": f"memory_proposal:save:{proposal_id}"},
                {"text": "Skip", "callback_data": f"memory_proposal:skip:{proposal_id}"},
            ]
        ],
    )
    try:
        result = send_telegram_message(config, outgoing)
    except TelegramSendError as exc:
        return {"attempted": True, "ok": False, "chat_id": chat_id, "error": str(exc)}
    response: dict[str, object] = {"attempted": True, "ok": bool(result.get("ok")), "chat_id": chat_id, "result": result.get("result")}
    if not result.get("ok"):
        response["error"] = result.get("description") or result
    return response


def _candidate_telegram_chat_id(payload: dict[str, Any]) -> str | None:
    explicit_chat_id = _payload_optional_text(payload, "telegram_chat_id") or _payload_optional_text(payload, "chat_id")
    if explicit_chat_id:
        return explicit_chat_id
    if (_payload_optional_text(payload, "platform") or "").lower() == "telegram":
        return _payload_optional_text(payload, "user_id")
    return None


def _conflicting_belief_payload(user_id: str | None, conflict: object) -> dict[str, object] | None:
    if not isinstance(conflict, dict):
        return None
    file_name = conflict.get("file_name")
    value = conflict.get("value")
    if not isinstance(file_name, str) or not isinstance(value, str) or not value.strip():
        return None
    return {
        "id": runtime_memory_id(user_id, file_name, value),
        "value": value,
        "confidence": "explicit_user_statement",
    }


def _load_approval_telegram_config(approval: object | None = None) -> TelegramConfig:
    token = _approval_telegram_bot_token(approval)
    if token:
        secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
        return TelegramConfig(bot_token=token, webhook_secret=secret)
    return load_telegram_config()


def _approval_telegram_bot_token(approval: object | None) -> str | None:
    payload = getattr(approval, "payload", None)
    if isinstance(payload, dict):
        value = payload.get("telegram_bot_token")
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("EIGHTMEM_APPROVAL_TELEGRAM_BOT_TOKEN", "TELEGRAM_APPROVAL_BOT_TOKEN"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return None


def _approval_telegram_chat_id(approval: object) -> str | None:
    payload = getattr(approval, "payload", None)
    if isinstance(payload, dict):
        for key in ("telegram_chat_id", "chat_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int):
                return str(value)
    for key in ("EIGHTMEM_TELEGRAM_APPROVAL_CHAT_ID", "TELEGRAM_APPROVAL_CHAT_ID"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    user_id = getattr(approval, "user_id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return None


def _runtime_auth_error(authorization: str | None) -> JSONResponse | None:
    expected_key = os.getenv("EIGHTMEM_LOCAL_API_KEY", "local_key")
    if authorization != f"Bearer {expected_key}":
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    return None


def _notify_connectors_safely(event: str, *, user_id: str | None = None) -> int:
    try:
        return notify_connectors(event, user_id=user_id)
    except Exception as exc:  # pragma: no cover - defensive launch safety guard
        logger.warning("8mem connector webhook push failed: %s", exc)
        return 0


def _redirect_dashboard_status(status: str, *, user_id: str | None = None) -> RedirectResponse:
    query: dict[str, str] = {"status": status}
    if user_id:
        query["user_id"] = user_id
    return RedirectResponse(url=f"/?{urlencode(query)}", status_code=303)


def _resolve_runtime_user_id(
    authorization: str | None,
    explicit_user_id: str | None = None,
) -> tuple[JSONResponse | None, str | None]:
    auth_error = _runtime_auth_error(authorization)
    if auth_error is not None:
        return auth_error, None

    owner_user_id = os.getenv("EIGHTMEM_CONTEXT_USER_ID", "").strip() or None
    allowed_user_ids = _runtime_allowed_user_ids(owner_user_id)
    if explicit_user_id:
        if allowed_user_ids is not None and "*" not in allowed_user_ids and explicit_user_id not in allowed_user_ids:
            return JSONResponse({"ok": False, "error": "Forbidden for requested user_id"}, status_code=403), None
        return None, explicit_user_id

    return None, owner_user_id


def _runtime_allowed_user_ids(owner_user_id: str | None) -> set[str] | None:
    configured = {
        item.strip()
        for item in os.getenv("EIGHTMEM_ALLOWED_USER_IDS", "").split(",")
        if item.strip()
    }
    if owner_user_id:
        configured.add(owner_user_id)
    # No configured owner means local/dev mode keeps historical explicit user_id behavior.
    return configured or None


async def _read_json_payload(request: Request) -> dict[str, Any] | JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "JSON body must be an object"}, status_code=400)
    return payload


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _payload_optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = _payload_text(payload, key)
    return value or None


def _payload_int(payload: dict[str, Any], key: str, *, default: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _payload_optional_int(payload: dict[str, Any], key: str) -> int | None:
    if key not in payload:
        return None
    value = _payload_int(payload, key, default=-1)
    return value if value >= 0 else None


def _payload_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    return default


def _parse_optional_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _token_prefix(token: str) -> str:
    return token[:10]


def _runtime_memory_file(payload: dict[str, Any]) -> str | None:
    requested = _payload_optional_text(payload, "file") or _payload_optional_text(payload, "file_name")
    if requested:
        normalized = requested.strip().upper()
        if not normalized.endswith(".MD"):
            normalized = f"{normalized}.MD"
        allowed = {
            "IDENTITY.MD": "IDENTITY.md",
            "BELIEFS.MD": "BELIEFS.md",
            "PREFERENCES.MD": "PREFERENCES.md",
            "CORRECTIONS.MD": "CORRECTIONS.md",
            "DECISIONS.MD": "DECISIONS.md",
            "EVOLUTION.MD": "EVOLUTION.md",
        }
        return allowed.get(normalized)

    value = _payload_text(payload, "value") or _payload_text(payload, "text")
    category = (_payload_optional_text(payload, "category") or "").lower()
    if category in {"identity", "profile"}:
        return "IDENTITY.md"
    if category in {"preference", "style", "communication", "communication style"}:
        return "PREFERENCES.md"
    if category in {"correction", "guardrail"}:
        return "CORRECTIONS.md"
    if category in {"decision"}:
        return "DECISIONS.md"
    if category in {"evolution", "change"}:
        return "EVOLUTION.md"
    if _looks_like_runtime_identity(value):
        return "IDENTITY.md"
    if _looks_like_runtime_preference(value):
        return "PREFERENCES.md"
    return "BELIEFS.md"


def _runtime_optional_memory_file(payload: dict[str, Any]) -> str | None:
    if (
        _payload_optional_text(payload, "file") is None
        and _payload_optional_text(payload, "file_name") is None
        and _payload_optional_text(payload, "category") is None
    ):
        return None
    return _runtime_memory_file(payload)


def _runtime_correction_file(payload: dict[str, Any]) -> str:
    file_name = _runtime_memory_file(payload)
    if file_name in {"PREFERENCES.md", "IDENTITY.md"}:
        return file_name
    return "CORRECTIONS.md"


def _normalize_runtime_memory_value(file_name: str, value: str) -> str:
    if file_name == "IDENTITY.md":
        return _normalize_runtime_identity_value(value)
    if file_name == "PREFERENCES.md":
        return normalize_preference_text(value)
    if file_name == "CORRECTIONS.md":
        return normalize_correction_text(value).rstrip(".") + "."
    cleaned = " ".join(value.strip().split())
    return cleaned.rstrip(".") + "." if cleaned else cleaned


def _looks_like_runtime_identity(value: str) -> bool:
    lowered = " ".join(value.lower().strip().split())
    return lowered.startswith(
        (
            "name:",
            "timezone:",
            "time zone:",
            "current location:",
            "location:",
            "my timezone is ",
            "timezone ",
            "i live in ",
            "i am based in ",
            "user is ",
        )
    )


def _normalize_runtime_identity_value(value: str) -> str:
    cleaned = " ".join(value.strip().split()).rstrip(".")
    lowered = cleaned.lower()
    if lowered.startswith("time zone:"):
        return f"Timezone: {cleaned.split(':', 1)[1].strip()}"
    if lowered.startswith("timezone:"):
        return f"Timezone: {cleaned.split(':', 1)[1].strip()}"
    if lowered.startswith("my timezone is "):
        return f"Timezone: {cleaned[15:].strip()}"
    if lowered.startswith("timezone "):
        return f"Timezone: {cleaned[9:].strip()}"
    if lowered.startswith("name:"):
        return f"Name: {cleaned.split(':', 1)[1].strip()}"
    if lowered.startswith("location:"):
        return f"Current location: {cleaned.split(':', 1)[1].strip()}"
    if lowered.startswith("current location:"):
        return f"Current location: {cleaned.split(':', 1)[1].strip()}"
    return cleaned


def _looks_like_runtime_preference(value: str) -> bool:
    lowered = " ".join(value.lower().split())
    return lowered.startswith(
        (
            "i prefer ",
            "prefer ",
            "prefers ",
            "i like ",
            "i want ",
            "please use ",
            "please keep ",
            "keep ",
            "always ",
            "from now on ",
            "avoid ",
            "do not ",
            "don't ",
        )
    )


def _runtime_memory_id(user_id: str | None, file_name: str, value: str) -> str:
    scope = user_id or "default"
    digest = hashlib.sha256(f"{scope}\0{file_name}\0{value.lower()}".encode("utf-8")).hexdigest()
    return f"mem_{digest[:16]}"


def _runtime_memory_candidate(user_id: str | None, item: dict[str, str]) -> dict[str, str]:
    file_name = item["file_name"]
    value = item["value"]
    return {
        "id": _runtime_memory_id(user_id, file_name, value),
        "value": value,
    }


def _mark_duplicate_update(seen_updates: dict[str, float], user_id: str, update_id: int) -> bool:
    now = time.monotonic()
    cutoff = now - (15 * 60)
    stale_keys = [key for key, seen_at in seen_updates.items() if seen_at < cutoff]
    for key in stale_keys:
        seen_updates.pop(key, None)

    dedup_key = f"{user_id}:{update_id}"
    if dedup_key in seen_updates:
        return True
    seen_updates[dedup_key] = now
    return False


def _check_rate_limit(user_windows: dict[str, list[float]], user_id: str) -> bool:
    now = time.monotonic()
    cutoff = now - TELEGRAM_RATE_LIMIT_WINDOW_SECONDS
    window = [ts for ts in user_windows.get(user_id, []) if ts >= cutoff]
    limited = len(window) >= TELEGRAM_RATE_LIMIT_MAX_REQUESTS
    if not limited:
        window.append(now)
    user_windows[user_id] = window
    return limited

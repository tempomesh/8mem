from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re

from eightmem.llm.ollama import generate_text, resolve_default_model
from eightmem.core.governance import ApprovalRequest, get_approval_request, list_approval_requests, resolve_approval_request
from eightmem.services.compare_card import build_compare_share_card, build_passport_share_card, is_passport_card_eligible, is_share_card_eligible
from eightmem.services.memory_service import (
    apply_memory_updates,
    apply_trusted_memory_change,
    build_compare_output,
    build_corrections_summary,
    build_passport_summary,
    build_recent_changes_summary,
    build_base_context,
    build_structured_memory_summary,
    clear_pending_contradiction,
    commit_memory_proposal,
    find_forget_candidates,
    find_conflicting_active_memory,
    forget_memory_entries,
    generate_memory_aware_draft,
    generate_memory_aware_reply,
    get_memory_dir,
    get_retrieval_sections,
    get_structured_tensions,
    infer_memory_category,
    load_pending_contradiction,
    get_memory_resolution_suggestions,
    normalize_correction_text,
    normalize_preference_text,
    reset_memory_runtime,
    resolve_memory_inbox_item,
    save_pending_contradiction,
    undo_last_memory_change,
)
from eightmem.services.webhook_service import notify_connectors
logger = logging.getLogger("eightmem.telegram")


@dataclass
class TelegramReply:
    message: str
    action: str
    memory_updated: bool = False
    reply_keyboard: list[list[str | dict[str, str]]] | None = None
    remove_keyboard: bool = False
    photo_path: Path | None = None


UTILITY_KEYBOARD = [["/passport", "/compare"]]
UNDO_UTILITY_KEYBOARD = [["Undo last memory change"], ["/passport", "/compare"]]
PENDING_COMPARE_SETUP_NAME = ".pending_compare_setup.json"
PENDING_FORGET_NAME = ".pending_forget.json"


def _notify_memory_change(event: str, *, user_id: str) -> int:
    try:
        return notify_connectors(event, user_id=user_id)
    except Exception as exc:  # pragma: no cover - connector failure must not break Telegram UX
        logger.warning("telegram_connector_notify_failed event=%s user_id=%s error=%s", event, user_id, exc)
        return 0


def process_telegram_message(
    *,
    user_id: str,
    text: str,
    model: str | None = None,
) -> TelegramReply:
    normalized_text = _normalize_inbound_message(text)
    context_submission = _detect_context_interview_submission(normalized_text)
    if context_submission is not None:
        pending_compare = _load_pending_compare_setup(user_id)
        if pending_compare is not None:
            return _save_context_and_rerun_pending_compare(
                user_id=user_id,
                updates=context_submission,
                pending_compare=pending_compare,
                model=model,
            )
        return _save_context_interview_submission(user_id=user_id, updates=context_submission)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    processable_lines = [line for line in lines if _is_processable_batch_line(line)]
    ignored_lines = [line for line in lines if not _is_processable_batch_line(line)]

    if len(lines) > 1 and processable_lines:
        replies = [_process_single_telegram_message(user_id=user_id, text=line, model=model) for line in processable_lines]
        replies = _dedupe_batch_conflict_prompts(replies)
        replies = _dedupe_batch_messages(replies)
        note = ""
        if ignored_lines and not all(_is_ignorable_heading(line.lower()) for line in ignored_lines):
            if len(ignored_lines) == 1:
                note = "\n\nI skipped one unrelated line in that message."
            else:
                note = f"\n\nI skipped {len(ignored_lines)} unrelated lines in that message."
        keyboard = None
        remove_keyboard = False
        for reply in reversed(replies):
            if reply.reply_keyboard:
                keyboard = reply.reply_keyboard
                break
            if reply.remove_keyboard:
                remove_keyboard = True
                break
        return TelegramReply(
            message="\n\n".join(reply.message for reply in replies) + note,
            action="batch_commands",
            memory_updated=any(reply.memory_updated for reply in replies),
            reply_keyboard=keyboard,
            remove_keyboard=remove_keyboard and keyboard is None,
        )
    return _process_single_telegram_message(user_id=user_id, text=normalized_text, model=model)


def _process_single_telegram_message(
    *,
    user_id: str,
    text: str,
    model: str | None = None,
) -> TelegramReply:
    message = _normalize_inbound_message(text)
    lowered = message.lower()
    natural_intent = _route_natural_intent(message, model=model)
    onboarding_kind = _get_onboarding_kind(message)
    meta_reply = _build_meta_reply(message, user_id=user_id, model=model)

    if onboarding_kind:
        return TelegramReply(
            message=_build_onboarding_message(user_id, kind=onboarding_kind),
            action="onboarding_help",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if meta_reply is not None:
        return meta_reply

    approval_reply = _try_handle_approval_message(user_id=user_id, message=message)
    if approval_reply is not None:
        return approval_reply

    proposal_reply = _try_handle_memory_proposal_message(user_id=user_id, message=message)
    if proposal_reply is not None:
        return proposal_reply

    pending_resolution = _try_resolve_pending_contradiction(user_id=user_id, message=message)
    if pending_resolution is not None:
        return pending_resolution

    correction_capture = _detect_correction_update(message)
    if correction_capture:
        file_name, normalized_text = correction_capture
        if not normalized_text:
            return TelegramReply(
                message="What should I replace it with?",
                action="correction_clarification",
            )
        event = apply_trusted_memory_change(
            user_id=user_id,
            file_name=file_name,
            new_value=normalized_text,
            trigger="correction",
            source_message=message,
            category=infer_memory_category(normalized_text),
        )
        _notify_memory_change("memory.corrected", user_id=user_id)
        keyboard = UNDO_UTILITY_KEYBOARD
        return TelegramReply(
            message=_build_trusted_change_confirmation(event),
            action="save_correction",
            memory_updated=True,
            reply_keyboard=keyboard,
        )

    if lowered.startswith("remember"):
        body = _strip_prefix(message, "remember")
        preference = normalize_preference_text(body)
        conflict = find_conflicting_active_memory(user_id, preference, target_file="PREFERENCES.md")
        if conflict:
            save_pending_contradiction(
                user_id=user_id,
                file_name="PREFERENCES.md",
                new_value=preference,
                old_file=conflict["file_name"],
                old_value=conflict["value"],
                source_message=message,
            )
            return TelegramReply(
                message=_build_contradiction_prompt(conflict["value"], preference),
                action="contradiction_prompt",
                reply_keyboard=[["Use new", "Keep old"], ["Both are true", "Review later"]],
            )
        apply_memory_updates({"PREFERENCES.md": {preference}}, user_id=user_id)
        _notify_memory_change("memory.created", user_id=user_id)
        followup = _build_telegram_resolution_prompt(user_id)
        keyboard = build_telegram_reply_keyboard(user_id) if followup else None
        return TelegramReply(
            message=f"Saved: {preference}{followup}",
            action="save_preference",
            memory_updated=True,
            reply_keyboard=keyboard,
        )

    if lowered.startswith("correct"):
        body = _strip_prefix(message, "correct")
        correction = normalize_correction_text(body.strip(" :.-"))
        event = apply_trusted_memory_change(
            user_id=user_id,
            file_name="CORRECTIONS.md",
            new_value=correction,
            trigger="correction",
            source_message=message,
            category=infer_memory_category(correction),
        )
        _notify_memory_change("memory.corrected", user_id=user_id)
        followup = _build_telegram_resolution_prompt(user_id)
        keyboard = build_telegram_reply_keyboard(user_id) if followup else None
        return TelegramReply(
            message=f"{_build_trusted_change_confirmation(event)}{followup}",
            action="save_correction",
            memory_updated=True,
            reply_keyboard=keyboard,
        )

    if lowered == "review later":
        return TelegramReply(
            message="Okay. I won't change your memory yet.",
            action="defer_memory_conflict",
            remove_keyboard=True,
        )

    if lowered in {"forget yes", "yes forget"}:
        pending_forget = _load_pending_forget(user_id)
        if pending_forget is None:
            return TelegramReply(message="There is no pending forget request.", action="forget_memory_missing")
        deleted = forget_memory_entries(
            pending_forget["query"],
            user_id=user_id,
            file_name=pending_forget.get("file_name"),
        )
        _clear_pending_forget(user_id)
        if not deleted:
            return TelegramReply(message="No matching memory was deleted.", action="forget_memory_empty")
        lines = ["Deleted from memory:"]
        for item in deleted[:5]:
            lines.append(f"- {item['value']}")
        lines.append("")
        lines.append("You can see this in /corrections.")
        return TelegramReply(
            message="\n".join(lines),
            action="forget_memory_confirmed",
            memory_updated=True,
            reply_keyboard=UNDO_UTILITY_KEYBOARD,
        )

    if lowered in {"forget no", "no forget", "cancel forget"}:
        _clear_pending_forget(user_id)
        return TelegramReply(message="Forget cancelled.", action="forget_memory_cancelled", remove_keyboard=True)

    if _is_resolution_phrase_without_pending(lowered):
        return TelegramReply(
            message="There is no memory conflict waiting for that choice right now.",
            action="resolve_memory_conflict_missing",
        )

    resolution = _match_telegram_resolution_command(lowered)
    if resolution:
        item_id, decision, confirmation = resolution
        success = resolve_memory_inbox_item(item_id, decision, user_id=user_id)
        if success:
            return TelegramReply(
                message=confirmation,
                action="resolve_memory_conflict",
                memory_updated=True,
                remove_keyboard=True,
            )
        return TelegramReply(
            message="I couldn't apply that memory resolution because there is no matching conflict right now.",
            action="resolve_memory_conflict_missing",
        )

    if lowered in {"undo", "undo last memory change", "undo memory"}:
        success = undo_last_memory_change(user_id=user_id)
        if success:
            return TelegramReply(
                message="Undid the last memory change.",
                action="undo_memory_change",
                memory_updated=True,
                remove_keyboard=True,
            )
        return TelegramReply(
            message="There is no recent memory change to undo.",
            action="undo_memory_missing",
        )

    if lowered in {"reset memory", "forget everything", "/resetmemory"}:
        reset_memory_runtime(user_id)
        return TelegramReply(
            message="Memory reset for this chat user.",
            action="reset_memory",
            memory_updated=True,
            remove_keyboard=True,
        )

    forget_query = _extract_forget_query(message)
    if forget_query:
        matches = find_forget_candidates(forget_query, user_id=user_id)
        if not matches:
            return TelegramReply(
                message=f'I could not find a saved memory matching "{forget_query}".',
                action="forget_memory_not_found",
            )
        _save_pending_forget(user_id, forget_query)
        lines = ["I found this saved memory:" if len(matches) == 1 else "I found these saved memories:"]
        for item in matches[:5]:
            lines.append(f"- {item['value']}")
        if len(matches) > 5:
            lines.append(f"- ...and {len(matches) - 5} more")
        lines.append("")
        lines.append('Reply "forget yes" to delete, or "forget no" to cancel.')
        return TelegramReply(
            message="\n".join(lines),
            action="forget_memory_confirm",
            reply_keyboard=[["forget yes", "forget no"]],
        )

    if _is_recall_request(lowered) or natural_intent == "recall":
        summary = _build_recall_summary(user_id)
        return TelegramReply(message=summary, action="recall_summary", reply_keyboard=UTILITY_KEYBOARD)

    if lowered in {"/corrections", "corrections"} or natural_intent == "corrections":
        return TelegramReply(
            message=build_corrections_summary(user_id),
            action="corrections_summary",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if natural_intent == "changes":
        return TelegramReply(
            message=build_recent_changes_summary(user_id),
            action="recent_changes_summary",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if lowered in {"/passport", "passport"} or natural_intent == "passport":
        passport_summary = build_passport_summary(user_id)
        photo_path = None
        if is_passport_card_eligible(passport_summary):
            try:
                photo_path = build_passport_share_card(passport_summary, user_id=user_id)
            except Exception as exc:  # pragma: no cover - defensive: text passport must still work
                logger.warning("telegram_passport_card_unavailable user_id=%s error=%s", user_id, exc)
        return TelegramReply(
            message=(
                f"{passport_summary}\n\n"
                "Try a compare next if you want to see the difference it makes."
            ),
            action="memory_passport",
            reply_keyboard=UTILITY_KEYBOARD,
            photo_path=photo_path,
        )

    natural_capture = _detect_natural_memory_capture(message)
    if natural_capture:
        file_name, normalized_text, acknowledgement = natural_capture
        conflict = find_conflicting_active_memory(user_id, normalized_text, target_file=file_name)
        if conflict:
            save_pending_contradiction(
                user_id=user_id,
                file_name=file_name,
                new_value=normalized_text,
                old_file=conflict["file_name"],
                old_value=conflict["value"],
                source_message=message,
            )
            return TelegramReply(
                message=_build_contradiction_prompt(conflict["value"], normalized_text),
                action="contradiction_prompt",
                reply_keyboard=[["Use new", "Keep old"], ["Both are true", "Review later"]],
            )
        apply_memory_updates({file_name: {normalized_text}}, user_id=user_id)
        _notify_memory_change("memory.created", user_id=user_id)
        followup = _build_telegram_resolution_prompt(user_id)
        saved_text = normalized_text.rstrip(".") + "."
        keyboard = build_telegram_reply_keyboard(user_id) if followup else UNDO_UTILITY_KEYBOARD
        return TelegramReply(
            message=(
                f"{acknowledgement}\n\n"
                f"Saved to memory: {saved_text}"
                f"{followup}"
            ),
            action="soft_memory_capture",
            memory_updated=True,
            reply_keyboard=keyboard,
        )

    compare_prompt = _extract_compare_prompt(message)
    if compare_prompt is not None or natural_intent == "compare_help":
        prompt = compare_prompt or ""
        if not prompt or _is_too_vague_compare_prompt(prompt):
            return TelegramReply(
                message=(
                    "Want to see the difference your memory makes?\n\n"
                    "Say something like:\n"
                    "- Compare the normal version with mine: answer this client: Can we move the meeting to Friday?\n"
                    "- Compare the normal version with mine: recommend a laptop for me\n"
                    "- /compare Draft: write a founder update in my style\n\n"
                    "You can also ask naturally:\n"
                    "- Reply like me: can we move tomorrow's meeting\n"
                    "- What do you remember about me?"
                ),
                action="compare_help",
            )
        try:
            comparison = build_compare_output(user_id, prompt, model=model)
        except Exception as exc:  # pragma: no cover - exercised via webhook/service tests
            logger.warning("telegram_compare_backend_unavailable user_id=%s error=%s", user_id, exc)
            return TelegramReply(
                message="I couldn't run that comparison right now because the model backend is unavailable. Please try again in a moment.",
                action="compare_backend_unavailable",
            )
        photo_path = None
        if is_share_card_eligible(comparison):
            try:
                photo_path = build_compare_share_card(comparison, user_id=user_id)
            except Exception as exc:  # pragma: no cover - defensive: text compare must still work
                logger.warning("telegram_compare_card_unavailable user_id=%s error=%s", user_id, exc)
        else:
            _save_pending_compare_setup(user_id=user_id, prompt=prompt)
            return TelegramReply(
                message=_build_guided_compare_capture_message(prompt),
                action="compare_guided_capture",
                reply_keyboard=None,
            )
        return TelegramReply(
            message=comparison,
            action="compare_with_memory",
            reply_keyboard=UTILITY_KEYBOARD,
            photo_path=photo_path,
        )

    draft_prompt = _extract_draft_prompt(message)
    if draft_prompt is not None or natural_intent == "draft":
        prompt = draft_prompt or message
        try:
            reply = generate_memory_aware_draft(user_id, prompt, model=model)
        except Exception as exc:  # pragma: no cover - exercised via webhook/service tests
            logger.warning("telegram_draft_backend_unavailable user_id=%s error=%s", user_id, exc)
            return TelegramReply(
                message="I couldn't generate that draft right now because the model backend is unavailable. Please try again in a moment.",
                action="draft_backend_unavailable",
            )
        return TelegramReply(message=reply, action="draft_with_memory")

    if _is_ambiguous_preference_statement(message):
        return TelegramReply(
            message="Okay. I haven't saved that as a stable preference.",
            action="ambiguous_preference_ignored",
        )

    try:
        reply = generate_memory_aware_reply(user_id, message, model=model)
    except Exception as exc:  # pragma: no cover - exercised via webhook/service tests
        logger.warning("telegram_reply_backend_unavailable user_id=%s error=%s", user_id, exc)
        return TelegramReply(
            message="I couldn't generate a reply right now because the model backend is unavailable. Please try again in a moment.",
            action="reply_backend_unavailable",
        )
    return TelegramReply(message=reply, action="memory_aware_reply")


def _normalize_inbound_message(text: str) -> str:
    message = text.strip()
    message = re.sub(r"^\s*[-*]\s+(?=/)", "", message)
    message = re.sub(r"^\s*(?:user|me)\s*:\s*(?=/|compare:|draft|remember|correct|working on:)", "", message, flags=re.IGNORECASE)
    return message.strip()


def _save_pending_compare_setup(*, user_id: str, prompt: str) -> None:
    mem = get_memory_dir(user_id)
    payload = {"prompt": prompt.strip()}
    (mem / PENDING_COMPARE_SETUP_NAME).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_pending_compare_setup(user_id: str) -> dict[str, str] | None:
    path = get_memory_dir(user_id) / PENDING_COMPARE_SETUP_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    prompt = raw.get("prompt") if isinstance(raw, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        path.unlink(missing_ok=True)
        return None
    return {"prompt": prompt.strip()}


def _clear_pending_compare_setup(user_id: str) -> None:
    (get_memory_dir(user_id) / PENDING_COMPARE_SETUP_NAME).unlink(missing_ok=True)


def _save_pending_forget(user_id: str, query: str, file_name: str | None = None) -> None:
    mem = get_memory_dir(user_id)
    payload = {"query": query.strip()}
    if file_name:
        payload["file_name"] = file_name
    (mem / PENDING_FORGET_NAME).write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_pending_forget(user_id: str) -> dict[str, str] | None:
    path = get_memory_dir(user_id) / PENDING_FORGET_NAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    if not isinstance(raw, dict):
        path.unlink(missing_ok=True)
        return None
    query = raw.get("query")
    file_name = raw.get("file_name")
    if not isinstance(query, str) or not query.strip():
        path.unlink(missing_ok=True)
        return None
    result = {"query": query.strip()}
    if isinstance(file_name, str) and file_name.strip():
        result["file_name"] = file_name.strip()
    return result


def _clear_pending_forget(user_id: str) -> None:
    (get_memory_dir(user_id) / PENDING_FORGET_NAME).unlink(missing_ok=True)


def _try_handle_approval_message(*, user_id: str, message: str) -> TelegramReply | None:
    lowered = message.lower().strip()
    if lowered in {"/approvals", "approvals", "pending approvals"}:
        pending = list_approval_requests(user_id=user_id, status="pending")
        if not pending:
            return TelegramReply(
                message="No pending approvals.",
                action="approval_list_empty",
                reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
            )
        approval = pending[0]
        remaining = len(pending) - 1
        suffix = f"\n\n{remaining} more pending. Send /approvals again after resolving this one." if remaining else ""
        return TelegramReply(
            message=f"{build_telegram_approval_message(approval)}{suffix}",
            action="approval_list",
            reply_keyboard=_build_approval_keyboard(approval.approval_id),
        )

    decision: str | None = None
    approval_id = ""
    if lowered.startswith("approval:"):
        parts = message.split(":", 2)
        if len(parts) == 3 and parts[1] in {"approved", "denied"}:
            decision = parts[1]
            approval_id = parts[2].strip()
        elif len(parts) == 3 and parts[1] == "edit":
            decision = "denied"
            approval_id = parts[2].strip()
    elif lowered.startswith("/approve "):
        decision = "approved"
        approval_id = message.split(maxsplit=1)[1].strip()
    elif lowered.startswith("/deny "):
        decision = "denied"
        approval_id = message.split(maxsplit=1)[1].strip()

    if decision is None or not approval_id:
        return None

    approval, error = resolve_approval_request(
        approval_id,
        decision=decision,  # type: ignore[arg-type]
        user_id=user_id,
        resolved_by="telegram",
        reason="User requested edit" if lowered.startswith("approval:edit:") else None,
    )
    if approval is None:
        return TelegramReply(
            message="I couldn't find that approval request. It may have expired or been created for another user.",
            action="approval_not_found",
            remove_keyboard=True,
        )
    if error == "already_resolved":
        return TelegramReply(
            message=_build_approval_already_resolved_message(approval),
            action="approval_already_resolved",
            remove_keyboard=True,
        )
    if lowered.startswith("approval:edit:"):
        return TelegramReply(
            message=f"Edit requested: {_humanize_action_type(approval.action_type)}\n\nWhat would you like to change?",
            action="approval_edit_requested",
            remove_keyboard=True,
        )
    return TelegramReply(
        message=_build_approval_resolved_message(approval),
        action="approval_resolved",
        remove_keyboard=True,
    )


def _try_handle_memory_proposal_message(*, user_id: str, message: str) -> TelegramReply | None:
    parts = message.strip().split(":", 2)
    if len(parts) != 3 or parts[0] != "memory_proposal":
        return None
    if parts[1] not in {"save", "skip"} or not parts[2].strip():
        return None

    decision = "approved" if parts[1] == "save" else "denied"
    result = commit_memory_proposal(user_id=user_id, proposal_id=parts[2].strip(), decision=decision)
    status = result.get("status")
    if status == "written":
        _notify_memory_change("memory.created", user_id=user_id)
        return TelegramReply(
            message="Saved.",
            action="memory_proposal_saved",
            memory_updated=True,
            remove_keyboard=True,
        )
    if status == "denied":
        return TelegramReply(
            message="Skipped.",
            action="memory_proposal_skipped",
            remove_keyboard=True,
        )
    if status in {"duplicate", "conflict"}:
        return TelegramReply(
            message="Not saved. That memory now duplicates or conflicts with saved memory.",
            action="memory_proposal_rejected",
            remove_keyboard=True,
        )
    return TelegramReply(
        message="I couldn't resolve that memory candidate. It may already be handled or belong to another user.",
        action="memory_proposal_missing",
        remove_keyboard=True,
    )


def build_telegram_approval_message(approval: ApprovalRequest) -> str:
    lines = [
        "Approval needed",
        "",
        f"Action: {_humanize_action_type(approval.action_type)}",
        f"Request: {approval.summary}",
    ]
    details = _approval_payload_details(approval.payload)
    if details:
        lines.extend(["", "Details:", *details])
    lines.extend(
        [
            "",
            f"Expires: {_short_expiry(approval.expires_at)}",
            "",
            "Approve only if this should run outside 8mem.",
        ]
    )
    return "\n".join(lines)


def _build_approval_keyboard(approval_id: str) -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "Approve", "callback_data": f"approval:approved:{approval_id}"},
            {"text": "Deny", "callback_data": f"approval:denied:{approval_id}"},
            {"text": "Edit", "callback_data": f"approval:edit:{approval_id}"},
        ],
        [{"text": "Show pending approvals", "callback_data": "/approvals"}],
    ]


def _build_approval_resolved_message(approval: ApprovalRequest) -> str:
    if approval.status == "approved":
        return f"Approved: {_humanize_action_type(approval.action_type)}\n\nThe runtime can continue now."
    return f"Denied: {_humanize_action_type(approval.action_type)}\n\nThe runtime must not run this action."


def _build_approval_already_resolved_message(approval: ApprovalRequest) -> str:
    return (
        f"This approval is already {approval.status}.\n\n"
        f"Action: {_humanize_action_type(approval.action_type)}\n"
        f"Request: {approval.summary}"
    )


def _humanize_action_type(action_type: str) -> str:
    labels = {
        "email_send": "Send email",
        "calendar_write": "Update calendar",
        "calendar_create": "Create calendar event",
        "public_post": "Publish public post",
        "external_delete": "Delete external data",
        "memory_write": "Write memory",
        "shell_command": "Run shell command",
    }
    return labels.get(action_type, action_type.replace("_", " ").strip().title())


def _approval_payload_details(payload: dict[str, object]) -> list[str]:
    detail_keys = ["to", "subject", "when", "title", "url", "command", "file", "reason"]
    details: list[str] = []
    for key in detail_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            label = key.replace("_", " ").title()
            details.append(f"- {label}: {value.strip()}")
        if len(details) >= 5:
            break
    return details


def _short_expiry(expires_at: str) -> str:
    return expires_at.replace("T", " ").split("+", 1)[0]


def _detect_correction_update(message: str) -> tuple[str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower()
    if lowered.startswith("correct"):
        return None
    if not _starts_with_correction_trigger(lowered):
        return None
    replacement = _extract_correction_replacement(stripped)
    if not replacement:
        return "CORRECTIONS.md", ""
    if lowered.startswith("actually") and not _looks_like_preference_fact(replacement):
        return None
    if _looks_like_preference_fact(replacement):
        return "PREFERENCES.md", normalize_preference_text(replacement)
    return "CORRECTIONS.md", normalize_correction_text(replacement)


def _starts_with_correction_trigger(lowered: str) -> bool:
    stripped = lowered.strip()
    return stripped.startswith(
        (
            "that's wrong",
            "that is wrong",
            "that's not right",
            "that is not right",
            "not true",
            "wrong,",
            "wrong.",
            "wrong:",
            "actually,",
            "actually ",
            "no i don't",
            "no i do not",
            "update this",
            "fix that",
        )
    )


def _extract_correction_replacement(message: str) -> str:
    patterns = [
        r"(?:that's wrong|that is wrong|that's not right|that is not right|not true|wrong)\s*[.,:-]?\s*(.+)$",
        r"(?:actually|update this|fix that)\s*[.,:-]?\s*(.+)$",
        r"no i (?:don't|do not)\s*[.,:-]?\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def _looks_like_preference_fact(value: str) -> bool:
    lowered = value.lower().strip()
    return lowered.startswith(
        (
            "i prefer ",
            "i like ",
            "i want ",
            "i value ",
            "i care about ",
            "keep ",
            "give ",
            "use ",
            "write in ",
            "respond in ",
            "avoid ",
        )
    )


def _build_trusted_change_confirmation(event: dict[str, str | None]) -> str:
    new_value = str(event.get("new_value") or "").strip()
    old_value = event.get("old_value")
    category = event.get("category") or "memory"
    if not new_value:
        return "What should I replace it with?"
    if old_value:
        old_display = str(old_value).rstrip(".")
        new_display = new_value.rstrip(".")
        return f'Updated {category}: "{old_display}" -> "{new_display}". I\'ll use this going forward.'
    return f"Updated. I'll use this going forward: {new_value}"


def _build_contradiction_prompt(old_value: str, new_value: str) -> str:
    return (
        "Heads up — that conflicts with memory I already have.\n\n"
        f'Previous: "{old_value}"\n'
        f'New: "{new_value}"\n\n'
        "Which is current?"
    )


def _try_resolve_pending_contradiction(*, user_id: str, message: str) -> TelegramReply | None:
    pending = load_pending_contradiction(user_id)
    if not pending:
        return None
    normalized = _normalize_intent_text(message)
    lowered = message.lower().strip()
    use_new = any(phrase in normalized for phrase in {"use new", "new one", "changed recently", "current", "morning now"})
    keep_old = any(phrase in normalized for phrase in {"keep old", "old one", "still true", "keep previous"})
    both_true = "both" in normalized and "true" in normalized
    review_later = lowered == "review later" or "ignore" in normalized

    if review_later:
        clear_pending_contradiction(user_id)
        return TelegramReply(
            message="Okay. I won't change your memory yet.",
            action="defer_memory_conflict",
            remove_keyboard=True,
        )
    if keep_old:
        clear_pending_contradiction(user_id)
        old_value = pending["old_value"].rstrip(".")
        return TelegramReply(
            message=f'Kept the existing memory: "{old_value}".',
            action="resolve_memory_conflict",
            remove_keyboard=True,
        )
    if both_true:
        updates = {
            pending["file_name"]: {
                _contextualize_both_true_value(pending["old_value"]),
                _contextualize_both_true_value(pending["new_value"]),
            }
        }
        apply_memory_updates(updates, user_id=user_id)
        _notify_memory_change("memory.corrected", user_id=user_id)
        clear_pending_contradiction(user_id)
        return TelegramReply(
            message=(
                "Got it. I'll keep both with context: "
                f"{pending['old_value']} / {pending['new_value']}"
            ),
            action="resolve_memory_conflict",
            memory_updated=True,
            remove_keyboard=True,
        )
    if use_new:
        event = apply_trusted_memory_change(
            user_id=user_id,
            file_name=pending["file_name"],
            new_value=pending["new_value"],
            trigger="contradiction",
            source_message=pending["source_message"],
            old_value=pending["old_value"],
            category=pending.get("category") or infer_memory_category(pending["new_value"]),
        )
        _notify_memory_change("memory.corrected", user_id=user_id)
        clear_pending_contradiction(user_id)
        return TelegramReply(
            message=f"{_build_trusted_change_confirmation(event)} The previous value was archived with a timestamp.",
            action="resolve_memory_conflict",
            memory_updated=True,
            remove_keyboard=True,
        )
    return None


def _contextualize_both_true_value(value: str) -> str:
    return normalize_preference_text(value)


def _is_resolution_phrase_without_pending(lowered: str) -> bool:
    normalized = _normalize_intent_text(lowered)
    return normalized in {
        "use new",
        "keep old",
        "both are true",
        "both true",
        "new one",
        "old one",
        "changed recently",
    }


def _dedupe_batch_conflict_prompts(replies: list[TelegramReply]) -> list[TelegramReply]:
    marker = "\n\nThese two memory notes conflict:\n"
    seen_blocks: set[str] = set()
    deduped: list[TelegramReply] = []
    for reply in replies:
        message = reply.message
        if marker in message:
            prefix, suffix = message.split(marker, 1)
            conflict_block = marker + suffix
            if conflict_block in seen_blocks:
                message = prefix.rstrip()
            else:
                seen_blocks.add(conflict_block)
        deduped.append(
            TelegramReply(
                message=message,
                action=reply.action,
                memory_updated=reply.memory_updated,
                reply_keyboard=reply.reply_keyboard,
                remove_keyboard=reply.remove_keyboard,
                photo_path=reply.photo_path,
            )
        )
    return deduped


def _dedupe_batch_messages(replies: list[TelegramReply]) -> list[TelegramReply]:
    deduped: list[TelegramReply] = []
    seen_messages: set[str] = set()
    for reply in replies:
        normalized = reply.message.strip()
        if normalized in seen_messages:
            continue
        seen_messages.add(normalized)
        deduped.append(reply)
    return deduped


def _get_onboarding_kind(message: str) -> str | None:
    normalized = _normalize_intent_text(message).strip()
    if normalized in {"/start", "start", "hello", "hi", "hey"}:
        return "start"
    if normalized in {
        "context",
        "/context",
        "setup",
        "/setup",
        "context setup",
        "setup context",
        "memory setup",
        "teach 8mem",
        "teach me",
    }:
        return "context_setup"
    if normalized in {"compare setup", "/compare setup", "setup compare", "make compare card", "make share card"}:
        return "compare_setup"
    if normalized in {"help", "what can you do", "how do i use this", "how do i use you", "what do i do here"}:
        return "help"
    return None


def _build_meta_reply(message: str, *, user_id: str | None, model: str | None = None) -> TelegramReply | None:
    normalized = _normalize_intent_text(message).strip()
    lowered = message.lower().strip()

    if _looks_like_product_about_intent(lowered, normalized):
        return TelegramReply(
            message=(
                "8mem is a memory layer for AI. It remembers your preferences and corrections, "
                "shows what it is using, and lets you correct memory when it gets something wrong."
            ),
            action="meta_about_8mem",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if _looks_like_memory_layer_intent(lowered, normalized):
        return TelegramReply(
            message=(
                "Yes. I'm the memory layer using your saved preferences and corrections to shape replies."
            ),
            action="meta_memory_layer",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if _looks_like_model_intent(lowered, normalized):
        active_model = resolve_default_model(model)
        return TelegramReply(
            message=f"Right now I'm using {active_model} through Ollama.",
            action="meta_model_info",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if _looks_like_passport_explanation_intent(lowered, normalized):
        return TelegramReply(
            message=(
                "/passport shows the memory I'm currently using for you, so you can inspect it and correct it."
            ),
            action="meta_passport_info",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    if _looks_like_compare_explanation_intent(lowered, normalized):
        return TelegramReply(
            message=(
                "/compare shows a standard answer next to one shaped by your memory, so you can see whether memory is actually helping."
            ),
            action="meta_compare_info",
            reply_keyboard=UTILITY_KEYBOARD if _has_saved_memory(user_id) else None,
        )

    return None


def _has_saved_memory(user_id: str | None) -> bool:
    sections = get_retrieval_sections(user_id)
    return any(items for items in sections.values())


def _build_onboarding_message(user_id: str | None, *, kind: str) -> str:
    if kind == "compare_setup":
        return _build_compare_setup_message()

    if kind == "context_setup":
        return (
            "Send one message in this format and I'll save the important context:\n\n"
            "Name: <your name>\n"
            "Timezone: <your timezone>\n"
            "Working on: <what you want assistants to know>\n"
            "Assistants should know: <important project context>\n"
            "Do not assume: <common mistake to avoid>\n"
            "Communication style: <how you want replies formatted>\n\n"
            "This becomes the context other AI runtimes receive through /v1/context."
        )

    if kind == "help":
        if _has_saved_memory(user_id):
            return (
                "You can ask normally and I'll use the memory I already have for you.\n\n"
                "Try:\n"
                "- Reply like me: can we move tomorrow's meeting\n"
                "- What do you remember about me?\n"
                "- Show me the difference with memory\n\n"
                "If you want more control, /passport shows the memory I'm using and /compare shows the difference it makes."
            )
        return (
            "You can talk to me normally. I can remember preferences and use them in replies.\n\n"
            "Try:\n"
            "- Keep my work replies concise\n"
            "- Don't use emojis in client messages\n"
            "- Reply like me: can we move tomorrow's meeting\n\n"
            "You can also ask: What do you remember about me?"
        )

    if _has_saved_memory(user_id):
        return (
            "You can talk to me normally. I can remember preferences, use them in replies, and show you what I'm using.\n\n"
            "Try:\n"
            "- Keep my work replies concise\n"
            "- Don't use emojis in client messages\n"
            "- Reply like me: can we move tomorrow's meeting\n"
            "- What do you remember about me?\n\n"
            "If you want more control, /passport shows the memory I'm using and /compare shows the difference it makes."
        )
    return (
        "You can talk to me normally. I can remember how you like replies and use that when I draft or answer for you.\n\n"
        "Try:\n"
        "- Keep my work replies concise\n"
        "- Don't use emojis in client messages\n"
        "- Reply like me: can we move tomorrow's meeting\n"
        "- What do you remember about me?\n\n"
        "You can also use /passport to see the memory I'm using and /compare to see the difference it makes."
    )


def _build_compare_setup_message() -> str:
    return (
        "To make a strong 8mem Compare card, paste or edit this as your next message:\n\n"
        "Working on: 8mem, an AI memory layer for visible, correctable, portable memory.\n"
        "Assistants should know: I care about execution speed, but not at the cost of stability.\n"
        "Do not assume: I want generic motivational language.\n"
        "Communication style: short, direct, structured as Progress / Risk / Next.\n\n"
        "After that, run:\n"
        "/compare Draft: write a founder update in my style"
    )


def _build_guided_compare_capture_message(prompt: str) -> str:
    return (
        "I need a little more memory before this is worth turning into a share card.\n\n"
        f"I saved your compare request: {prompt}\n\n"
        "Reply once with this filled in, and I'll rerun the compare automatically:\n\n"
        "Working on: 8mem, an AI memory layer for visible, correctable, portable memory.\n"
        "Assistants should know: I care about execution speed, but not at the cost of stability.\n"
        "Do not assume: I want generic motivational language.\n"
        "Communication style: short, direct, structured as Progress / Risk / Next."
    )


def _save_context_and_rerun_pending_compare(
    *,
    user_id: str,
    updates: dict[str, set[str]],
    pending_compare: dict[str, str],
    model: str | None,
) -> TelegramReply:
    saved = _save_context_interview_submission(user_id=user_id, updates=updates)
    prompt = pending_compare["prompt"]
    _clear_pending_compare_setup(user_id)
    try:
        comparison = build_compare_output(user_id, prompt, model=model)
    except Exception as exc:  # pragma: no cover - exercised via webhook/service tests
        logger.warning("telegram_guided_compare_backend_unavailable user_id=%s error=%s", user_id, exc)
        return TelegramReply(
            message=(
                f"{saved.message}\n\n"
                "I saved the context, but I couldn't rerun the comparison because the model backend is unavailable. "
                f"Try again with: /compare {prompt}"
            ),
            action="compare_guided_capture_saved_backend_unavailable",
            memory_updated=True,
            reply_keyboard=UTILITY_KEYBOARD,
        )

    photo_path = None
    if is_share_card_eligible(comparison):
        try:
            photo_path = build_compare_share_card(comparison, user_id=user_id)
        except Exception as exc:  # pragma: no cover - defensive: text compare must still work
            logger.warning("telegram_guided_compare_card_unavailable user_id=%s error=%s", user_id, exc)

    if photo_path is None:
        comparison += "\n\nNo share card generated yet because the memory-shaped answer is still not meaningfully different enough."

    return TelegramReply(
        message=f"{saved.message}\n\nRerunning your compare now.\n\n{comparison}",
        action="compare_guided_capture_complete",
        memory_updated=True,
        reply_keyboard=UTILITY_KEYBOARD,
        photo_path=photo_path,
    )


def _strip_prefix(text: str, prefix: str) -> str:
    stripped = text[len(prefix) :].strip()
    return stripped.lstrip(": ").strip()


def _strip_compare_prefix(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("/compare"):
        return text[len("/compare") :].strip()
    if lowered.startswith("compare:"):
        return text[len("compare:") :].strip()
    return text.strip()


def _extract_compare_prompt(text: str) -> str | None:
    lowered = text.lower().strip()
    if lowered.startswith("/compare") or lowered.startswith("compare:"):
        return _strip_compare_prefix(text)

    normalized = _normalize_intent_text(text)
    if any(
        phrase in normalized
        for phrase in {
            "compare the normal version with mine",
            "show me the difference with memory",
            "compare generic vs personalized",
            "show me the normal version vs mine",
        }
    ):
        if ":" in text:
            return text.split(":", 1)[1].strip()
        return ""
    return None


def _extract_forget_query(text: str) -> str | None:
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in ("/forget", "forget memory", "forget"):
        if lowered == prefix:
            return None
        if lowered.startswith(prefix + " "):
            query = stripped[len(prefix) :].strip(" :")
            return query or None
    return None


def _extract_draft_prompt(text: str) -> str | None:
    lowered = text.lower().strip()
    if lowered.startswith("draft"):
        return _strip_prefix(text, "draft")

    direct_patterns = [
        r"^(?:can you\s+)?(?:draft|write|answer|reply)(?:\s+this)?\s+(?:like me|in my style|the way i usually talk)\s*[:,-]?\s*(.+)$",
        r"^(?:can you\s+)?help me (?:answer|reply to)\s+(?:this|the client)\s+(?:like me|in my style)\s*[:,-]?\s*(.+)$",
    ]
    for pattern in direct_patterns:
        match = re.match(pattern, text.strip(), flags=re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def _is_too_vague_compare_prompt(prompt: str) -> bool:
    lowered = prompt.lower().strip().rstrip("?.!")
    vague_prompts = {
        "",
        "draft",
        "reply",
        "reply answer this client in my style",
        "reply: answer this client in my style",
        "summarize",
        "summary",
        "founder update",
        "update",
        "recommend",
        "recommendation",
        "laptop",
    }
    return lowered in vague_prompts


def _detect_natural_memory_capture(message: str) -> tuple[str, str, str] | None:
    stripped = message.strip()
    lowered = stripped.lower().strip(" .!?")
    if not stripped or "?" in stripped:
        return None

    correction_match = re.match(r"^(?:please\s+)?(?:don't|do not)\s+(.+)$", lowered, flags=re.IGNORECASE)
    if correction_match:
        body = stripped
        if lowered.startswith("please "):
            body = stripped[7:].strip()
        normalized = normalize_correction_text(body)
        if normalized:
            return "CORRECTIONS.md", normalized, _acknowledge_correction(normalized)

    preference_patterns = [
        (r"^(?:please\s+)?keep\s+(.+)$", "keep"),
        (r"^(?:please\s+)?give\s+(.+)$", "give"),
        (r"^(?:please\s+)?use\s+(.+)$", "use"),
        (r"^(?:please\s+)?write in\s+(.+)$", "write_in"),
        (r"^(?:please\s+)?respond in\s+(.+)$", "respond_in"),
        (r"^(?:please\s+)?avoid\s+(.+)$", "avoid"),
        (r"^i prefer\s+(.+)$", "prefer"),
        (r"^i like\s+(.+)$", "prefer"),
        (r"^i want\s+(.+)$", "prefer"),
        (r"^i value\s+(.+)$", "value"),
        (r"^i care about\s+(.+)$", "value"),
    ]
    for pattern, kind in preference_patterns:
        match = re.match(pattern, lowered, flags=re.IGNORECASE)
        if not match:
            continue
        body = stripped
        if kind in {"keep", "give", "use", "write_in", "respond_in", "avoid"} and lowered.startswith("please "):
            body = stripped[7:].strip()
        normalized = normalize_preference_text(body)
        if normalized:
            return "PREFERENCES.md", normalized, _acknowledge_preference(normalized)
    return None


def _detect_context_interview_submission(message: str) -> dict[str, set[str]] | None:
    fields = _parse_context_interview_fields(message)
    if not fields:
        return None

    updates: dict[str, set[str]] = {}

    def add(file_name: str, value: str) -> None:
        cleaned = _normalize_context_fact(value)
        if cleaned:
            updates.setdefault(file_name, set()).add(cleaned)

    for label, value in fields:
        if label in {"name", "display name"}:
            add("IDENTITY.md", f"Name: {value}")
        elif label in {"timezone", "time zone"}:
            add("IDENTITY.md", f"Timezone: {value}")
        elif label in {"working on", "building", "project", "current project", "focus"}:
            add("BELIEFS.md", value)
        elif label in {"assistants should know", "ai should know", "important context", "project context", "known fact"}:
            add("BELIEFS.md", value)
        elif label in {"do not assume", "don't assume", "never assume"}:
            add("CORRECTIONS.md", f"Do not assume {value}")
        elif label in {"communication style", "reply style", "response style", "style"}:
            normalized = normalize_preference_text(value)
            if normalized:
                updates.setdefault("PREFERENCES.md", set()).add(normalized)

    return updates or None


def _parse_context_interview_fields(message: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    valid_labels = {
        "name",
        "display name",
        "timezone",
        "time zone",
        "working on",
        "building",
        "project",
        "current project",
        "focus",
        "assistants should know",
        "ai should know",
        "important context",
        "project context",
        "known fact",
        "do not assume",
        "don't assume",
        "never assume",
        "communication style",
        "reply style",
        "response style",
        "style",
    }
    for raw_line in message.splitlines():
        line = raw_line.strip().strip("-* ")
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        normalized_label = _normalize_intent_text(label).strip()
        value = value.strip()
        if normalized_label in valid_labels and value:
            fields.append((normalized_label, value))
    return fields if len(fields) >= 2 else []


def _normalize_context_fact(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        return ""
    return cleaned.rstrip(".") + "."


def _save_context_interview_submission(*, user_id: str, updates: dict[str, set[str]]) -> TelegramReply:
    apply_memory_updates(updates, user_id=user_id)
    _notify_memory_change("memory.created", user_id=user_id)
    saved_lines: list[str] = []
    labels = {
        "IDENTITY.md": "Identity",
        "BELIEFS.md": "Project context",
        "PREFERENCES.md": "Communication style",
        "CORRECTIONS.md": "Guardrail",
    }
    for file_name in ("IDENTITY.md", "BELIEFS.md", "PREFERENCES.md", "CORRECTIONS.md"):
        for item in sorted(updates.get(file_name, set())):
            saved_lines.append(f"- {labels[file_name]}: {item}")
    return TelegramReply(
        message=(
            "Context captured.\n\n"
            "Saved:\n"
            f"{chr(10).join(saved_lines)}\n\n"
            "This will now shape /passport, /compare, and /v1/context."
        ),
        action="context_interview_saved",
        memory_updated=True,
        reply_keyboard=UTILITY_KEYBOARD,
    )


def _is_ambiguous_preference_statement(message: str) -> bool:
    normalized = _normalize_intent_text(message).strip()
    if not normalized or "?" in message:
        return False
    ambiguous_starts = (
        "sometimes i like ",
        "sometimes i prefer ",
        "i sometimes like ",
        "i sometimes prefer ",
        "at times i like ",
        "at times i prefer ",
    )
    return normalized.startswith(ambiguous_starts)


def _acknowledge_preference(normalized_text: str) -> str:
    lowered = normalized_text.lower().rstrip(".")
    if lowered.startswith("keep "):
        return f"I’ll {lowered}."
    if lowered.startswith("avoid "):
        return f"I’ll {lowered}."
    if lowered.startswith("use "):
        return f"I’ll {lowered}."
    if lowered.startswith("write in "):
        return f"I’ll {lowered}."
    if lowered.startswith("respond in "):
        return f"I’ll {lowered}."
    if lowered.startswith("prefers "):
        return f"I’ll keep in mind that you {lowered}."
    if lowered.startswith("values "):
        return f"I’ll keep in mind that you {lowered}."
    return "I’ll remember that."


def _acknowledge_correction(normalized_text: str) -> str:
    lowered = normalized_text.lower().rstrip(".")
    if lowered.startswith("do not "):
        return f"I won’t {lowered[7:]}."
    if lowered.startswith("don't "):
        return f"I won’t {lowered[6:]}."
    if lowered.startswith("use "):
        return f"I’ll use {normalized_text[4:-1].strip()}."
    return "I’ll correct for that."


def _route_natural_intent(message: str, *, model: str | None = None) -> str | None:
    normalized = _normalize_intent_text(message)

    if _is_recall_request(normalized):
        return "recall"
    if normalized in {
        "what do you remember about me",
        "show my memory",
        "how do you know me",
        "what have you learned about me",
    }:
        return "recall"
    if normalized in {
        "what are you using about me",
        "what are you using for me",
        "what are you using right now",
        "what are you using right now about me",
        "show me what youre using about me",
        "show me what you are using about me",
    }:
        return "passport"
    if normalized in {
        "what changed about me",
        "what changed recently",
        "what changed in my memory",
        "what changed about how i like responses",
        "show me what changed",
    }:
        return "changes"
    if normalized in {
        "corrections",
        "show corrections",
        "show my corrections",
        "what corrections",
        "what have i corrected",
        "what did i correct",
    }:
        return "corrections"
    if _extract_compare_prompt(message) is not None:
        return "compare_help" if not _extract_compare_prompt(message) else None
    if _extract_draft_prompt(message) is not None:
        return "draft"

    if _looks_like_recall_intent(normalized):
        return "recall"
    if _looks_like_passport_intent(normalized):
        return "passport"
    if _looks_like_changes_intent(normalized):
        return "changes"
    if _looks_like_compare_intent(normalized):
        return "compare_help"
    if _looks_like_draft_intent(normalized):
        return "draft"

    return _classify_llm_intent(message, model=model)


def _normalize_intent_text(text: str) -> str:
    normalized = text.lower()
    replacements = {
        "u": "you",
        "ur": "your",
        "abt": "about",
        "wht": "what",
        "wat": "what",
        "knw": "know",
        "rember": "remember",
        "rembr": "remember",
        "dont": "don't",
        "pls": "please",
    }
    words = re.findall(r"[a-zA-Z']+", normalized)
    expanded = [replacements.get(word, word) for word in words]
    return " ".join(expanded)


def _looks_like_recall_intent(normalized: str) -> bool:
    return (
        "what" in normalized
        and "me" in normalized
        and any(word in normalized for word in {"know", "remember", "learned"})
    )


def _looks_like_passport_intent(normalized: str) -> bool:
    return (
        ("using" in normalized or "use" in normalized)
        and "me" in normalized
        and any(word in normalized for word in {"what", "show"})
    )


def _looks_like_product_about_intent(lowered: str, normalized: str) -> bool:
    return (
        "8mem" in lowered
        and any(phrase in normalized for phrase in {"what do you know about", "what is", "tell me about"})
    )


def _looks_like_memory_layer_intent(lowered: str, normalized: str) -> bool:
    return any(
        phrase in normalized or phrase in lowered
        for phrase in {
            "are you memory layer",
            "are you a memory layer",
            "are you the memory layer",
            "are you memory",
        }
    )


def _looks_like_model_intent(lowered: str, normalized: str) -> bool:
    return (
        "model" in normalized
        and any(phrase in normalized for phrase in {"which model", "what model", "what model are you running", "which model are you using"})
    )


def _looks_like_passport_explanation_intent(lowered: str, normalized: str) -> bool:
    return (
        "/passport" in lowered
        and any(phrase in normalized for phrase in {"what is", "what's", "what does"})
    ) or normalized in {"passport meaning", "passport here"}


def _looks_like_compare_explanation_intent(lowered: str, normalized: str) -> bool:
    return (
        "/compare" in lowered
        and any(phrase in normalized for phrase in {"what is", "what's", "what does"})
    ) or normalized in {"compare meaning", "compare here"}


def _looks_like_compare_intent(normalized: str) -> bool:
    return (
        (
            any(word in normalized for word in {"compare", "difference", "diff"})
            and any(word in normalized for word in {"memory", "mine", "style", "personalized"})
        )
        or ("normal version" in normalized and "mine" in normalized)
    )


def _looks_like_changes_intent(normalized: str) -> bool:
    return (
        "changed" in normalized
        and any(word in normalized for word in {"me", "memory", "responses"})
    )


def _looks_like_draft_intent(normalized: str) -> bool:
    return (
        any(word in normalized for word in {"draft", "reply", "answer", "write"})
        and any(phrase in normalized for phrase in {"like me", "my style", "the way i usually talk"})
    )


def _classify_llm_intent(message: str, *, model: str | None = None) -> str | None:
    if not message or len(message) > 220:
        return None
    normalized = _normalize_intent_text(message)
    strong_cues = {
        "remember",
        "memory",
        "style",
        "compare",
        "difference",
        "draft",
        "personalized",
    }
    if any(word in normalized for word in strong_cues):
        pass
    elif "know" in normalized and "me" in normalized:
        pass
    elif "using" in normalized and "me" in normalized:
        pass
    elif "changed" in normalized and any(word in normalized for word in {"me", "memory"}):
        pass
    elif any(word in normalized for word in {"reply", "answer", "write"}) and any(
        phrase in normalized for phrase in {"like me", "my style", "mine"}
    ):
        pass
    else:
        return None
    prompt = (
        "Classify the user message into one label only: recall, passport, changes, compare_help, draft, none.\n"
        "Use recall for asking what you know or remember about the user.\n"
        "Use passport for asking what memory you are currently using.\n"
        "Use changes for asking what changed recently in the user's memory or preferences.\n"
        "Use compare_help for asking to compare a normal answer with a personalized one.\n"
        "Use draft for asking for a reply or draft in the user's style.\n"
        "Use none if the message does not clearly match one of those intents.\n\n"
        f"Message: {message}\n"
        "Label:"
    )
    try:
        result = generate_text(model=resolve_default_model(model), prompt=prompt, timeout_seconds=20)
    except Exception:
        return None
    label = result.text.strip().lower()
    if label in {"recall", "passport", "changes", "compare_help", "draft", "none"}:
        return None if label == "none" else label
    return None


def _is_recall_request(lowered: str) -> bool:
    normalized = " ".join(lowered.strip().replace("?", " ").replace(":", " ").split())
    return normalized in {
        "what do you know about me",
        "wht do you know about me",
        "what do u know about me",
        "wht do u know about me",
        "what do you know abt me",
        "what do you know about how i like responses",
        "recall",
        "recall what do you know about me",
    }


def _is_supported_command(lowered: str) -> bool:
    return (
        lowered.startswith("remember")
        or lowered.startswith("correct")
        or lowered.startswith("draft")
        or lowered.startswith("/compare")
        or lowered in {"/corrections", "corrections"}
        or lowered.startswith("compare:")
        or lowered in {"/passport", "passport"}
        or lowered in {"reset memory", "forget everything", "/resetmemory"}
        or lowered in {"undo", "undo last memory change", "undo memory"}
        or lowered == "review later"
        or _match_telegram_resolution_command(lowered) is not None
        or _is_recall_request(lowered)
    )


def _is_processable_batch_line(text: str) -> bool:
    lowered = text.lower().strip()
    return (
        _is_supported_command(lowered)
        or _detect_natural_memory_capture(text) is not None
        or _extract_compare_prompt(text) is not None
        or _extract_draft_prompt(text) is not None
        or _route_natural_intent(text) in {"recall", "passport", "changes", "compare_help", "draft"}
        or _route_natural_intent(text) == "corrections"
    )


def _is_ignorable_heading(lowered: str) -> bool:
    return (
        lowered.startswith("sequence ")
        or lowered.startswith("test ")
        or lowered.startswith("case ")
        or lowered.startswith("#")
    )


def _build_recall_summary(user_id: str) -> str:
    sections = get_retrieval_sections(user_id)
    structured = build_structured_memory_summary(user_id)
    lines: list[str] = ["Here's what I know about you:"]

    preferences = sections.get("PREFERENCES.md", [])
    corrections = sections.get("CORRECTIONS.md", [])
    identity = sections.get("IDENTITY.md", [])

    if identity:
        lines.append(f"- Identity: {identity[0]}")
    if isinstance(structured, dict):
        for item in structured.get("highlights", [])[:2]:
            lines.append(f"- Right now: {item}")
    if preferences:
        for item in preferences[:3]:
            lines.append(f"- Preference: {item}")
    if corrections:
        for item in corrections[:2]:
            lines.append(f"- Correction: {item}")

    tensions = get_structured_tensions(user_id)
    for item in tensions[:2]:
        lines.append(f"- Possible conflict: {item}")

    if len(lines) == 1:
        lines.append("- I don't have saved memory for you yet.")
    return "\n".join(lines)


def _match_telegram_resolution_command(lowered: str) -> tuple[str, str, str] | None:
    command = lowered.strip()
    mapping = {
        "keep no emojis": ("emoji_guidance", "keep_correction", "Resolved. I'll avoid emojis by default."),
        "keep emojis": ("emoji_guidance", "keep_preference", "Resolved. I'll allow emojis by default."),
        "keep concise": ("detail_conflict", "keep_concise", "Resolved. I'll keep concise responses as the default."),
        "keep detailed": ("detail_conflict", "keep_detailed", "Resolved. I'll keep detailed responses as the default."),
        "keep bullets": ("format_conflict", "keep_bullets", "Resolved. I'll keep structured bullets as the default."),
        "keep long-form": ("format_conflict", "keep_long_form", "Resolved. I'll keep long-form writing as the default."),
        "keep long form": ("format_conflict", "keep_long_form", "Resolved. I'll keep long-form writing as the default."),
    }
    return mapping.get(command)


def _build_telegram_resolution_prompt(user_id: str) -> str:
    items = get_memory_resolution_suggestions(user_id=user_id)
    if not items:
        return ""

    item = items[0]
    if item["id"] == "emoji_guidance":
        return (
            "\n\nThese two memory notes conflict:\n"
            "- Use emojis.\n"
            "- do not use emojis\n\n"
            "Choose one:\n"
            "- Keep no emojis\n"
            "- Keep emojis\n"
            "- Review later\n\n"
            "Tip: /passport shows the memory I'm using. /compare shows the difference."
        )
    if item["id"] == "detail_conflict":
        return (
            "\n\nThese two memory notes conflict:\n"
            "- Keep answers concise.\n"
            "- Give detailed explanations.\n\n"
            "Choose one:\n"
            "- Keep concise\n"
            "- Keep detailed\n"
            "- Review later\n\n"
            "Tip: /passport shows the memory I'm using. /compare shows the difference."
        )
    if item["id"] == "format_conflict":
        return (
            "\n\nThese two memory notes conflict:\n"
            "- Structured bullets.\n"
            "- Long-form writing.\n\n"
            "Choose one:\n"
            "- Keep bullets\n"
            "- Keep long-form\n"
            "- Review later\n\n"
            "Tip: /passport shows the memory I'm using. /compare shows the difference."
        )
    return ""


def build_telegram_reply_keyboard(user_id: str) -> list[list[str]] | None:
    items = get_memory_resolution_suggestions(user_id=user_id)
    if not items:
        return None

    item = items[0]
    if item["id"] == "emoji_guidance":
        return [["Keep no emojis", "Keep emojis"], ["Review later"], ["/passport", "/compare"]]
    if item["id"] == "detail_conflict":
        return [["Keep concise", "Keep detailed"], ["Review later"], ["/passport", "/compare"]]
    if item["id"] == "format_conflict":
        return [["Keep bullets", "Keep long-form"], ["Review later"], ["/passport", "/compare"]]
    return None

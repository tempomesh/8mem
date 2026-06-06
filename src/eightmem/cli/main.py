from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import typer
import uvicorn

from eightmem import __version__
from eightmem.channels.telegram_adapter import (
    TelegramConfigError,
    TelegramSendError,
    get_telegram_webhook_info,
    load_telegram_config,
    normalize_telegram_webhook_url,
    set_telegram_webhook,
)
from eightmem.core.context_export import compile_context
from eightmem.core.env import load_project_env, remove_runtime_env_keys, runtime_env_path, write_runtime_env
from eightmem.core.governance import ensure_governance_policy, evaluate_action
from eightmem.core.heartbeat import run_heartbeat
from eightmem.core.ingestion import (
    build_candidate_review,
    build_import_preview,
    prepare_import_candidates,
    prepare_import_document,
)
from eightmem.core.paths import ensure_runtime_dirs, runtime_home
from eightmem.core.pipeline import analyze_chat_export
from eightmem.core.templates import copy_default_templates
from eightmem.llm.ollama import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OllamaError,
    check_backend as check_ollama_backend,
    generate_text,
    resolve_base_url,
    resolve_default_model,
)
from eightmem.mirror.summary import build_mirror_text
from eightmem.services.memory_service import (
    apply_memory_dedup,
    apply_memory_updates,
    build_engram_context,
    find_forget_candidates,
    forget_memory_entries,
    preview_memory_dedup,
    get_structured_tensions,
)
from eightmem.services.webhook_service import build_signed_request, list_connectors, register_connector
from eightmem.core.sqlite_facts import sqlite_vec_available
from eightmem.ui.app import create_app

app = typer.Typer(help="8mem: AI forgets. 8mem remembers.")


def _pid_file() -> Path:
    return runtime_home() / "8mem.pid"


def _log_file() -> Path:
    return runtime_home() / "8mem.log"


SYSTEMD_SERVICE_NAME = "8mem.service"
OPENCLAW_GATEWAY_SERVICE_NAME = "openclaw-gateway"
OPENCLAW_BLOCK_NAME = "8mem-openclaw-integration"
OPENCLAW_BLOCK_START = f"<!-- 8mem managed block: {OPENCLAW_BLOCK_NAME} -->"
OPENCLAW_BLOCK_END = f"<!-- /8mem managed block: {OPENCLAW_BLOCK_NAME} -->"
OPENCLAW_MANIFEST_NAME = "openclaw-install-manifest.json"
OPENCLAW_BOOTSTRAP_FILE_CHAR_LIMIT = 12_000
OPENCLAW_BOOTSTRAP_FILE_SAFE_LIMIT = int(OPENCLAW_BOOTSTRAP_FILE_CHAR_LIMIT * 0.8)
OPENCLAW_COMMANDS_FILE_NAME = "8MEM-COMMANDS.md"
OPENCLAW_CARD_FILE_NAME = "8mem-card.md"
OPENCLAW_RUNTIME_ASSET_PATH = "resources/integrations/openclaw/external-pre-llm-hooks.ts"
OPENCLAW_PACKAGE_RUNTIME_ASSET_PATH = "resources/integrations/openclaw/eightmem-external-pre-llm-hooks.js"
OPENCLAW_PACKAGE_RUNTIME_HELPER_FILE_NAME = "eightmem-external-pre-llm-hooks.js"
OPENCLAW_RUNTIME_HELPER_RELATIVE_PATH = Path("src/auto-reply/reply/external-pre-llm-hooks.ts")
OPENCLAW_RUNTIME_RUNNER_RELATIVE_PATH = Path("src/auto-reply/reply/agent-runner.ts")
OPENCLAW_RUNTIME_HOOK_TYPES_RELATIVE_PATH = Path("src/config/types.hooks.ts")
OPENCLAW_RUNTIME_HOOK_SCHEMA_RELATIVE_PATH = Path("src/config/zod-schema.hooks.ts")
OPENCLAW_RUNTIME_ROOT_SCHEMA_RELATIVE_PATH = Path("src/config/zod-schema.ts")
AGENT_CONTEXT_STALE_SECONDS = 2 * 60 * 60
OPENCLAW_MEMORY_FLOW_FILES = (
    "8mem-api-remember.md",
    "8mem-api-propose.md",
    "8mem-api-correct.md",
    "8mem-api-forget.md",
    "8mem-api-approval.md",
)
OPENCLAW_LEGACY_COMMANDS_FILE_NAME = "AGENTS-COMMANDS.md"
HERMES_BLOCK_NAME = "8mem-hermes-integration"
HERMES_BLOCK_START = f"<!-- 8mem managed block: {HERMES_BLOCK_NAME} -->"
HERMES_BLOCK_END = f"<!-- /8mem managed block: {HERMES_BLOCK_NAME} -->"
HERMES_CONNECTOR_ID = "hermes"
AGENT_SYNC_RECEIVER_SERVICE_NAME = "8mem-agent-sync-receiver.service"
LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME = "8mem-webhook-receiver.service"
AGENT_SYNC_CONNECTOR_ID = "local-agents"
AGENT_SYNC_RECEIVER_PORT = 18792
TELEGRAM_COMMANDS = [
    {"command": "passport", "description": "Show 8mem memory"},
    {"command": "compare", "description": "Generic vs memory-shaped answer"},
    {"command": "corrections", "description": "Show correction history"},
    {"command": "refresh8mem", "description": "Fetch latest 8mem context"},
    {"command": "brief", "description": "Generate a knowledge brief"},
]


def has_command(command: str) -> bool:
    return any((Path(directory) / command).exists() for directory in os.getenv("PATH", "").split(os.pathsep) if directory)


def _read_pid(path: Path | None = None) -> int | None:
    pid_path = path or _pid_file()
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (FileNotFoundError, ValueError):
        return None


def _pid_is_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _probe_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _probe_readyz(host: str, port: int, timeout_seconds: float = 1.5) -> tuple[bool, str]:
    url = f"http://{_probe_host(host)}:{port}/readyz"
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None) or response.getcode()
            if 200 <= int(status) < 300:
                return True, url
            return False, f"{url} returned HTTP {status}"
    except (OSError, URLError, ValueError) as exc:
        return False, f"{url} not ready: {exc}"


def _wait_for_readyz(host: str, port: int, timeout_seconds: float = 8.0) -> tuple[bool, str]:
    deadline = time.time() + timeout_seconds
    last_message = ""
    while time.time() < deadline:
        ok, message = _probe_readyz(host, port)
        if ok:
            return True, message
        last_message = message
        time.sleep(0.25)
    return False, last_message or f"http://{host}:{port}/readyz not ready"


def _port_is_in_use(host: str, port: int, timeout_seconds: float = 0.5) -> bool:
    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _systemd_user_service() -> dict[str, str] | None:
    if not has_command("systemctl"):
        return None
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                SYSTEMD_SERVICE_NAME,
                "--property=LoadState,ActiveState,SubState,MainPID",
                "--no-page",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value
    if fields.get("LoadState") not in {"loaded", "masked"}:
        return None
    return fields


def _systemd_service_active(service: dict[str, str] | None) -> bool:
    return bool(service and service.get("ActiveState") == "active")


def _run_systemd_user_action(action: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", action, SYSTEMD_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message


def _prepare_runtime() -> Path:
    load_project_env()
    try:
        _, mem = ensure_runtime_dirs()
    except PermissionError as exc:
        typer.secho(
            f"Cannot access runtime directory (~/.8mem by default): {exc}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from exc
    return mem


@app.command()
def version() -> None:
    """Print version."""
    typer.echo(__version__)


@app.command()
def init() -> None:
    """Initialize ~/.8mem runtime and memory files."""
    mem = _prepare_runtime()
    created = copy_default_templates(mem)
    typer.echo(f"Runtime ready: {mem.parent}")
    typer.echo(f"Memory folder: {mem}")
    if created:
        typer.echo("Created templates:")
        for path in created:
            typer.echo(f"- {path.name}")
    else:
        typer.echo("Templates already exist (idempotent init).")


def _prompt_optional(label: str, *, hide_input: bool = False) -> str:
    value = typer.prompt(label, default="", show_default=False, hide_input=hide_input)
    return str(value).strip()


def _prompt_setup_mode(max_attempts: int = 2) -> str:
    typer.echo("How do you want to use 8mem first?")
    typer.echo("1. Browser UI only (save and inspect memory; recommended first)")
    typer.echo("2. Telegram bot (needs BotFather token + public HTTPS URL)")
    typer.echo("3. Both browser UI and Telegram")
    typer.echo("4. OpenClaw agent integration (if OpenClaw is already installed)")
    typer.echo("5. Hermes agent integration (if Hermes is already installed)")
    typer.echo("6. Skip optional setup")
    for attempt in range(max_attempts):
        choice = _prompt_optional("Choose [1]")
        normalized = choice.strip().lower()
        if normalized in {"", "1", "browser", "browser ui", "ui"}:
            return "browser"
        if normalized in {"2", "telegram", "bot"}:
            return "telegram"
        if normalized in {"3", "both"}:
            return "both"
        if normalized in {"4", "openclaw", "type1", "type-1"}:
            return "openclaw"
        if normalized in {"5", "hermes"}:
            return "hermes"
        if normalized in {"6", "skip", "none"}:
            return "skip"
        remaining = max_attempts - attempt - 1
        if remaining:
            typer.secho("Choose 1, 2, 3, 4, 5, or 6.", fg=typer.colors.YELLOW)
        else:
            typer.secho("Using Browser UI only because the setup choice was not valid.", fg=typer.colors.YELLOW)
            return "browser"
    return "browser"


def _print_telegram_setup_prereqs() -> None:
    typer.echo("")
    typer.echo("Telegram setup checklist:")
    typer.echo("1. Create a bot in Telegram with BotFather and paste the bot token here.")
    typer.echo("2. Start a public HTTPS tunnel to this machine, for example ngrok, Tailscale Funnel, Cloudflare Tunnel, or your own HTTPS domain.")
    typer.echo("3. Keep 8mem running with `8mem start` so Telegram can reach it.")
    typer.echo("If you only have the bot token now, you can skip the public URL. Browser memory will work, but Telegram will not reply until the webhook is added.")


def _prompt_agent_host() -> str:
    try:
        value = _prompt_optional("Agent machine IP or hostname (where Hermes/OpenClaw run)? Press Enter to skip if same machine")
    except Exception:
        return "127.0.0.1"
    return value or "127.0.0.1"


def _normalize_setup_mode(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    aliases = {
        "1": "browser",
        "browser": "browser",
        "browser-ui": "browser",
        "browser ui": "browser",
        "ui": "browser",
        "2": "telegram",
        "telegram": "telegram",
        "bot": "telegram",
        "telegram-bot": "telegram",
        "3": "both",
        "both": "both",
        "all": "both",
        "4": "openclaw",
        "5": "hermes",
        "6": "skip",
        "skip": "skip",
        "none": "skip",
        "openclaw": "openclaw",
        "type1": "openclaw",
        "type-1": "openclaw",
        "hermes": "hermes",
    }
    return aliases.get(normalized)


def _default_openclaw_workspace() -> Path:
    return Path.home() / ".openclaw" / "workspace"


def _default_openclaw_config_path(workspace: Path | None = None) -> Path:
    if workspace is not None and workspace.name == "workspace" and workspace.parent.name == ".openclaw":
        return workspace.parent / "openclaw.json"
    return Path.home() / ".openclaw" / "openclaw.json"


def _standard_openclaw_config_exists() -> bool:
    return _default_openclaw_config_path().exists()


def _load_openclaw_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{path} must contain a JSON object")
    return payload


def _openclaw_workspace_from_config(payload: dict[str, object]) -> Path | None:
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return None
    defaults = agents.get("defaults")
    if not isinstance(defaults, dict):
        return None
    workspace = defaults.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        return None
    return Path(workspace).expanduser()


def _normalize_openclaw_workspace(value: str | Path | None, *, config_path: Path | None = None) -> Path:
    if value is None:
        if config_path is not None:
            configured = _openclaw_workspace_from_config(_load_openclaw_json(config_path))
            if configured is not None:
                return configured
        return _default_openclaw_workspace()
    raw = str(value).strip()
    if not raw:
        if config_path is not None:
            configured = _openclaw_workspace_from_config(_load_openclaw_json(config_path))
            if configured is not None:
                return configured
        return _default_openclaw_workspace()
    return Path(raw).expanduser()


def _normalize_openclaw_api_url(value: str | None) -> str | None:
    normalized = _normalize_http_base_url(value)
    if normalized is None:
        return None
    return normalized


def _openclaw_key_env_var_for_api_url(api_url: str, explicit_key: str | None) -> str:
    if explicit_key:
        return "EIGHTMEM_OPENCLAW_API_KEY"
    parsed = urlparse(api_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if host in {"127.0.0.1", "localhost", "::1"} and port == 8787:
        return "EIGHTMEM_LOCAL_API_KEY"
    return "EIGHTMEM_OPENCLAW_API_KEY"


def _approval_flow_block(bot_token_env_file: str, *, use_wrapper_scripts: bool = False) -> str:
    if use_wrapper_scripts:
        return """
### 8mem Approval Flow

Use before email send, calendar write, file delete, external message, destructive shell, or any external-state change.

Rules:
- Create exactly one approval request. Do NOT send a text fallback.
- Do NOT poll. Do NOT say "waiting for approval".
- STOP immediately after creating. Silence until callback arrives.
- Execute only after resolving as approved.
- HARD RULE: never write inline curl for approvals, and never read TELEGRAM_APPROVAL_CHAT_ID or TELEGRAM_BOT_TOKEN yourself. Inline curl bypasses bot routing and can send cards through the wrong bot. Always use the installed scripts below.

Step 1 — create approval.

```bash
bash $HOME/.hermes/agent-hooks/8mem-approval.sh "<type>" "<summary>"
```

After Step 1: STOP. Card sent. Silence.

Step 2 — on callback `approval:approved:<id>`, `approval:denied:<id>`, or `approval:edit:<id>`, MUST resolve before executing.

```bash
bash $HOME/.hermes/agent-hooks/8mem-resolve.sh "<approval:approved:id>"
```

Step 3 — act on result:
- approved: execute.
- denied: say `Denied.` Stop.
- edit: ask for the corrected request. Stop.
- missing/not found: say `Approval expired — ask again.` Stop.
- Never execute without confirmed resolution.
""".strip()

    return f"""
### 8mem Approval Flow

Use before email send, calendar write, file delete, external message, destructive shell, or any external-state change.

Rules:
- Create exactly one approval request.
- Do NOT poll. Do NOT say "waiting for approval".
- STOP immediately after creating. Silence until callback arrives or the user sends the manual fallback.
- Execute only after resolving as approved.
- Some OpenClaw Telegram runtimes do not forward inline-button callbacks. If the card button expires or no callback arrives, ask the user to reply exactly `approval:approved:<id>` or `approval:denied:<id>` using the approval id printed by Step 1. This is a manual fallback for external actions only.

Step 1 — create approval.

```bash
E=$HOME/.8mem/.env
BOT_TOKEN_ENV={bot_token_env_file}
LOCAL_API_KEY=$(grep '^EIGHTMEM_LOCAL_API_KEY=' "$E" | cut -d= -f2- | tr -d '"')
TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_APPROVAL_CHAT_ID=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_ALLOWED_USER_ID=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_APPROVAL_CHAT_ID=' "$E" 2>/dev/null | cut -d= -f2- | tr -d '"')
fi
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')
curl -s -X POST "http://127.0.0.1:8787/v1/approval" \\
  -H "Authorization: Bearer $LOCAL_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"action_type":"<type>","summary":"<summary>","ttl_seconds":900,"payload":{{"telegram_chat_id":"'"$TELEGRAM_CHAT_ID"'","telegram_bot_token":"'"$BOT_TOKEN"'"}}}}' \\
  -o /tmp/8mem_approval.json
python3 -c "import json; print(json.load(open('/tmp/8mem_approval.json'))['approval']['approval_id'])" > /tmp/8mem_approval_id
cat /tmp/8mem_approval_id
```

After Step 1: STOP. Card sent. If the user says the button expired or no callback arrives, tell them to reply exactly `approval:approved:<id>` or `approval:denied:<id>` using the id from `/tmp/8mem_approval_id`.

Step 2 — on callback or manual fallback `approval:approved:<id>` or `approval:denied:<id>`, MUST resolve before executing.

```bash
E=$HOME/.8mem/.env
LOCAL_API_KEY=$(grep '^EIGHTMEM_LOCAL_API_KEY=' "$E" | cut -d= -f2- | tr -d '"')
CALLBACK="<approval:approved:id>"
DECISION=$(printf "%s" "$CALLBACK" | cut -d: -f2)
APPROVAL_ID=$(printf "%s" "$CALLBACK" | cut -d: -f3)
curl -s -X POST "http://127.0.0.1:8787/v1/approval/$APPROVAL_ID" \\
  -H "Authorization: Bearer $LOCAL_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"decision":"'"$DECISION"'","resolved_by":"telegram"}}' \\
  -o /tmp/8mem_resolve.json
python3 -c "import json; print(json.load(open('/tmp/8mem_resolve.json')).get('approval', {{}}).get('status', 'missing'))"
```

Step 3 — act on result:
- approved: execute.
- denied: say `Denied.` Stop.
- missing/not found: say `Approval expired — ask again.` Stop.
- Never execute without confirmed resolution.
""".strip()


def _hermes_approval_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

ACTION_TYPE="${1:?action_type required}"
SUMMARY="${2:?summary required}"
E="${EIGHTMEM_ENV:-$HOME/.8mem/.env}"
BOT_TOKEN_ENV="${EIGHTMEM_BOT_TOKEN_ENV:-$HOME/.hermes/.env}"

LOCAL_API_KEY=$(grep '^EIGHTMEM_LOCAL_API_KEY=' "$E" 2>/dev/null | cut -d= -f2- | tr -d '"')
TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_APPROVAL_CHAT_ID=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_ALLOWED_USER_ID=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  TELEGRAM_CHAT_ID=$(grep '^TELEGRAM_APPROVAL_CHAT_ID=' "$E" 2>/dev/null | cut -d= -f2- | tr -d '"')
fi
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$BOT_TOKEN_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')

if [ -z "$LOCAL_API_KEY" ]; then
  echo "missing EIGHTMEM_LOCAL_API_KEY in $E" >&2
  exit 2
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
  echo "missing Telegram approval chat id in $BOT_TOKEN_ENV or $E" >&2
  exit 2
fi
if [ -z "$BOT_TOKEN" ]; then
  echo "missing TELEGRAM_BOT_TOKEN in $BOT_TOKEN_ENV" >&2
  exit 2
fi

python3 - "$ACTION_TYPE" "$SUMMARY" "$TELEGRAM_CHAT_ID" "$BOT_TOKEN" <<'PY' > /tmp/8mem_approval_payload.json
import json
import sys

action_type, summary, chat_id, bot_token = sys.argv[1:5]
print(json.dumps({
    "action_type": action_type,
    "summary": summary,
    "ttl_seconds": 900,
    "payload": {
        "telegram_chat_id": chat_id,
        "telegram_bot_token": bot_token,
    },
}))
PY

curl -s -X POST "http://127.0.0.1:8787/v1/approval" \
  -H "Authorization: Bearer $LOCAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/8mem_approval_payload.json \
  -o /tmp/8mem_approval.json

python3 -c "import json; print(json.load(open('/tmp/8mem_approval.json'))['approval']['approval_id'])"
"""


def _hermes_resolve_script() -> str:
    return r"""#!/usr/bin/env bash
set -euo pipefail

CALLBACK="${1:?callback required}"
E="${EIGHTMEM_ENV:-$HOME/.8mem/.env}"
LOCAL_API_KEY=$(grep '^EIGHTMEM_LOCAL_API_KEY=' "$E" 2>/dev/null | cut -d= -f2- | tr -d '"')
DECISION=$(printf "%s" "$CALLBACK" | cut -d: -f2)
APPROVAL_ID=$(printf "%s" "$CALLBACK" | cut -d: -f3)

if [ -z "$LOCAL_API_KEY" ]; then
  echo "missing EIGHTMEM_LOCAL_API_KEY in $E" >&2
  exit 2
fi
if [ -z "$APPROVAL_ID" ]; then
  echo "missing approval id in callback" >&2
  exit 2
fi
case "$DECISION" in
  approved|denied|edit) ;;
  *)
    echo "invalid approval decision: $DECISION" >&2
    exit 2
    ;;
esac

python3 - "$DECISION" <<'PY' > /tmp/8mem_resolve_payload.json
import json
import sys

decision = sys.argv[1]
body = {"decision": decision, "resolved_by": "telegram"}
if decision == "edit":
    body["reason"] = "User requested edit"
print(json.dumps(body))
PY

curl -s -X POST "http://127.0.0.1:8787/v1/approval/$APPROVAL_ID" \
  -H "Authorization: Bearer $LOCAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/8mem_resolve_payload.json \
  -o /tmp/8mem_resolve.json

python3 -c "import json; print(json.load(open('/tmp/8mem_resolve.json')).get('approval', {}).get('status', 'missing'))"
"""


def _default_hermes_config_path() -> Path:
    return Path.home() / ".hermes" / "config.yaml"


def _standard_hermes_config_exists() -> bool:
    return _default_hermes_config_path().exists()


def _default_hermes_gateway_url() -> str:
    return "http://127.0.0.1:8766"


def _agent_sync_dir() -> Path:
    return runtime_home() / "agents"


def _agent_sync_receiver_path() -> Path:
    return runtime_home() / "webhook-receiver.py"


def _agent_sync_receiver_env_path() -> Path:
    return _agent_sync_dir() / "receiver.env"


def _agent_sync_secret_path() -> Path:
    return runtime_home() / "webhook-secret.txt"


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _detect_hermes_project_dir() -> Path | None:
    if not has_command("systemctl"):
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "hermes-gateway.service", "--property=WorkingDirectory"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("WorkingDirectory="):
            continue
        value = line.split("=", 1)[1].strip()
        if value:
            return Path(value).expanduser()
    return None


def _hermes_integration_present() -> bool:
    hermes_home = Path.home() / ".hermes"
    return any(
        path.exists()
        for path in (
            _default_hermes_config_path(),
            hermes_home / "agent-hooks" / "8mem-approval.sh",
            hermes_home / "agent-hooks" / "8mem-resolve.sh",
            hermes_home / "plugins" / "8mem",
        )
    )


def _configured_hermes_project_dir() -> Path | None:
    configured = os.getenv("EIGHTMEM_HERMES_PROJECT_DIR", "").strip() or os.getenv("HERMES_PROJECT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _detect_hermes_project_dir()


def _hermes_approval_callbacks_check() -> dict[str, str] | None:
    if not _hermes_integration_present():
        return None

    project_dir = _configured_hermes_project_dir()
    if project_dir is None:
        return _doctor_check(
            "hermes_approval_callbacks",
            "warn",
            "Hermes integration is present, but Hermes source could not be located. Core 8mem memory features can still work; Telegram Approve buttons for external actions may not wake the agent. Fix: update Hermes to a version with 8mem approval callback forwarding, or set EIGHTMEM_HERMES_PROJECT_DIR for inspection.",
        )

    telegram_py = project_dir / "gateway" / "platforms" / "telegram.py"
    if not telegram_py.exists():
        return _doctor_check(
            "hermes_approval_callbacks",
            "warn",
            f"Hermes source was found at {project_dir}, but gateway/platforms/telegram.py is missing. Core 8mem memory features can still work; Telegram Approve buttons for external actions may not wake the agent. Fix: update Hermes to a version with 8mem approval callback forwarding.",
        )

    try:
        source = telegram_py.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _doctor_check(
            "hermes_approval_callbacks",
            "warn",
            f"Could not inspect Hermes Telegram callback support: {exc}. Core 8mem memory features can still work; Telegram Approve buttons for external actions may not wake the agent.",
        )

    has_callback_router = 'data.startswith("approval:")' in source or "data.startswith('approval:')" in source
    has_approval_resolve = "/v1/approval/" in source
    has_session_wake = "handle_message(_wake_event)" in source or "Woke session after 8mem approval" in source
    if has_callback_router and has_approval_resolve and has_session_wake:
        return _doctor_check("hermes_approval_callbacks", "pass", "Hermes forwards 8mem approval callbacks and wakes the agent session")

    return _doctor_check(
        "hermes_approval_callbacks",
        "warn",
        "Hermes does not appear to forward 8mem approval callbacks. /passport, /compare, and memory flows can work, but Telegram Approve buttons for external actions may not wake the agent. Fix: update Hermes to a version with 8mem approval callback forwarding.",
    )


def _register_telegram_commands(bot_token: str, *, timeout_seconds: int = 10) -> dict[str, Any]:
    payload = json.dumps({"commands": TELEGRAM_COMMANDS}).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{bot_token}/setMyCommands",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "description": raw}
    return result if isinstance(result, dict) else {"ok": False, "description": raw}


def _telegram_token_from_env_file(path: Path) -> str | None:
    values = _read_env_file(path)
    token = values.get("TELEGRAM_BOT_TOKEN") or values.get("HERMES_TELEGRAM_BOT_TOKEN")
    return token if token and _looks_like_telegram_token(token) else None


def _register_telegram_commands_from_env(path: Path) -> str:
    token = _telegram_token_from_env_file(path)
    if not token:
        return "skipped: no Telegram bot token found"
    try:
        result = _register_telegram_commands(token)
    except Exception as exc:
        return f"warning: {exc}"
    if result.get("ok"):
        return "registered"
    return f"warning: {result.get('description') or 'Telegram rejected setMyCommands'}"


def _default_openclaw_gateway_env_path() -> Path:
    return Path.home() / ".openclaw" / "gateway.systemd.env"


def _managed_markdown_block(body: str) -> str:
    return f"{OPENCLAW_BLOCK_START}\n{body.strip()}\n{OPENCLAW_BLOCK_END}\n"


def _hermes_managed_markdown_block(body: str) -> str:
    return f"{HERMES_BLOCK_START}\n{body.strip()}\n{HERMES_BLOCK_END}\n"


def _replace_marked_block(existing: str, block: str, *, start_marker: str, end_marker: str) -> str:
    start = existing.find(start_marker)
    end = existing.find(end_marker)
    if start >= 0 and end >= start:
        end += len(end_marker)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].strip()
        parts = [item for item in [prefix, block.strip(), suffix] if item]
        return "\n\n".join(parts) + "\n"
    if not existing.strip():
        return block
    return existing.rstrip() + "\n\n" + block


def _write_hermes_markdown_block(path: Path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _hermes_managed_markdown_block(body)
    updated = _replace_marked_block(existing, block, start_marker=HERMES_BLOCK_START, end_marker=HERMES_BLOCK_END)
    if updated == existing:
        return "unchanged"
    path.write_text(updated, encoding="utf-8")
    return "updated"


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _replace_managed_block(existing: str, block: str) -> str:
    start = existing.find(OPENCLAW_BLOCK_START)
    end = existing.find(OPENCLAW_BLOCK_END)
    if start >= 0 and end >= start:
        end += len(OPENCLAW_BLOCK_END)
        prefix = existing[:start].rstrip()
        suffix = existing[end:].strip()
        parts = [item for item in [prefix, block.strip(), suffix] if item]
        return "\n\n".join(parts) + "\n"
    if not existing.strip():
        return block
    return existing.rstrip() + "\n\n" + block


def _managed_markdown_update(path: Path, block: str) -> tuple[str, str]:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = _replace_managed_block(existing, block)
    return updated, "updated" if updated != existing else "unchanged"


def _validate_openclaw_bootstrap_file_size(path: Path, content: str) -> None:
    size = len(content)
    if size <= OPENCLAW_BOOTSTRAP_FILE_SAFE_LIMIT:
        return
    over_by = size - OPENCLAW_BOOTSTRAP_FILE_SAFE_LIMIT
    raise typer.BadParameter(
        f"{path} would be {size} chars after 8mem setup, exceeding 8mem's "
        f"{OPENCLAW_BOOTSTRAP_FILE_SAFE_LIMIT} char safety limit by {over_by}. "
        f"OpenClaw's hard bootstrap-file limit is {OPENCLAW_BOOTSTRAP_FILE_CHAR_LIMIT} chars. "
        "OpenClaw silently truncates oversized bootstrap files. Split existing rules into another startup file, "
        "then rerun `8mem setup --mode openclaw`."
    )


def _write_validated_managed_markdown(path: Path, content: str, status: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if status == "updated":
        path.write_text(content, encoding="utf-8")
    return status


def _remove_managed_block(existing: str) -> tuple[str, bool]:
    start = existing.find(OPENCLAW_BLOCK_START)
    end = existing.find(OPENCLAW_BLOCK_END)
    if start < 0 or end < start:
        return existing, False
    end += len(OPENCLAW_BLOCK_END)
    prefix = existing[:start].rstrip()
    suffix = existing[end:].strip()
    parts = [item for item in [prefix, suffix] if item]
    return ("\n\n".join(parts) + "\n") if parts else "", True


def _remove_managed_markdown(path: Path, *, remove_empty_file: bool = False) -> str:
    if not path.exists():
        return "missing"
    existing = path.read_text(encoding="utf-8")
    updated, changed = _remove_managed_block(existing)
    if changed:
        if remove_empty_file and not updated.strip():
            path.unlink()
            return "removed file"
        path.write_text(updated, encoding="utf-8")
        return "removed"
    return "unchanged"


def _openclaw_agents_block(api_url: str, key_env_var: str) -> str:
    return _managed_markdown_block(
        f"""
## 8mem Memory Integration

8mem is the memory layer. The connected OpenClaw agent is the user-facing assistant. The user should not need to talk to a separate 8mem bot for normal memory flows.

Startup instruction:
- Read `MEMORY-API.md` at startup.
- Read `8MEM-COMMANDS.md` at startup.
- Read `8mem-card.md` at startup.
- Treat those files as the source of truth for 8mem memory commands, context refresh behavior, approval flow, and card helpers.
- 8mem API base URL: `{api_url}`
- 8mem bearer key source: read `{key_env_var}` from `{runtime_env_path()}` at call time. Do not paste or expose the key in chat.

Precedence:
- Apply 8mem intent routing before email, calendar, file, shell, or generic assistant routing.
- Do not save memory, send external messages, or expose API keys unless the referenced 8mem files explicitly allow it.
- For every memory save/update/delete, follow the `MEMORY-API.md` status contract exactly.
- Never hardcode `Saved.` after a memory API call. Say `Saved.` only for exact `status: written`; say `Already in memory.` for exact `status: duplicate`; say `Already tracked.` for exact `status: skipped`.
- If the model context contains an `[8mem] ...` directive from the runtime hook, obey it exactly and do not call the 8mem write API yourself.
- `[8mem] Saved.` -> reply exactly `Saved.` Stop.
- `[8mem] Already in memory.` -> reply exactly `Already in memory.` Stop.
- `[8mem] Already tracked.` -> reply exactly `Already tracked.` Stop.
- `[8mem] Forgotten.` -> reply exactly `Forgotten.` Stop.
- `[8mem] Updated.` -> reply exactly `Updated.` Stop.
- `[8mem] Existing:` -> show the existing/new question exactly, then wait for the user's answer.
- `[8mem] Could not` -> reply with that failure line in plain English, then stop.
"""
    )

def _openclaw_agents_commands_block() -> str:
    return _managed_markdown_block(
        """
## 8mem Command Rules

Use these rules for 8mem user-facing command output. `AGENTS.md` is the thin startup router. `MEMORY-API.md` routes to exact memory API flow files.

Precedence:
- Apply 8mem intent routing before email, calendar, file, shell, or generic assistant routing.
- Negotiation/commercial quote detection runs before email drafting and before approval flow.
- If a message mentions pricing, quotes, vendor terms, investor terms, valuation, discounts, contract terms, or a saved negotiation floor, treat it as a negotiation memory task first. Do not draft or send an email until the user explicitly asks for an email draft.

Command routing:
| User asks | Required behavior |
| --- | --- |
| `/corrections` | Run exact command from `MEMORY-API.md` section `/corrections`. Do not write your own parsing code. |
| `/passport`, `what do you know about me`, `summarise what you know about me` | Run exact command from `MEMORY-API.md` section `/passport`. Do not write your own passport headings. After the text reply, run `8mem-card.md` function `SEND_PASSPORT_CARD` before ending the turn. |
| `/brief [person]` | Use the Brief format below. Summarise what someone should know before working with that person. |
| bare `/compare` with no topic | Ask: `What topic should I compare? Send /compare followed by the topic. Example: /compare should I add one more launch feature tonight` Then stop. Do not send a compare card. |
| `/compare <topic>` | Use the exact Compare format below. Show standard answer versus memory-shaped answer using cached `memory/8mem-context.md`; if missing/empty, run `/refresh8mem` once. If still missing/empty, say `8mem context is not refreshed yet. Send /refresh8mem and retry.` and stop. After the text reply, run `8mem-card.md` function `SEND_COMPARE_CARD` before ending the turn. |
| inferred durable preference or operating principle | Use `MEMORY-API.md` section `propose`. Create a proposal; do not silently save. |

## /passport

Show a compact, useful view of what 8mem is actively using for the user. Keep it readable; do not dump the entire memory store.

Primary source:
- Read directly from cached `memory/8mem-context.md`.
- If cached context is missing or empty, run `/refresh8mem` once before answering. If it is still missing or empty, say `8mem context is not refreshed yet. Send /refresh8mem and retry.` and stop.
- The first line of `memory/8mem-context.md` is `User is Name (timezone).` Extract the timezone from there. Never use the Pi system clock or infer UTC.
- Do not perform a live fetch before answering.
- Do not invent beliefs; do not invent saved memory when context is missing.
- Do not stop if 8mem is temporarily unreachable.

Optional correction detail:
- Best effort only: use `MEMORY-API.md` section `recent correction details` to fetch old -> new correction records.
- If the fetch succeeds, show at most 3 `Recent corrections` as `date: old -> new`.
- If it fails, omit `Recent corrections` entirely.
- Do not fill `Recent corrections` with flat correction-sourced beliefs.
- Never use a `/passport` section heading named `Corrections`; that heading is reserved for the `/corrections` command only.
- Never use a `/passport` section heading named `Hard rules`; corrections are not rules.
- Show at most 8 total memory bullets in the text reply.

Card:
- After the text reply, run `8mem-card.md` function `SEND_PASSPORT_CARD` before ending the turn.
- Card send is best effort. If it fails, do not mention it to the user.
- Do not skip the helper just because the text answer was sent successfully.

Output formats:

Passport:
```text
<Name> - 8mem passport
<location/timezone> - <short role/context>

Currently helping with
- <short active memory>
- <short active memory>

Recent corrections
<date>: <old> -> <new>

Trust check
- Read-only commands do not save memory.
- Corrections override older memories.
- Ask "forget <memory>" anytime.
```

Brief:
```text
Before working with <name>

Context
- <identity/work context>

How to work with them
- <communication/style preference>
- <important constraint>

Recent changes
<date>: <old> -> <new>
```

Compare:
```text
Without memory
<generic answer>

With <Name>'s memory
<answer shaped by memory>

Used
- <specific memory/correction used>
```

Formatting rules:
- Keep Telegram replies compact and human. No markdown tables in Telegram.
- Bullets only; no inline labels like `Name:` or `Timezone:`.
- Use short headings and bullets. No file names, file paths, ports, hostnames, API names, bearer keys, JSON, or implementation notes.
- If a section has no useful facts, omit that section instead of saying it is empty.

Confirmation rules:
- After remember returns exact status `written`: reply exactly `Saved.` Then stop.
- If remember returns `duplicate`: reply exactly `Already in memory.` Then stop.
- If remember returns `skipped`: reply exactly `Already tracked.` Then stop.
- If conflict resolution keeps existing memory: reply exactly `Kept existing.` Then stop.
- After correct returns exact status `corrected`: reply exactly `Updated.` Then stop.
- After forget confirm returns exact status `deleted`: reply exactly `Forgotten.` Then stop.
- If forget preview returns `not_found`: reply exactly `No matching memory found.` Then stop.
- If forget confirm returns `not_found`: reply exactly `Nothing was deleted.` Then stop.
- For inferred durable memory, do not say it was saved until `MEMORY-API.md` section `commit proposal` returns `"status": "written"`.
- NEVER add anything after the confirmation line.
- Specifically banned: `Note: I did not schedule a reminder`, `this will not trigger automatically`, and any mention of scheduling, reminders, triggers, automation, files, APIs, or implementation details.
- The confirmation line is the complete response. Nothing follows it.

Approval rules:
- Use `MEMORY-API.md` section `Approval Flow` before email send, calendar write, file delete, external message, destructive shell command, or any action that changes external state.
- Show exactly one approval request to the user. The 8mem approval endpoint sends the Telegram inline card automatically when configured; do not also send a text fallback.
- Buttons must include Approve, Deny, and Edit.
- Callback data:
  - `approval:approved:<approval_id>` -> resolve approved, then execute the action
  - `approval:denied:<approval_id>` -> resolve denied, then cancel
  - `approval:edit:<approval_id>` -> resolve original as denied with reason `User requested edit`, ask what to change, then create a fresh approval

Privacy rules:
- `/passport` and `what do you know about me` must read only from `memory/8mem-context.md`.
- Reply only with facts about the user.
- Do not expose file paths, ports, hostnames, bearer keys, JSON payloads, stack traces, service names, or how the memory system works unless the user explicitly asks for debugging details.
"""
    )


def _openclaw_memory_api_block(api_url: str, workspace: Path, key_env_var: str) -> str:
    parsed = urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    context_path = _shell_quote(str(workspace / "memory" / "8mem-context.md"))
    env_path = _shell_quote(str(runtime_env_path()))
    gateway_env_path = _shell_quote(str(_default_openclaw_gateway_env_path()))
    template = """
## 8mem Memory API

Exact OpenClaw memory commands. Read when `AGENTS.md` asks for 8mem behavior.

API: `__API_URL__` (`__HOST__:__PORT__`). Auth: read `__KEY_ENV_VAR__` from `__ENV_PATH__` at call time.

Shared shell prelude. Use before commands below unless the command defines `E` itself:
```bash
E=__ENV_PATH__
API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
```

### /corrections

Run this. Do not infer from cache.

```bash
E=__ENV_PATH__
API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
curl -s -f --connect-timeout 10 "__API_URL__/v1/context" -H "Authorization: Bearer $API_KEY" -o /tmp/8mem_ctx.json && \
python3 -c "import json; c=json.load(open('/tmp/8mem_ctx.json')).get('corrections', []); shown=[x for x in c if x.get('old_value')]; [print(f'{(x.get(\"corrected_at\") or \"\").split(\"T\")[0]}: {x.get(\"old_value\",\"\")} -> {x.get(\"new_value\",\"\")}') for x in shown] or print('No old -> new corrections recorded yet.')" || echo "Error: could not reach 8mem - may be offline."
```

### /passport

Run this exact command for `/passport`, `what do you know about me`, or `summarise what you know about me`. Do not invent extra sections. Do not use headings named `Hard rules` or `Corrections`.

```bash
E=__ENV_PATH__
API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
curl -s -f --connect-timeout 10 "__API_URL__/v1/context" -H "Authorization: Bearer $API_KEY" -o /tmp/8mem_ctx.json && \
python3 - <<'PY'
import json

def clean(value):
    value = " ".join(str(value or "").strip().split())
    if not value:
        return ""
    lower = value.lower()
    if lower.startswith(("user is ", "timezone:", "language:", "deleted memory")):
        return ""
    if any(token in lower for token in ("/v1/", "bearer", "api key", "localhost", "system_prompt_injection")):
        return ""
    return value.rstrip(".")

ctx = json.load(open("/tmp/8mem_ctx.json"))
identity = ctx.get("identity") if isinstance(ctx.get("identity"), dict) else {}
name = identity.get("display_name") or identity.get("name") or "User"
timezone = identity.get("timezone") or ""
role = identity.get("role") or identity.get("context") or ""
subtitle = " - ".join(part for part in (timezone, role) if part)

lines = [f"{name} - 8mem passport"]
if subtitle:
    lines.append(subtitle)

seen = set()
beliefs = []
for item in ctx.get("beliefs", []):
    if not isinstance(item, dict) or item.get("status", "active") != "active":
        continue
    value = clean(item.get("value", ""))
    key = value.lower()
    if not value or key in seen:
        continue
    seen.add(key)
    beliefs.append(value)
    if len(beliefs) >= 8:
        break
if beliefs:
    lines.extend(["", "Currently helping with"])
    lines.extend(f"- {item}" for item in beliefs)

corrections = []
for item in ctx.get("corrections", []):
    if not isinstance(item, dict):
        continue
    old = clean(item.get("old_value", ""))
    new = clean(item.get("new_value", ""))
    if not old or not new:
        continue
    date = (item.get("corrected_at") or item.get("created_at") or "").split("T")[0]
    corrections.append(f"{date + ': ' if date else ''}{old} -> {new}")
if corrections:
    lines.extend(["", "Recent corrections"])
    lines.extend(f"- {item}" for item in corrections[-3:])

lines.extend([
    "",
    "Trust check",
    "- Read-only commands do not save memory.",
    "- Corrections override older memories.",
    "- Ask \"forget <memory>\" anytime.",
])
print("\n".join(lines))
PY
```

### /refresh8mem

```bash
mkdir -p __WORKSPACE_MEMORY_DIR__
E=__ENV_PATH__
API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
response=$(curl -s -f --connect-timeout 10 "__API_URL__/v1/context" \
  -H "Authorization: Bearer $API_KEY") && \
echo "$response" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['system_prompt_injection'], end='')" \
  > __CONTEXT_PATH__.tmp && mv __CONTEXT_PATH__.tmp __CONTEXT_PATH__
```

### recent correction details

Best effort for `/passport` Recent changes. Use `/corrections` output if live fetch succeeds; otherwise use cache and omit Recent changes.

### passport card

After `/passport` text, run `8mem-card.md` function `SEND_PASSPORT_CARD` before ending the turn. Best effort; never mention card failure. Do not skip the helper just because the text answer was sent successfully.

### compare card

After successful `/compare <topic>` text, run `8mem-card.md` function `SEND_COMPARE_CARD` before ending the turn. Do not run this for bare `/compare` with no topic. Use same text answers/memories. Best effort; never mention card failure. Do not skip the helper just because the text answer was sent successfully.

### remember

Read `memory/8mem-api-remember.md` and follow it exactly.

### propose

Read `memory/8mem-api-propose.md` and follow it exactly.

### correct

Read `memory/8mem-api-correct.md` and follow it exactly.

### forget

Read `memory/8mem-api-forget.md` and follow it exactly.

### Approval Flow

Read `memory/8mem-api-approval.md` and follow it exactly. Approval is for external actions only, never for memory remember/correct/forget.
"""
    return _managed_markdown_block(
        template.replace("__API_URL__", api_url)
        .replace("__HOST__", host)
        .replace("__PORT__", port)
        .replace("__KEY_ENV_VAR__", key_env_var)
        .replace("__ENV_PATH__", env_path)
        .replace("__GATEWAY_ENV_PATH__", gateway_env_path)
        .replace("__WORKSPACE_MEMORY_DIR__", _shell_quote(str(workspace / "memory")))
        .replace("__CONTEXT_PATH__", context_path)
        .replace("__APPROVAL_FLOW__", _approval_flow_block(gateway_env_path))
    )


def _openclaw_memory_flow_blocks(api_url: str, key_env_var: str) -> dict[str, str]:
    env_path = _shell_quote(str(runtime_env_path()))
    gateway_env_path = _shell_quote(str(_default_openclaw_gateway_env_path()))
    prelude = f"""```bash
E={env_path}
API_KEY=$(grep '^{key_env_var}=' "$E" | cut -d= -f2- | tr -d '"')
```"""
    common_status = """
Status contract:
- `written`: run `/refresh8mem`; only after refresh succeeds, reply exactly `Saved.` Stop.
- `duplicate`: reply exactly `Already in memory.` Stop.
- `skipped`: reply exactly `Already tracked.` Stop.
- `refinement_pending`: show existing vs new and ask whether to update. Existing is `conflicting_belief.value`, `existing.value`, or `conflict.value`; new is `proposed.value`.
- If user says no to refinement: reply exactly `Kept existing.` Stop.
- If user says yes to refinement: run `correct` with `old_text` set to existing and `text` set to new. Reply `Updated.` only after `status: corrected`.
- `conflict`: show existing vs new. Existing is `conflicting_belief.value` or `conflict.value`; new is `proposed.value`.
- If user keeps existing: reply exactly `Kept existing.` Stop. Do not call another write.
- If user keeps new: run `correct` with `old_text` set to existing and `text` set to new. Reply `Updated.` only after `status: corrected`.
- Any other status, HTTP error, timeout, or connection failure: report the failure. Never say `Saved.`, `Updated.`, or `Forgotten.`
""".strip()
    remember = f"""
## 8mem Remember Flow

Installer-owned exact flow. Use only for explicit remember/from-now-on/always/do-not memory requests.

Shared prelude:
{prelude}

Rules:
- Use Python `json.dumps`; never hand-build JSON.
- Do not use external-action approval for memory operations.

```bash
FACT='<fact>'
body=$(FACT="$FACT" python3 -c 'import json, os; print(json.dumps({{"text": os.environ.get("FACT", ""), "source": "openclaw_agent", "confidence": "explicit_user_statement"}}))')
curl -sS -X POST "{api_url}/v1/memory" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_memory_write.json
python3 -c 'import json; print(json.load(open("/tmp/8mem_memory_write.json")).get("status","missing"))'
```

{common_status}
"""
    propose = f"""
## 8mem Proposal Flow

Installer-owned exact flow. Use only for inferred durable memory. Do not say saved until commit returns `written`.

Shared prelude:
{prelude}

Step 1: create proposal.

```bash
CANDIDATE='<candidate memory>'
SOURCE_MESSAGE='<user message>'
body=$(CANDIDATE="$CANDIDATE" SOURCE_MESSAGE="$SOURCE_MESSAGE" python3 -c 'import json, os; print(json.dumps({{"text": os.environ.get("CANDIDATE", ""), "source": "openclaw_agent", "confidence": "inferred_durable_preference", "source_message": os.environ.get("SOURCE_MESSAGE", "")}}))')
curl -sS -X POST "{api_url}/v1/memory/propose" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_proposal.json
python3 -c 'import json; r=json.load(open("/tmp/8mem_proposal.json")); print(r.get("status","missing")); print(r.get("proposal",{{}}).get("id",""))' > /tmp/8mem_proposal_status
```

Proposal statuses:
- `proposed`: capture `proposal.id`. If missing, reply `8mem proposal failed: missing proposal id.` Stop. Otherwise ask exactly `Save this to memory? Reply yes or no.` Stop.
- `pending`: show existing pending proposal and ask yes/no. Do not create another proposal.
- `duplicate`: reply exactly `Already in memory.` Stop.
- `skipped`: reply exactly `Already tracked.` Stop.
- `refinement_pending`: follow the refinement branch from `8mem-api-remember.md`.
- `conflict`: follow the conflict branch from `8mem-api-remember.md`.

Step 2: commit only after yes.

```bash
PROPOSAL_ID='<proposal_id>'
body=$(PROPOSAL_ID="$PROPOSAL_ID" python3 -c 'import json, os; print(json.dumps({{"proposal_id": os.environ.get("PROPOSAL_ID", ""), "decision": "approved"}}))')
curl -sS -X POST "{api_url}/v1/memory/commit" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_commit.json
python3 -c 'import json; print(json.load(open("/tmp/8mem_commit.json")).get("status","missing"))'
```

Commit statuses:
- `written`: run `/refresh8mem`; only after refresh succeeds, reply exactly `Saved.` Stop.
- `denied`: reply exactly `Not saved.` Stop.
- `not_found`: reply `Approval expired - ask again.` Stop.
- `not_pending`: reply `That memory proposal is no longer pending.` Stop.
- `duplicate`, `skipped`, `refinement_pending`, `conflict`: handle as in remember flow.

If user says no:

```bash
PROPOSAL_ID='<proposal_id>'
body=$(PROPOSAL_ID="$PROPOSAL_ID" python3 -c 'import json, os; print(json.dumps({{"proposal_id": os.environ.get("PROPOSAL_ID", ""), "decision": "denied"}}))')
curl -sS -X POST "{api_url}/v1/memory/commit" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_commit.json
```

Only after response status `denied`, reply exactly `Not saved.` Stop.
"""
    correct = f"""
## 8mem Correct Flow

Installer-owned exact flow. Use when the user corrects an existing memory or when conflict resolution keeps the new value.

Shared prelude:
{prelude}

```bash
CORRECTED_FACT='<corrected fact>'
OLD_FACT='<old fact if known>'
body=$(CORRECTED_FACT="$CORRECTED_FACT" OLD_FACT="$OLD_FACT" python3 -c 'import json, os; print(json.dumps({{"text": os.environ.get("CORRECTED_FACT", ""), "source": "openclaw_agent", "old_text": os.environ.get("OLD_FACT", "")}}))')
curl -sS -X POST "{api_url}/v1/correction" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_correction.json
python3 -c 'import json; print(json.load(open("/tmp/8mem_correction.json")).get("status","missing"))'
```

Status contract:
- `corrected`: run `/refresh8mem`; only after refresh succeeds, reply exactly `Updated.` Stop.
- Any other status, HTTP error, timeout, or connection failure: report the failure. Never say `Updated.`
"""
    forget = f"""
## 8mem Forget Flow

Installer-owned exact flow. Preview first; delete only after user confirms.

Shared prelude:
{prelude}

Step 1: preview.

```bash
THING='<thing>'
body=$(THING="$THING" python3 -c 'import json, os; print(json.dumps({{"query": os.environ.get("THING", ""), "source": "openclaw_agent"}}))')
curl -sS -X POST "{api_url}/v1/forget" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_forget_preview.json
python3 -c 'import json; print(json.load(open("/tmp/8mem_forget_preview.json")).get("status","missing"))'
```

Preview statuses:
- `needs_confirmation` with 1-3 candidates: show candidates without file/path details, ask `Should I forget it? Reply yes or no.`, stop.
- `needs_confirmation` with more than 3 candidates: list candidates and ask which specific item to forget. Do not bulk delete.
- `not_found`: reply exactly `No matching memory found.` Stop.

Step 2: delete only after yes.

```bash
THING='<thing>'
body=$(THING="$THING" python3 -c 'import json, os; print(json.dumps({{"query": os.environ.get("THING", ""), "source": "openclaw_agent", "confirm": True}}))')
curl -sS -X POST "{api_url}/v1/forget" -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" -d "$body" -o /tmp/8mem_forget_delete.json
python3 -c 'import json; print(json.load(open("/tmp/8mem_forget_delete.json")).get("status","missing"))'
```

Delete statuses:
- `deleted`: run `/refresh8mem`; only after refresh succeeds, reply exactly `Forgotten.` Stop.
- `not_found`: reply exactly `Nothing was deleted.` Stop.
- Any other status, HTTP error, timeout, or connection failure: report the failure. Never say `Forgotten.`
"""
    approval = f"""
## 8mem Approval Flow

Installer-owned exact flow for external actions only. Never use this for memory remember/correct/forget.

{_approval_flow_block(gateway_env_path)}
"""
    return {
        "8mem-api-remember.md": _managed_markdown_block(remember.strip()),
        "8mem-api-propose.md": _managed_markdown_block(propose.strip()),
        "8mem-api-correct.md": _managed_markdown_block(correct.strip()),
        "8mem-api-forget.md": _managed_markdown_block(forget.strip()),
        "8mem-api-approval.md": _managed_markdown_block(approval.strip()),
    }


def _openclaw_card_block(api_url: str, key_env_var: str) -> str:
    parsed = urlparse(api_url)
    host = parsed.hostname or "127.0.0.1"
    port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
    template = """
## 8mem Card Helpers

Source this file from OpenClaw command handling when a Telegram card should be sent.
These helpers are best effort. They must never block or fail the text reply.

Shared behavior:
- Read the 8mem bearer key from `__ENV_PATH__` at call time.
- Read Telegram credentials from `__GATEWAY_ENV_PATH__`.
- Default HOST is the 8mem API host configured by `8mem setup --mode openclaw`.
- Override HOST only for debugging.

```bash
SEND_PASSPORT_CARD() {
  E=__ENV_PATH__
  GW=__GATEWAY_ENV_PATH__
  API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
  HOST=${HOST:-__API_HOST__}
  if [ -f "$GW" ]; then set -a; . "$GW"; set +a; fi
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_ALLOWED_USER_ID" ] && [ -n "$API_KEY" ]; then
    curl -sf --connect-timeout 10 "http://${HOST}/v1/passport/card" \
      -H "Authorization: Bearer $API_KEY" \
      -o /tmp/8mem_passport_card.png && \
    curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto" \
      -F "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
      -F "photo=@/tmp/8mem_passport_card.png" \
      -F "caption=8mem passport" >/dev/null || true
  fi
}

SEND_COMPARE_CARD() {
  E=__ENV_PATH__
  GW=__GATEWAY_ENV_PATH__
  API_KEY=$(grep '^__KEY_ENV_VAR__=' "$E" | cut -d= -f2- | tr -d '"')
  HOST=${HOST:-__API_HOST__}
  if [ -f "$GW" ]; then set -a; . "$GW"; set +a; fi
  if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_ALLOWED_USER_ID" ] && [ -n "$API_KEY" ] && [ -f /tmp/8mem_compare_card.json ]; then
    curl -sf --connect-timeout 10 -X POST "http://${HOST}/v1/compare/card" \
      -H "Authorization: Bearer $API_KEY" \
      -H "Content-Type: application/json" \
      -d @/tmp/8mem_compare_card.json \
      -o /tmp/8mem_compare_card.png && \
    curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendPhoto" \
      -F "chat_id=${TELEGRAM_ALLOWED_USER_ID}" \
      -F "photo=@/tmp/8mem_compare_card.png" \
      -F "caption=8mem Compare" >/dev/null || true
  fi
}
```

For compare cards, create `/tmp/8mem_compare_card.json` before calling `SEND_COMPARE_CARD`:

```json
{"topic":"<topic>","generic_answer":"<without memory answer>","memory_answer":"<with memory answer>","used_beliefs":["<memory used>"]}
```
"""
    return _managed_markdown_block(
        template.replace("__KEY_ENV_VAR__", key_env_var)
        .replace("__API_HOST__", f"{host}:{port}")
        .replace("__ENV_PATH__", _shell_quote(str(runtime_env_path())))
        .replace("__GATEWAY_ENV_PATH__", _shell_quote(str(_default_openclaw_gateway_env_path())))
    )


def _openclaw_heartbeat_block(api_url: str, key_env_var: str) -> str:
    return _managed_markdown_block(
        f"""
## 8mem Context Refresh

Refresh 8mem context before memory-sensitive replies and immediately after any successful remember/correct/forget write. Context writes must be atomic.

Context source:
- API: `{api_url}/v1/context`
- Auth: `Authorization: Bearer <{key_env_var} from {runtime_env_path()}>`
- Local output file: `memory/8mem-context.md`

Refresh command:
```bash
mkdir -p memory
API_KEY=$(grep '^{key_env_var}=' {_shell_quote(str(runtime_env_path()))} | cut -d= -f2- | tr -d '"')
curl -fsS "{api_url}/v1/context" -H "Authorization: Bearer $API_KEY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("system_prompt_injection",""))' > memory/8mem-context.md.tmp && mv memory/8mem-context.md.tmp memory/8mem-context.md
```

If refresh fails, keep the current session safe:
- Do not claim a new memory was saved unless the write API succeeded.
- Do not show raw curl, JSON, stack traces, hostnames, or bearer keys in normal replies.
- Ask the operator to run `8mem doctor` if memory stays unavailable.
"""
    )


def _openclaw_soul_block() -> str:
    return _managed_markdown_block(
        """
## 8mem Hook Directives

When the injected context contains an [8mem] directive, obey it exactly and do not call the 8mem API yourself.

If the injected context contains "[8mem] Saved." — your entire reply is: Saved.
If the injected context contains "[8mem] Already in memory." — your entire reply is: Already in memory.
If the injected context contains "[8mem] Already tracked." — your entire reply is: Already tracked.
If the injected context contains "[8mem] Forgotten." — your entire reply is: Forgotten.
If the injected context contains "[8mem] No matching memory found." — your entire reply is: No matching memory found.
If the injected context contains "[8mem] Updated." — your entire reply is: Updated.
If the injected context contains "[8mem] Existing:" — show the question exactly as written, wait for reply.
If the injected context contains "[8mem] Could not save" — your entire reply is: Could not save — try again.
If the injected context contains "[8mem] Could not forget" — your entire reply is: Could not forget — try again.
If the injected context contains "[8mem] Could not update" — your entire reply is: Could not update — try again.
Do NOT call the 8mem API yourself for memory writes. The hook handles it before you respond.
"""
    )


def _openclaw_inline_buttons_state(payload: dict[str, object]) -> dict[str, object]:
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return {"present": False, "value": None, "capabilitiesPresent": False}
    telegram = channels.get("telegram")
    if not isinstance(telegram, dict):
        return {"present": False, "value": None, "capabilitiesPresent": False}
    capabilities = telegram.get("capabilities")
    capabilities_present = isinstance(capabilities, dict)
    if not capabilities_present or "inlineButtons" not in capabilities:
        return {"present": False, "value": None, "capabilitiesPresent": capabilities_present}
    return {"present": True, "value": capabilities.get("inlineButtons"), "capabilitiesPresent": True}


def _openclaw_session_memory_state(payload: dict[str, object]) -> dict[str, object]:
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return {
            "present": False,
            "hooksPresent": False,
            "internalPresent": False,
            "entriesPresent": False,
            "value": None,
        }
    internal = hooks.get("internal")
    if not isinstance(internal, dict):
        return {
            "present": False,
            "hooksPresent": True,
            "internalPresent": False,
            "entriesPresent": False,
            "value": None,
        }
    entries = internal.get("entries")
    entries_present = isinstance(entries, dict)
    if not entries_present or "session-memory" not in entries:
        return {
            "present": False,
            "hooksPresent": True,
            "internalPresent": True,
            "entriesPresent": entries_present,
            "value": None,
        }
    return {
        "present": True,
        "hooksPresent": True,
        "internalPresent": True,
        "entriesPresent": True,
        "value": entries.get("session-memory") if isinstance(entries, dict) else None,
    }


def _openclaw_source_layout(source_dir: Path) -> tuple[Path, Path, Path]:
    return (
        source_dir / OPENCLAW_RUNTIME_HELPER_RELATIVE_PATH,
        source_dir / OPENCLAW_RUNTIME_RUNNER_RELATIVE_PATH,
        source_dir / OPENCLAW_RUNTIME_HOOK_TYPES_RELATIVE_PATH,
    )


def _is_openclaw_source_dir(source_dir: Path) -> bool:
    _, runner_path, hook_types_path = _openclaw_source_layout(source_dir)
    return runner_path.exists() and hook_types_path.exists()


def _systemd_user_service_working_directory(service_name: str) -> Path | None:
    if not has_command("systemctl"):
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", service_name, "--property=WorkingDirectory"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("WorkingDirectory="):
            value = line.split("=", 1)[1].strip()
            if value:
                return Path(value).expanduser()
    return None


def _detect_openclaw_source_dir() -> Path | None:
    configured = os.getenv("EIGHTMEM_OPENCLAW_SOURCE_DIR", "").strip() or os.getenv("OPENCLAW_SOURCE_DIR", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        _systemd_user_service_working_directory(f"{OPENCLAW_GATEWAY_SERVICE_NAME}.service"),
        Path.home() / "general" / "projects" / "openclaw",
        Path.home() / "openclaw",
    ]
    for candidate in candidates:
        if candidate is not None and _is_openclaw_source_dir(candidate):
            return candidate
    return None


def _openclaw_package_runner_path(package_dir: Path) -> Path | None:
    export_path = package_dir / "dist" / "agent-runner.runtime.js"
    if export_path.exists():
        try:
            source = export_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        prefix = 'export * from "./'
        suffix = '";'
        if source.startswith(prefix) and source.endswith(suffix):
            candidate = export_path.parent / source[len(prefix) : -len(suffix)]
            if candidate.exists():
                return candidate
    candidates = sorted((package_dir / "dist").glob("agent-runner.runtime-*.js"))
    return candidates[0] if len(candidates) == 1 else None


def _openclaw_package_schema_path(package_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for candidate in sorted((package_dir / "dist").glob("zod-schema-*.js")):
        try:
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "const InternalHooksSchema = z.object({" in source and "internal: InternalHooksSchema" in source:
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _is_openclaw_package_dir(package_dir: Path) -> bool:
    return (
        (package_dir / "package.json").exists()
        and _openclaw_package_runner_path(package_dir) is not None
        and _openclaw_package_schema_path(package_dir) is not None
    )


def _npm_global_root() -> Path | None:
    if not has_command("npm"):
        return None
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return Path(value).expanduser() if result.returncode == 0 and value else None


def _detect_openclaw_package_dir() -> Path | None:
    configured = os.getenv("EIGHTMEM_OPENCLAW_PACKAGE_DIR", "").strip() or os.getenv("OPENCLAW_PACKAGE_DIR", "").strip()
    npm_root = _npm_global_root()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / ".npm-global" / "lib" / "node_modules" / "openclaw",
        npm_root / "openclaw" if npm_root is not None else None,
    ]
    for candidate in candidates:
        if candidate is not None and _is_openclaw_package_dir(candidate):
            return candidate
    return None


def _detect_openclaw_runtime_dir() -> Path | None:
    return _detect_openclaw_source_dir() or _detect_openclaw_package_dir()


def _openclaw_runtime_asset_text(asset_path: str = OPENCLAW_RUNTIME_ASSET_PATH) -> str:
    asset = resources.files("eightmem").joinpath(*Path(asset_path).parts)
    return asset.read_text(encoding="utf-8")


def _openclaw_pre_llm_hook_source_status(source_dir: Path) -> tuple[str, str]:
    helper_path, runner_path, hook_types_path = _openclaw_source_layout(source_dir)
    hook_schema_path = source_dir / OPENCLAW_RUNTIME_HOOK_SCHEMA_RELATIVE_PATH
    root_schema_path = source_dir / OPENCLAW_RUNTIME_ROOT_SCHEMA_RELATIVE_PATH
    if not _is_openclaw_source_dir(source_dir):
        return "invalid_source", f"{source_dir} is not a supported OpenClaw source checkout"
    try:
        runner_source = runner_path.read_text(encoding="utf-8", errors="replace")
        hook_types_source = hook_types_path.read_text(encoding="utf-8", errors="replace")
        helper_source = helper_path.read_text(encoding="utf-8", errors="replace") if helper_path.exists() else ""
        hook_schema_source = hook_schema_path.read_text(encoding="utf-8", errors="replace") if hook_schema_path.exists() else ""
        root_schema_source = root_schema_path.read_text(encoding="utf-8", errors="replace") if root_schema_path.exists() else ""
    except OSError as exc:
        return "not_inspectable", f"Could not inspect OpenClaw source: {exc}"
    supported = (
        'from "./external-pre-llm-hooks.js"' in runner_source
        and "runExternalPreLlmHooks({" in runner_source
        and "externalHookDirectReply" in runner_source
        and "externalHookNoMatch" in runner_source
        and "external?: ExternalHooksConfig;" in hook_types_source
        and "export async function runExternalPreLlmHooks" in helper_source
        and 'matchType === "always" || matchType === "context"' in helper_source
        and 'outputs.join("\\n\\n")' in helper_source
        and "export const ExternalHooksSchema" in hook_schema_source
        and "external: ExternalHooksSchema" in root_schema_source
    )
    if supported:
        return "supported", f"OpenClaw source at {source_dir} includes external pre_llm_call hook support"
    return "missing", f"OpenClaw source at {source_dir} is missing external pre_llm_call hook support"


def _openclaw_pre_llm_hook_package_status(package_dir: Path) -> tuple[str, str]:
    runner_path = _openclaw_package_runner_path(package_dir)
    schema_path = _openclaw_package_schema_path(package_dir)
    if runner_path is None or schema_path is None:
        return "invalid_package", f"{package_dir} is not a supported packaged OpenClaw runtime"
    helper_path = package_dir / "dist" / OPENCLAW_PACKAGE_RUNTIME_HELPER_FILE_NAME
    try:
        runner_source = runner_path.read_text(encoding="utf-8", errors="replace")
        schema_source = schema_path.read_text(encoding="utf-8", errors="replace")
        helper_source = helper_path.read_text(encoding="utf-8", errors="replace") if helper_path.exists() else ""
    except OSError as exc:
        return "not_inspectable", f"Could not inspect packaged OpenClaw runtime: {exc}"
    supported = (
        "runEightmemExternalPreLlmHooks" in runner_source
        and "runExternalPreLlmHooks as runEightmemExternalPreLlmHooks" in runner_source
        and "externalHookDirectReply" in runner_source
        and "externalHookNoMatch" in runner_source
        and "export async function runExternalPreLlmHooks" in helper_source
        and 'matchType === "always" || matchType === "context"' in helper_source
        and 'outputs.join("\\n\\n")' in helper_source
        and "const ExternalHooksSchema = z.object({" in schema_source
        and "external: ExternalHooksSchema" in schema_source
    )
    if supported:
        return "supported", f"Packaged OpenClaw runtime at {package_dir} includes external pre_llm_call hook support"
    return "missing", f"Packaged OpenClaw runtime at {package_dir} is missing external pre_llm_call hook support"


def _openclaw_pre_llm_hook_runtime_status(runtime_dir: Path | None = None) -> tuple[str, str]:
    resolved_runtime = runtime_dir or _detect_openclaw_runtime_dir()
    if resolved_runtime is None:
        return "not_inspectable", "OpenClaw source checkout or packaged npm runtime was not detected"
    if _is_openclaw_source_dir(resolved_runtime):
        return _openclaw_pre_llm_hook_source_status(resolved_runtime)
    if _is_openclaw_package_dir(resolved_runtime):
        return _openclaw_pre_llm_hook_package_status(resolved_runtime)
    return "invalid_runtime", f"{resolved_runtime} is not a supported OpenClaw source checkout or packaged npm runtime"


def _write_runtime_fix_backup(path: Path, content: str) -> None:
    backup = path.with_name(f"{path.name}.8mem-backup")
    if not backup.exists():
        backup.write_text(content, encoding="utf-8")


def _apply_openclaw_source_runtime_fix(source_dir: Path) -> dict[str, str]:
    source_dir = source_dir.expanduser()
    helper_path, runner_path, hook_types_path = _openclaw_source_layout(source_dir)
    hook_schema_path = source_dir / OPENCLAW_RUNTIME_HOOK_SCHEMA_RELATIVE_PATH
    root_schema_path = source_dir / OPENCLAW_RUNTIME_ROOT_SCHEMA_RELATIVE_PATH
    if not _is_openclaw_source_dir(source_dir):
        raise typer.BadParameter(
            f"{source_dir} is not a supported OpenClaw source checkout. "
            "Expected src/auto-reply/reply/agent-runner.ts and src/config/types.hooks.ts."
        )

    helper_source = _openclaw_runtime_asset_text()
    runner_source = runner_path.read_text(encoding="utf-8")
    hook_types_source = hook_types_path.read_text(encoding="utf-8")
    hook_schema_source = hook_schema_path.read_text(encoding="utf-8")
    root_schema_source = root_schema_path.read_text(encoding="utf-8")
    runner_updated = runner_source
    hook_types_updated = hook_types_source
    hook_schema_updated = hook_schema_source
    root_schema_updated = root_schema_source

    import_line = 'import { runExternalPreLlmHooks } from "./external-pre-llm-hooks.js";'
    import_anchor = 'import { createFollowupRunner } from "./followup-runner.js";'
    if import_line not in runner_updated:
        if import_anchor not in runner_updated:
            raise typer.BadParameter(f"{runner_path}: expected followup-runner import anchor was not found; no files changed.")
        runner_updated = runner_updated.replace(import_anchor, f"{import_line}\n{import_anchor}", 1)

    advisory_hook_call = """  const externalHookDirective = await runExternalPreLlmHooks({
    config: followupRun.run.config,
    message: sessionCtx.BodyStripped ?? sessionCtx.Body ?? commandBody,
    sessionKey: followupRun.run.sessionKey,
    sessionId: followupRun.run.sessionId,
    workspaceDir: followupRun.run.workspaceDir,
    sessionCtx,
  });
  if (externalHookDirective) {
    followupRun.run.extraSystemPrompt = [followupRun.run.extraSystemPrompt, externalHookDirective]
      .filter(Boolean)
      .join("\\n\\n");
  }

"""
    hook_call = """  const externalHookDirective = await runExternalPreLlmHooks({
    config: followupRun.run.config,
    message: sessionCtx.BodyStripped ?? sessionCtx.Body ?? commandBody,
    sessionKey: followupRun.run.sessionKey,
    sessionId: followupRun.run.sessionId,
    workspaceDir: followupRun.run.workspaceDir,
    sessionCtx,
  });
  if (externalHookDirective) {
    const externalHookNoMatch = externalHookDirective.match(/^\\[8mem-no-match\\]$/m);
    if (externalHookNoMatch) {
      return finalizeWithFollowup({ text: "No saved memory found." }, queueKey, runFollowupTurn);
    }
    const externalHookDirectReply = externalHookDirective.match(/^\\[8mem\\]\\s*(Saved\\.|Already in memory\\.|Already tracked\\.|Forgotten\\.|No matching memory found\\.|Updated\\.|Could not save\\. Try again\\.|Could not forget\\. Try again\\.|Could not update\\. Try again\\.)$/m)?.[1];
    if (externalHookDirectReply) {
      return finalizeWithFollowup({ text: externalHookDirectReply }, queueKey, runFollowupTurn);
    }
    followupRun.run.extraSystemPrompt = [followupRun.run.extraSystemPrompt, externalHookDirective]
      .filter(Boolean)
      .join("\\n\\n");
  }

"""
    hook_call_anchor = "  let responseUsageLine: string | undefined;"
    if "runExternalPreLlmHooks({" not in runner_updated:
        if hook_call_anchor not in runner_updated:
            raise typer.BadParameter(f"{runner_path}: expected response-usage anchor was not found; no files changed.")
        runner_updated = runner_updated.replace(hook_call_anchor, f"{hook_call}{hook_call_anchor}", 1)
    elif "externalHookDirectReply" not in runner_updated and advisory_hook_call in runner_updated:
        runner_updated = runner_updated.replace(advisory_hook_call, hook_call, 1)
    old_direct_reply_line = """    const externalHookDirectReply = externalHookDirective.match(/^\\[8mem\\]\\s*(Saved\\.|Already in memory\\.|Already tracked\\.|Forgotten\\.|No matching memory found\\.|Updated\\.|Could not save\\. Try again\\.|Could not forget\\. Try again\\.|Could not update\\. Try again\\.)$/m)?.[1];
"""
    no_match_direct_reply = """    const externalHookNoMatch = externalHookDirective.match(/^\\[8mem-no-match\\]$/m);
    if (externalHookNoMatch) {
      return finalizeWithFollowup({ text: "No saved memory found." }, queueKey, runFollowupTurn);
    }
"""
    if "externalHookNoMatch" not in runner_updated and old_direct_reply_line in runner_updated:
        runner_updated = runner_updated.replace(old_direct_reply_line, f"{no_match_direct_reply}{old_direct_reply_line}", 1)

    hook_types_block = """export type ExternalHookMatchConfig = {
  type?: "intent" | string;
  patterns?: string[];
};

export type ExternalHookEntryConfig = {
  enabled?: boolean;
  script?: string;
  trigger?: "pre_llm_call" | string;
  timeoutMs?: number;
  timeout?: number;
  match?: ExternalHookMatchConfig;
};

export type ExternalHooksConfig = {
  enabled?: boolean;
  entries?: Record<string, ExternalHookEntryConfig>;
};

"""
    hook_types_anchor = "export type HooksConfig = {"
    if "export type ExternalHooksConfig" not in hook_types_updated:
        if hook_types_anchor not in hook_types_updated:
            raise typer.BadParameter(f"{hook_types_path}: expected HooksConfig anchor was not found; no files changed.")
        hook_types_updated = hook_types_updated.replace(hook_types_anchor, f"{hook_types_block}{hook_types_anchor}", 1)

    external_property = "  /** External process hooks. pre_llm_call hooks run after queue/drop decisions and before model execution. */\n  external?: ExternalHooksConfig;"
    internal_property = "  internal?: InternalHooksConfig;"
    if "external?: ExternalHooksConfig;" not in hook_types_updated:
        if internal_property not in hook_types_updated:
            raise typer.BadParameter(f"{hook_types_path}: expected internal hooks anchor was not found; no files changed.")
        hook_types_updated = hook_types_updated.replace(internal_property, f"{internal_property}\n{external_property}", 1)

    external_schema = """export const ExternalHooksSchema = z
  .object({
    enabled: z.boolean().optional(),
    entries: z
      .record(
        z.string(),
        z
          .object({
            enabled: z.boolean().optional(),
            script: z.string().optional(),
            trigger: z.string().optional(),
            timeoutMs: z.number().int().positive().optional(),
            timeout: z.number().int().positive().optional(),
            match: z
              .object({
                type: z.string().optional(),
                patterns: z.array(z.string()).optional(),
              })
              .strict()
              .optional(),
          })
          .strict(),
      )
      .optional(),
  })
  .strict()
  .optional();

"""
    source_schema_anchor = "export const HooksGmailSchema = z"
    if "export const ExternalHooksSchema" not in hook_schema_updated:
        if source_schema_anchor not in hook_schema_updated:
            raise typer.BadParameter(f"{hook_schema_path}: expected HooksGmailSchema anchor was not found; no files changed.")
        hook_schema_updated = hook_schema_updated.replace(source_schema_anchor, f"{external_schema}{source_schema_anchor}", 1)

    source_import_anchor = 'import { HookMappingSchema, HooksGmailSchema, InternalHooksSchema } from "./zod-schema.hooks.js";'
    source_import_updated = (
        'import { ExternalHooksSchema, HookMappingSchema, HooksGmailSchema, InternalHooksSchema } '
        'from "./zod-schema.hooks.js";'
    )
    if "ExternalHooksSchema" not in root_schema_updated:
        if source_import_anchor not in root_schema_updated:
            raise typer.BadParameter(f"{root_schema_path}: expected hooks-schema import anchor was not found; no files changed.")
        root_schema_updated = root_schema_updated.replace(source_import_anchor, source_import_updated, 1)
    source_external_anchor = "        internal: InternalHooksSchema,"
    if "external: ExternalHooksSchema" not in root_schema_updated:
        if source_external_anchor not in root_schema_updated:
            raise typer.BadParameter(f"{root_schema_path}: expected internal hooks schema anchor was not found; no files changed.")
        root_schema_updated = root_schema_updated.replace(
            source_external_anchor,
            f"        external: ExternalHooksSchema,\n{source_external_anchor}",
            1,
        )

    updates = {
        helper_path: helper_source,
        runner_path: runner_updated,
        hook_types_path: hook_types_updated,
        hook_schema_path: hook_schema_updated,
        root_schema_path: root_schema_updated,
    }
    statuses: dict[str, str] = {}
    for path, updated in updates.items():
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing == updated:
            statuses[str(path.relative_to(source_dir))] = "unchanged"
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _write_runtime_fix_backup(path, existing)
        path.write_text(updated, encoding="utf-8")
        statuses[str(path.relative_to(source_dir))] = "updated"
    return statuses


def _apply_openclaw_package_runtime_fix(package_dir: Path) -> dict[str, str]:
    package_dir = package_dir.expanduser()
    runner_path = _openclaw_package_runner_path(package_dir)
    schema_path = _openclaw_package_schema_path(package_dir)
    if runner_path is None or schema_path is None:
        raise typer.BadParameter(
            f"{package_dir} is not a supported packaged OpenClaw runtime. "
            "Expected dist/agent-runner.runtime.js exporting one hashed agent-runner bundle."
        )
    helper_path = package_dir / "dist" / OPENCLAW_PACKAGE_RUNTIME_HELPER_FILE_NAME
    helper_source = _openclaw_runtime_asset_text(OPENCLAW_PACKAGE_RUNTIME_ASSET_PATH)
    runner_source = runner_path.read_text(encoding="utf-8")
    schema_source = schema_path.read_text(encoding="utf-8")
    runner_updated = runner_source
    schema_updated = schema_source

    import_line = (
        'import { runExternalPreLlmHooks as runEightmemExternalPreLlmHooks } '
        'from "./eightmem-external-pre-llm-hooks.js";'
    )
    if import_line not in runner_updated:
        if not runner_updated.startswith("import "):
            raise typer.BadParameter(f"{runner_path}: expected ESM import anchor was not found; no files changed.")
        runner_updated = f"{import_line}\n{runner_updated}"

    advisory_hook_call = """		const externalHookDirective = await runEightmemExternalPreLlmHooks({
			config: followupRun.run.config,
			message: sessionCtx.BodyStripped ?? sessionCtx.Body ?? commandBody,
			sessionKey: followupRun.run.sessionKey,
			sessionId: followupRun.run.sessionId,
			workspaceDir: followupRun.run.workspaceDir,
			sessionCtx
		});
		if (externalHookDirective) followupRun.run.extraSystemPrompt = [followupRun.run.extraSystemPrompt, externalHookDirective].filter(Boolean).join("\\n\\n");
"""
    hook_call = """		const externalHookDirective = await runEightmemExternalPreLlmHooks({
			config: followupRun.run.config,
			message: sessionCtx.BodyStripped ?? sessionCtx.Body ?? commandBody,
			sessionKey: followupRun.run.sessionKey,
			sessionId: followupRun.run.sessionId,
			workspaceDir: followupRun.run.workspaceDir,
			sessionCtx
		});
		if (externalHookDirective) {
			const externalHookNoMatch = externalHookDirective.match(/^\\[8mem-no-match\\]$/m);
			if (externalHookNoMatch) return finalizeWithFollowup({ text: "No saved memory found." }, queueKey, runFollowupTurn);
			const externalHookDirectReply = externalHookDirective.match(/^\\[8mem\\]\\s*(Saved\\.|Already in memory\\.|Already tracked\\.|Forgotten\\.|No matching memory found\\.|Updated\\.|Could not save\\. Try again\\.|Could not forget\\. Try again\\.|Could not update\\. Try again\\.)$/m)?.[1];
			if (externalHookDirectReply) return finalizeWithFollowup({ text: externalHookDirectReply }, queueKey, runFollowupTurn);
			followupRun.run.extraSystemPrompt = [followupRun.run.extraSystemPrompt, externalHookDirective].filter(Boolean).join("\\n\\n");
		}
"""
    hook_call_anchor = "\t\tlet responseUsageLine;"
    if "runEightmemExternalPreLlmHooks({" not in runner_updated:
        anchor_count = runner_updated.count(hook_call_anchor)
        if anchor_count != 1:
            raise typer.BadParameter(
                f"{runner_path}: expected one response-usage anchor, found {anchor_count}; no files changed."
            )
        runner_updated = runner_updated.replace(hook_call_anchor, f"{hook_call}{hook_call_anchor}", 1)
    elif "externalHookDirectReply" not in runner_updated and advisory_hook_call in runner_updated:
        runner_updated = runner_updated.replace(advisory_hook_call, hook_call, 1)
    old_direct_reply_line = """			const externalHookDirectReply = externalHookDirective.match(/^\\[8mem\\]\\s*(Saved\\.|Already in memory\\.|Already tracked\\.|Forgotten\\.|No matching memory found\\.|Updated\\.|Could not save\\. Try again\\.|Could not forget\\. Try again\\.|Could not update\\. Try again\\.)$/m)?.[1];
"""
    no_match_direct_reply = """			const externalHookNoMatch = externalHookDirective.match(/^\\[8mem-no-match\\]$/m);
			if (externalHookNoMatch) return finalizeWithFollowup({ text: "No saved memory found." }, queueKey, runFollowupTurn);
"""
    if "externalHookNoMatch" not in runner_updated and old_direct_reply_line in runner_updated:
        runner_updated = runner_updated.replace(old_direct_reply_line, f"{no_match_direct_reply}{old_direct_reply_line}", 1)

    external_schema = """const ExternalHooksSchema = z.object({
	enabled: z.boolean().optional(),
	entries: z.record(z.string(), z.object({
		enabled: z.boolean().optional(),
		script: z.string().optional(),
		trigger: z.string().optional(),
		timeoutMs: z.number().int().positive().optional(),
		timeout: z.number().int().positive().optional(),
		match: z.object({
			type: z.string().optional(),
			patterns: z.array(z.string()).optional()
		}).strict().optional()
	}).strict()).optional()
}).strict().optional();
"""
    package_schema_anchor = "const HooksGmailSchema = z.object({"
    if "const ExternalHooksSchema = z.object({" not in schema_updated:
        if package_schema_anchor not in schema_updated:
            raise typer.BadParameter(f"{schema_path}: expected HooksGmailSchema anchor was not found; no files changed.")
        schema_updated = schema_updated.replace(package_schema_anchor, f"{external_schema}{package_schema_anchor}", 1)
    package_external_anchor = "\t\tinternal: InternalHooksSchema"
    if "external: ExternalHooksSchema" not in schema_updated:
        anchor_count = schema_updated.count(package_external_anchor)
        if anchor_count != 1:
            raise typer.BadParameter(
                f"{schema_path}: expected one internal hooks schema anchor, found {anchor_count}; no files changed."
            )
        schema_updated = schema_updated.replace(
            package_external_anchor,
            f"\t\texternal: ExternalHooksSchema,\n{package_external_anchor}",
            1,
        )

    updates = {
        helper_path: helper_source,
        runner_path: runner_updated,
        schema_path: schema_updated,
    }
    statuses: dict[str, str] = {}
    for path, updated in updates.items():
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing == updated:
            statuses[str(path.relative_to(package_dir))] = "unchanged"
            continue
        if path.exists():
            _write_runtime_fix_backup(path, existing)
        path.write_text(updated, encoding="utf-8")
        statuses[str(path.relative_to(package_dir))] = "updated"
    return statuses


def _apply_openclaw_runtime_fix(runtime_dir: Path) -> dict[str, str]:
    runtime_dir = runtime_dir.expanduser()
    if _is_openclaw_source_dir(runtime_dir):
        return _apply_openclaw_source_runtime_fix(runtime_dir)
    if _is_openclaw_package_dir(runtime_dir):
        return _apply_openclaw_package_runtime_fix(runtime_dir)
    raise typer.BadParameter(
        f"{runtime_dir} is not a supported OpenClaw source checkout or packaged npm runtime."
    )


def _openclaw_hook_targets(config_path: Path) -> tuple[Path, str, Path, str]:
    expanded_config = config_path.expanduser()
    write_path = expanded_config.parent / "agent-hooks" / "8mem-write.sh"
    inject_path = expanded_config.parent / "agent-hooks" / "8mem-inject.sh"
    default_config = _default_openclaw_config_path().expanduser()
    if expanded_config.resolve() == default_config.resolve():
        return write_path, "~/.openclaw/agent-hooks/8mem-write.sh", inject_path, "~/.openclaw/agent-hooks/8mem-inject.sh"
    return write_path, str(write_path), inject_path, str(inject_path)


def _patch_openclaw_json(
    path: Path,
    *,
    write_hook_script: str = "~/.openclaw/agent-hooks/8mem-write.sh",
    inject_hook_script: str = "~/.openclaw/agent-hooks/8mem-inject.sh",
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_openclaw_json(path)

    channels = payload.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise typer.BadParameter(f"{path}: channels must be an object")
    telegram = channels.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        raise typer.BadParameter(f"{path}: channels.telegram must be an object")
    telegram.setdefault("enabled", True)
    capabilities = telegram.setdefault("capabilities", {})
    if not isinstance(capabilities, dict):
        raise typer.BadParameter(f"{path}: channels.telegram.capabilities must be an object")
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{path}: hooks must be an object")
    internal = hooks.setdefault("internal", {})
    if not isinstance(internal, dict):
        raise typer.BadParameter(f"{path}: hooks.internal must be an object")
    internal_entries = internal.setdefault("entries", {})
    if not isinstance(internal_entries, dict):
        raise typer.BadParameter(f"{path}: hooks.internal.entries must be an object")
    external = hooks.setdefault("external", {})
    if not isinstance(external, dict):
        raise typer.BadParameter(f"{path}: hooks.external must be an object")
    entries = external.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise typer.BadParameter(f"{path}: hooks.external.entries must be an object")
    before = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    internal_entries["session-memory"] = {
        "enabled": False,
    }
    capabilities["inlineButtons"] = "dm"
    telegram["customCommands"] = TELEGRAM_COMMANDS
    external["enabled"] = True
    entries.pop("8mem-context-inject", None)
    entries.pop("8mem-memory-write", None)
    entries["8mem-context-inject"] = {
        "enabled": True,
        "script": inject_hook_script,
        "trigger": "pre_llm_call",
        "timeoutMs": 10000,
        "match": {
            "type": "context",
            "patterns": [
                "what is my",
                "what's my",
                "what do you know",
                "summarise what you know",
                "summarize what you know",
                "do you know my",
                "do you remember",
                "what should",
                "when should",
                "how long should",
                "does a",
                "do i",
                "memory",
                "preference",
                "policy",
                "threshold",
                "/compare",
            ],
        },
    }
    entries["8mem-memory-write"] = {
        "enabled": True,
        "script": write_hook_script,
        "trigger": "pre_llm_call",
        "timeoutMs": 10000,
        "match": {
            "type": "intent",
            "patterns": [
                "remember",
                "from now on",
                "always",
                "do not",
                "forget",
                "/forget",
                "correct",
                "update",
                "my favorite",
                "my favourite",
                "i prefer",
                "i like",
                "i drink",
            ],
        },
    }
    after = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if before == after and path.exists():
        return "unchanged"
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return "updated"


def _restore_openclaw_json(
    path: Path,
    *,
    inline_buttons: dict[str, object] | None,
    session_memory: dict[str, object] | None = None,
) -> str:
    if not path.exists():
        return "missing"
    payload = _load_openclaw_json(path)
    channels = payload.get("channels")
    telegram = channels.get("telegram") if isinstance(channels, dict) else None
    capabilities = telegram.get("capabilities") if isinstance(telegram, dict) else None
    before = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if isinstance(capabilities, dict):
        if inline_buttons and inline_buttons.get("present") is True:
            capabilities["inlineButtons"] = inline_buttons.get("value")
        else:
            capabilities.pop("inlineButtons", None)
            if inline_buttons and inline_buttons.get("capabilitiesPresent") is False and not capabilities:
                telegram.pop("capabilities", None)
    hooks = payload.get("hooks")
    if isinstance(hooks, dict):
        internal = hooks.get("internal")
        if isinstance(internal, dict):
            entries = internal.get("entries")
            if isinstance(entries, dict):
                if session_memory and session_memory.get("present") is True:
                    entries["session-memory"] = session_memory.get("value")
                else:
                    entries.pop("session-memory", None)
                if session_memory and session_memory.get("entriesPresent") is False and not entries:
                    internal.pop("entries", None)
            if session_memory and session_memory.get("internalPresent") is False and not internal:
                hooks.pop("internal", None)
        external = hooks.get("external")
        if isinstance(external, dict):
            entries = external.get("entries")
            if isinstance(entries, dict):
                entries.pop("8mem-context-inject", None)
                entries.pop("8mem-memory-write", None)
                if not entries:
                    external.pop("entries", None)
        if session_memory and session_memory.get("hooksPresent") is False and not hooks:
            payload.pop("hooks", None)
    after = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    if before == after:
        return "unchanged"
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return "restored"


def _restart_openclaw_gateway() -> tuple[bool, str]:
    if not has_command("systemctl"):
        return False, "systemctl is not available. Restart OpenClaw manually."
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", OPENCLAW_GATEWAY_SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{exc}. Restart OpenClaw manually."
    if result.returncode == 0:
        return True, f"Restarted {OPENCLAW_GATEWAY_SERVICE_NAME}."
    message = (result.stderr or result.stdout).strip()
    return False, f"{message or 'restart failed'}. Restart OpenClaw manually."


def _hermes_inject_script() -> str:
    return """#!/bin/bash
set -u

CONTEXT_FILE="$HOME/.hermes/8mem-context.md"
WRITE_HOOK="$HOME/.hermes/agent-hooks/8mem-write.sh"
input=$(cat)

message=$(printf "%s" "$input" | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except Exception:
    payload = {}
extra = payload.get("extra") if isinstance(payload, dict) else {}
print(extra.get("user_message", "") if isinstance(extra, dict) else "")
' 2>/dev/null)

parsed=$(MESSAGE="$message" python3 -c '
import json
import os

text = os.environ.get("MESSAGE", "").strip()
lowered = text.lower()
action = ""
fact = ""
for candidate, prefixes in (
    ("save", ("remember ", "from now on ", "always ")),
    ("forget", ("forget ", "/forget ")),
    ("correct", ("correct that ", "correct ", "update that ", "update ")),
):
    for prefix in prefixes:
        if lowered.startswith(prefix):
            action = candidate
            fact = text[len(prefix):].strip()
            break
    if action:
        break
print(json.dumps({"action": action, "fact": fact}))
')

action=$(printf "%s" "$parsed" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("action", ""))' 2>/dev/null)
fact=$(printf "%s" "$parsed" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("fact", ""))' 2>/dev/null)
directive=""
if [ -n "$action" ] && [ -n "$fact" ] && [ -x "$WRITE_HOOK" ]; then
    directive=$(HERMES_8MEM_CONTEXT_FILE="$CONTEXT_FILE" "$WRITE_HOOK" "$action" "$fact" 2>/dev/null || true)
fi

content=""
if [ -f "$CONTEXT_FILE" ] && [ -s "$CONTEXT_FILE" ]; then
    content=$(cat "$CONTEXT_FILE")
fi

if [ -n "$directive" ]; then
    if [ -n "$content" ]; then
        content="$content

[8mem] $directive"
    else
        content="[8mem] $directive"
    fi
fi

if [ -n "$content" ]; then
    CONTENT="$content" python3 -c 'import json,os; print(json.dumps({"context": os.environ.get("CONTENT", "")}))'
else
    echo "{}"
fi
"""


def _hermes_candidate_hook_yaml() -> str:
    return """name: 8mem-candidate
description: Propose high-trust natural memory candidates to 8mem
events:
  - agent:start
"""


def _hermes_candidate_hook_handler(api_url: str, key_env_var: str) -> str:
    return f'''"""Installer-managed Hermes gateway hook for natural 8mem candidates."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

API_URL = "{api_url}"
KEY_ENV_VAR = "{key_env_var}"


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    for key in ("text", "content", "message", "body"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    return ""


async def handle(event_type: str, context: dict) -> None:
    if event_type != "agent:start":
        return
    key = _env_value(Path.home() / ".8mem" / ".env", KEY_ENV_VAR)
    message = _message_text(context.get("message"))
    if not key or not message:
        return
    payload = {{"message": message, "source": "hermes_gateway_candidate"}}
    platform = context.get("platform")
    user_id = context.get("user_id")
    if isinstance(platform, str) and platform.strip():
        payload["platform"] = platform.strip().lower()
    if payload.get("platform") == "telegram" and user_id is not None:
        payload["user_id"] = str(user_id)
        payload["telegram_chat_id"] = str(user_id)
    payload = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{{API_URL}}/v1/memory/candidate",
        data=payload,
        headers={{"Authorization": f"Bearer {{key}}", "Content-Type": "application/json"}},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3):
            return
    except Exception:
        return
'''


def _hermes_sync_script(api_url: str, key_env_var: str) -> str:
    return f"""#!/bin/bash
API_KEY=$(grep '^{key_env_var}=' "$HOME/.8mem/.env" | cut -d= -f2- | tr -d '"')
ENDPOINT="{api_url}/v1/context"
OUTPUT="$HOME/.hermes/8mem-context.md"

if [ -z "$API_KEY" ]; then
    exit 0
fi

response=$(curl -sf --connect-timeout 10 "$ENDPOINT" -H "Authorization: Bearer $API_KEY") || exit 0

echo "$response" | python3 -c \\
    "import sys, json; print(json.load(sys.stdin).get('system_prompt_injection', ''), end='')" \\
    > "$OUTPUT.tmp" 2>/dev/null || exit 0
mv "$OUTPUT.tmp" "$OUTPUT"
"""


def _hermes_soul_block() -> str:
    return """How your 8mem integration works:
- Before every response, a pre_llm_call hook injects the user's live 8mem context alongside the user message.
- That injected context is the ground truth — preferences, profile, project facts.
- Treat it as authoritative. Do not contradict it. Do not search externally to verify it.

Memory rules — follow exactly, no exceptions:
- Do NOT use the native Hermes memory tool for 8mem writes. 8mem has its own API.
- Do NOT write memory via direct curl commands.
- Explicit save, forget, and correct requests are classified and handled automatically by the system hook before you respond.
- If the injected context contains "[8mem] Saved." — your entire response is: Saved. One word. Stop.
- If the injected context contains "[8mem] Already in memory." — your entire response is: Already in memory. Stop.
- If the injected context contains "[8mem] Already tracked." — your entire response is: Already tracked. Stop.
- If the injected context contains "[8mem] Forgotten." — your entire response is: Forgotten. Stop.
- If the injected context contains "[8mem] No matching memory found." — your entire response is: No matching memory found. Stop.
- If the injected context contains "[8mem] Updated." — your entire response is: Updated. Stop.
- If the injected context contains "[8mem] Existing:" — show the question exactly as written, wait for reply. Stop.
- If the injected context contains "[8mem] Could not save" — your entire response is: Could not save — try again. Stop.
- If the injected context contains "[8mem] Could not forget" — your entire response is: Could not forget — try again. Stop.
- If the injected context contains "[8mem] Could not update" — your entire response is: Could not update — try again. Stop.
- Do NOT re-save injected 8mem context — it is a live feed, not new information.
- Only act on things genuinely new and NOT already in the injected context.

MANUAL MEMORY fallback — when no [8mem] directive is injected and the user explicitly asks to save, forget, or correct memory:
- Save: run terminal_tool with `$HOME/.hermes/agent-hooks/8mem-write.sh save "<exact fact>"`
- Forget: run terminal_tool with `$HOME/.hermes/agent-hooks/8mem-write.sh forget "<exact fact or query>"`
- Correct: run terminal_tool with `$HOME/.hermes/agent-hooks/8mem-write.sh correct "<old value -> new value or corrected text>"`
The script outputs a human-readable directive — respond with exactly that text. Stop.
- "Saved." → reply: Saved.
- "Already in memory." → reply: Already in memory.
- "Already tracked." → reply: Already tracked.
- "Forgotten." → reply: Forgotten.
- "No matching memory found." → reply: No matching memory found.
- "Updated." → reply: Updated.
- "Existing: ... / New: ... — Update existing memory? Reply yes or no." → show the question, wait for reply.
- "Existing: ... / New: ... — Which should I keep? Reply existing or new." → show the question, wait for reply.
- "Could not save. Try again." → reply: Could not save — try again.
- "Could not forget. Try again." → reply: Could not forget — try again.
- "Could not update. Try again." → reply: Could not update — try again.
ONLY for explicit memory operations — not general questions or task requests."""


def _hermes_write_script(api_url: str, key_env_var: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail

ACTION="${{1:-}}"
FACT="${{2:-}}"
DEFAULT_EIGHTMEM_ENV={shlex.quote(str(runtime_env_path()))}
EIGHTMEM_ENV="${{EIGHTMEM_ENV:-$DEFAULT_EIGHTMEM_ENV}}"
API_KEY=$(grep '^{key_env_var}=' "$EIGHTMEM_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')

if [ -z "$ACTION" ] || [ -z "$FACT" ] || [ -z "$API_KEY" ]; then
    echo "Could not save. Try again."
    exit 0
fi

_refresh_context() {{
    [ -n "${{HERMES_8MEM_CONTEXT_FILE:-}}" ] || return 0
    curl -fsS --connect-timeout 8 "{api_url}/v1/context" \\
        -H "Authorization: Bearer $API_KEY" 2>/dev/null | \\
        python3 -c 'import json,sys; print(json.load(sys.stdin).get("system_prompt_injection",""),end="")' \\
        > "${{HERMES_8MEM_CONTEXT_FILE}}.tmp" 2>/dev/null && mv "${{HERMES_8MEM_CONTEXT_FILE}}.tmp" "$HERMES_8MEM_CONTEXT_FILE" 2>/dev/null || true
}}

case "$ACTION" in
  save)
    body=$(FACT="$FACT" python3 -c 'import json, os; print(json.dumps({{"text": os.environ.get("FACT", ""), "source": "hermes_agent", "confidence": "explicit_user_statement"}}))')
    response=$(curl -s -X POST "{api_url}/v1/memory" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c \\
      "import sys,json; print(json.load(sys.stdin).get('status','error'))" \\
      2>/dev/null || echo "error")
    case "$status" in
      written)            echo "Saved."; _refresh_context & ;;
      duplicate)          echo "Already in memory." ;;
      skipped)            echo "Already tracked." ;;
      refinement_pending) existing=$(echo "$response" | python3 -c \\
                            "import sys,json; d=json.load(sys.stdin); \\
                            print(d.get('existing',{{}}).get('value','(unknown)'))" \\
                            2>/dev/null)
                          echo "Existing: \\"$existing\\" / New: \\"$FACT\\" — Update existing memory? Reply yes or no." ;;
      conflict)           existing=$(echo "$response" | python3 -c \\
                            "import sys,json; d=json.load(sys.stdin); \\
                            print(d.get('conflict',{{}}).get('value','(unknown)'))" \\
                            2>/dev/null)
                          echo "Existing: \\"$existing\\" / New: \\"$FACT\\" — Which should I keep? Reply existing or new." ;;
      *)                  echo "Could not save. Try again." ;;
    esac
    ;;
  forget)
    body=$(FACT="$FACT" python3 -c 'import json, os; print(json.dumps({{"query": os.environ.get("FACT", ""), "confirm": True}}))')
    response=$(curl -s -X POST "{api_url}/v1/forget" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    case "$status" in
      deleted)    echo "Forgotten."; _refresh_context & ;;
      not_found)  echo "No matching memory found." ;;
      *)          echo "Could not forget. Try again." ;;
    esac
    ;;
  correct)
    body=$(FACT="$FACT" python3 -c 'import json, os; raw=os.environ.get("FACT", "").strip(); old, sep, new=raw.partition(" -> "); print(json.dumps({{"text": new.strip() if sep else raw, "old_text": old.strip() if sep else "", "source_message": raw}}))')
    response=$(curl -s -X POST "{api_url}/v1/correction" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    case "$status" in
      corrected)  echo "Updated."; _refresh_context & ;;
      *)          echo "Could not update. Try again." ;;
    esac
    ;;
  *)
    echo "Could not save. Try again."
    ;;
esac
"""


def _openclaw_inject_script(api_url: str, key_env_var: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail

ACTION="${{1:-context}}"
QUERY="${{2:-}}"
DEFAULT_EIGHTMEM_ENV={shlex.quote(str(runtime_env_path()))}
EIGHTMEM_ENV="${{EIGHTMEM_ENV:-$DEFAULT_EIGHTMEM_ENV}}"
API_KEY=$(grep '^{key_env_var}=' "$EIGHTMEM_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')

if [ -z "$API_KEY" ]; then
    exit 0
fi

response=$(curl -s -f --connect-timeout 8 "{api_url}/v1/context" \\
  -H "Authorization: Bearer $API_KEY") || exit 0

context=$(printf '%s' "$response" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("system_prompt_injection",""), end="")' 2>/dev/null || true)
if [ -z "$context" ]; then
    exit 0
fi

if [ -n "${{OPENCLAW_8MEM_WORKSPACE_DIR:-}}" ]; then
    ctx_file="$OPENCLAW_8MEM_WORKSPACE_DIR/memory/8mem-context.md"
    mkdir -p "$(dirname "$ctx_file")" 2>/dev/null || true
    printf '%s' "$context" > "${{ctx_file}}.tmp" 2>/dev/null && mv "${{ctx_file}}.tmp" "$ctx_file" 2>/dev/null || true
fi

scoped_context=$(EIGHTMEM_CONTEXT_QUERY="$QUERY" EIGHTMEM_CONTEXT_TEXT="$context" python3 -c '
import os
import re

query = os.environ.get("EIGHTMEM_CONTEXT_QUERY", "").strip()
context = os.environ.get("EIGHTMEM_CONTEXT_TEXT", "").strip()
if not context:
    raise SystemExit

stopwords = {{
    "about", "above", "after", "again", "before", "does", "from", "have",
    "launch", "long", "must", "need", "needs", "over", "please", "saved",
    "should", "test", "that", "this", "value", "what", "when", "where",
    "which", "with", "would", "your",
}}
tokens = {{
    token
    for token in re.findall(r"[a-z0-9]+", query.lower())
    if len(token) >= 3 and token not in stopwords
}}
broad = any(
    phrase in query.lower()
    for phrase in (
        "what do you know about me",
        "summarise what you know about me",
        "summarize what you know about me",
        "passport",
    )
)

def limit_words(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]) + " ..."

if broad or not tokens:
    print(limit_words(context, 500), end="")
    raise SystemExit

header = []
matches = []
for line in context.splitlines():
    stripped = line.strip()
    if not stripped:
        continue
    if not stripped.startswith("- ") and len(header) < 2:
        header.append(stripped)
        continue
    line_tokens = set(re.findall(r"[a-z0-9]+", stripped.lower()))
    overlap = tokens & line_tokens
    required_overlap = 1 if len(tokens) == 1 else 2
    if len(overlap) >= required_overlap:
        if stripped not in matches:
            matches.append(stripped)

out = list(header)
if matches:
    out.append("Relevant saved memory:")
    out.extend(matches[:8])
else:
    out.append("No matching saved 8mem memory exists for this question.")
    out.append("Do not answer from older conversation history or stale session memory. Tell the user no saved memory was found.")
    out.append("[8mem-no-match]")
print(limit_words("\\n".join(out), 260), end="")
' 2>/dev/null || printf '%s' "$context")

printf 'Live 8mem context for this turn. Use this instead of older session memory when answering user-memory, policy, preference, or personal-context questions.\\n%s' "$scoped_context"
"""


def _openclaw_write_script(api_url: str, key_env_var: str) -> str:
    return f"""#!/bin/bash
set -euo pipefail

ACTION="${{1:-}}"
FACT="${{2:-}}"
DEFAULT_EIGHTMEM_ENV={shlex.quote(str(runtime_env_path()))}
EIGHTMEM_ENV="${{EIGHTMEM_ENV:-$DEFAULT_EIGHTMEM_ENV}}"
API_KEY=$(grep '^{key_env_var}=' "$EIGHTMEM_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"')

if [ -z "$ACTION" ] || [ -z "$FACT" ] || [ -z "$API_KEY" ]; then
    echo "Could not save. Try again."
    exit 0
fi

_refresh_context() {{
    [ -n "${{OPENCLAW_8MEM_WORKSPACE_DIR:-}}" ] || return 0
    local ctx_file="$OPENCLAW_8MEM_WORKSPACE_DIR/memory/8mem-context.md"
    curl -fsS --connect-timeout 8 "{api_url}/v1/context" \\
        -H "Authorization: Bearer $API_KEY" 2>/dev/null | \\
        python3 -c 'import json,sys; print(json.load(sys.stdin).get("system_prompt_injection",""),end="")' \\
        > "${{ctx_file}}.tmp" 2>/dev/null && mv "${{ctx_file}}.tmp" "$ctx_file" 2>/dev/null || true
}}

case "$ACTION" in
  save)
    body=$(FACT="$FACT" python3 -c 'import json, os; print(json.dumps({{"text": os.environ.get("FACT", ""), "source": "openclaw_agent", "confidence": "explicit_user_statement"}}))')
    response=$(curl -s -X POST "{api_url}/v1/memory" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    case "$status" in
      written)            echo "Saved."; _refresh_context & ;;
      duplicate)          echo "Already in memory." ;;
      skipped)            echo "Already tracked." ;;
      refinement_pending) existing=$(echo "$response" | python3 -c \\
                            "import sys,json; d=json.load(sys.stdin); print(d.get('existing',{{}}).get('value','(unknown)'))" \\
                            2>/dev/null)
                          echo "Existing: \\"$existing\\" / New: \\"$FACT\\" — Update existing memory? Reply yes or no." ;;
      conflict)           existing=$(echo "$response" | python3 -c \\
                            "import sys,json; d=json.load(sys.stdin); print(d.get('conflict',{{}}).get('value','(unknown)'))" \\
                            2>/dev/null)
                          echo "Existing: \\"$existing\\" / New: \\"$FACT\\" — Which should I keep? Reply existing or new." ;;
      *)                  echo "Could not save. Try again." ;;
    esac
    ;;
  forget)
    body=$(FACT="$FACT" python3 -c 'import json, os; print(json.dumps({{"query": os.environ.get("FACT", ""), "confirm": True}}))')
    response=$(curl -s -X POST "{api_url}/v1/forget" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    case "$status" in
      deleted)    echo "Forgotten."; _refresh_context & ;;
      not_found)  echo "No matching memory found." ;;
      *)          echo "Could not forget. Try again." ;;
    esac
    ;;
  correct)
    body=$(FACT="$FACT" python3 -c 'import json, os; raw=os.environ.get("FACT", "").strip(); old, sep, new=raw.partition(" -> "); print(json.dumps({{"text": new.strip() if sep else raw, "old_text": old.strip() if sep else "", "source_message": raw}}))')
    response=$(curl -s -X POST "{api_url}/v1/correction" \\
      -H "Authorization: Bearer $API_KEY" \\
      -H "Content-Type: application/json" \\
      -d "$body")
    status=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    case "$status" in
      corrected)  echo "Updated."; _refresh_context & ;;
      *)          echo "Could not update. Try again." ;;
    esac
    ;;
  *)
    echo "Could not save. Try again."
    ;;
esac
"""


def _hermes_guard_script(api_url: str, key_env_var: str) -> str:
    return f"""#!/bin/bash
input=$(cat)

tool=$(echo "$input" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {{}}
print(d.get('tool_name') or d.get('name') or '')
" 2>/dev/null)

tool_input=$(echo "$input" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {{}}
print(str(d.get('tool_input') or d.get('input') or d.get('arguments') or ''))
" 2>/dev/null)

tool_lc=$(printf "%s" "$tool" | tr '[:upper:]' '[:lower:]')
input_lc=$(printf "%s" "$tool_input" | tr '[:upper:]' '[:lower:]')

if [[ "$tool_lc" == *"terminal"* ]] || [[ "$tool_lc" == *"shell"* ]] || [[ "$tool_lc" == *"command"* ]]; then
    if [[ "$input_lc" == *"8mem dedupe"* ]] || [[ "$input_lc" == *"8mem apply-import-facts"* ]]; then
        echo '{{"action":"block","message":"Do not use maintenance or import commands to capture memory from a chat turn. For one inferred durable user fact, call POST /v1/memory/propose with terminal_tool. For explicit remember/correct/forget, use the 8mem API route.","human_description":"Blocked unsafe 8mem maintenance write path"}}'
        exit 0
    fi
fi

if [[ "$tool_lc" == *"memory"* ]]; then
    API_KEY=$(grep '^{key_env_var}=' "$HOME/.8mem/.env" | cut -d= -f2- | tr -d '"')
    fact=$(echo "$input" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {{}}
value = d.get('tool_input') or d.get('input') or d.get('arguments') or {{}}
if isinstance(value, dict):
    print(value.get('text') or value.get('fact') or value.get('memory') or value.get('query') or '')
else:
    print(str(value))
" 2>/dev/null)
    classification=$(FACT="$fact" python3 -c '
import os
text = os.environ.get("FACT", "").strip()
lc = " ".join(text.lower().split())
bulk_markers = (
    "8mem passport",
    "saved memory",
    "confirmed project context",
    "system_prompt_injection",
    "preferences",
    "corrections",
    "without memory",
    "with 8mem",
)
explicit_prefixes = (
    "remember ",
    "remember:",
    "please remember ",
    "from now on ",
    "always ",
    "never ",
    "do not ",
    "dont ",
    "i prefer ",
    "i want ",
    "i like ",
)
if len(text) > 500 or text.count("\\n") > 3 or any(marker in lc for marker in bulk_markers):
    print("bulk")
elif lc.startswith(explicit_prefixes):
    print("explicit")
else:
    print("inferred")
')
    escaped_fact=$(printf "%s" "$fact" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    if [ "$classification" = "bulk" ]; then
        echo '{{"action":"block","message":"Do not re-save existing 8mem context or passport output. Extract exactly one new user-stated fact, then call POST /v1/memory/propose with terminal_tool.","human_description":"Blocked bulk memory re-save"}}'
        exit 0
    fi
    if [ "$classification" = "explicit" ]; then
        command=$(FACT="$fact" python3 -c '
import json, os, shlex
body = json.dumps({{"text": os.environ.get("FACT", ""), "source": "hermes_agent", "confidence": "explicit_user_statement"}})
print("API_KEY=$(grep '\\''^{key_env_var}='\\'' \\"$HOME/.8mem/.env\\" | cut -d= -f2- | tr -d '\\''\\"'\\''); curl -s -X POST " + shlex.quote("{api_url}/v1/memory") + " -H \\"Authorization: Bearer $API_KEY\\" -H \\"Content-Type: application/json\\" -d " + shlex.quote(body))
')
        COMMAND="$command" python3 -c "import json, os; print(json.dumps({{'action':'block','message':'Explicit memory writes must go through 8mem. Run the provided terminal command after user approval.','human_description':'Save explicit memory to 8mem','command':os.environ['COMMAND']}}))"
        exit 0
    fi
    command=$(FACT="$fact" python3 -c '
import json, os, shlex
fact = os.environ.get("FACT", "")
body = json.dumps({{"text": fact, "source": "hermes_agent", "confidence": "inferred_durable_preference", "source_message": fact}})
print("API_KEY=$(grep '\\''^{key_env_var}='\\'' \\"$HOME/.8mem/.env\\" | cut -d= -f2- | tr -d '\\''\\"'\\''); curl -s -X POST " + shlex.quote("{api_url}/v1/memory/propose") + " -H \\"Authorization: Bearer $API_KEY\\" -H \\"Content-Type: application/json\\" -d " + shlex.quote(body))
')
    COMMAND="$command" python3 -c "import json, os; print(json.dumps({{'action':'block','message':'Inferred memory must be proposed first, not saved directly. Run the command, ask the user yes/no, then commit only if approved.','human_description':'Propose inferred memory to 8mem','command':os.environ['COMMAND']}}))"
    exit 0
fi

if [[ "$tool_lc" == *"search"* ]] || [[ "$tool_lc" == *"browser"* ]] || [[ "$tool_lc" == *"web"* ]]; then
    for term in "8mem" "openclaw" "engramspec" "engram protocol" "engramspec.org"; do
        if printf "%s" "$input_lc" | grep -qi "$term"; then
            echo '{{"action":"block","message":"Internal project - use injected 8mem context. Do not search externally for 8mem, OpenClaw, Engram, or connected agent internals.","human_description":"Blocked external search for internal 8mem context"}}'
            exit 0
        fi
    done
fi

echo "{{}}"
"""


def _hermes_plugin_init(api_url: str, key_env_var: str) -> str:
    return f'''"""8mem Hermes plugin.

Installer-managed plugin for /refresh8mem, /passport, /corrections, and /compare.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

API_URL = "{api_url}"
KEY_ENV_VAR = "{key_env_var}"


def _log_card_error(message: str) -> None:
    try:
        Path("/tmp/8mem_hermes_card_error.log").write_text(message[:2000], encoding="utf-8")
    except Exception:
        pass


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def _8mem_key() -> str:
    return _env_value(Path.home() / ".8mem" / ".env", KEY_ENV_VAR)


def _openai_key() -> str:
    return _env_value(Path.home() / ".hermes" / ".env", "OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")


def _telegram_bot_token() -> str:
    return _env_value(Path.home() / ".hermes" / ".env", "TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _telegram_chat_id() -> str:
    env_chat_id = _env_value(Path.home() / ".hermes" / ".env", "TELEGRAM_ALLOWED_USER_ID") or os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
    if env_chat_id:
        return env_chat_id
    try:
        directory = json.loads((Path.home() / ".hermes" / "channel_directory.json").read_text(encoding="utf-8"))
        telegram_entries = directory.get("platforms", {{}}).get("telegram", [])
        if isinstance(telegram_entries, list) and telegram_entries:
            chat_id = telegram_entries[0].get("id")
            return str(chat_id) if chat_id is not None else ""
    except Exception:
        return ""
    return ""


def _send_telegram_photo(png_bytes: bytes, caption: str = "") -> bool:
    token = _telegram_bot_token()
    chat_id = _telegram_chat_id()
    if not token or not chat_id or not png_bytes:
        missing = []
        if not token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not chat_id:
            missing.append("telegram chat id")
        if not png_bytes:
            missing.append("png bytes")
        _log_card_error("Missing " + ", ".join(missing))
        return False
    boundary = "----8memBoundary"
    body = (
        f"--{{boundary}}\\r\\nContent-Disposition: form-data; name=\\"chat_id\\"\\r\\n\\r\\n{{chat_id}}\\r\\n"
        f"--{{boundary}}\\r\\nContent-Disposition: form-data; name=\\"caption\\"\\r\\n\\r\\n{{caption}}\\r\\n"
        f"--{{boundary}}\\r\\nContent-Disposition: form-data; name=\\"photo\\"; filename=\\"card.png\\"\\r\\nContent-Type: image/png\\r\\n\\r\\n"
    ).encode("utf-8") + png_bytes + f"\\r\\n--{{boundary}}--\\r\\n".encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{{token}}/sendPhoto",
        data=body,
        headers={{"Content-Type": f"multipart/form-data; boundary={{boundary}}"}},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()
            return True
    except Exception as exc:
        _log_card_error(f"sendPhoto failed: {{exc}}")
        return False


def _fetch_passport_card() -> bytes:
    key = _8mem_key()
    if not key:
        return b""
    req = urllib.request.Request(
        f"{{API_URL}}/v1/passport/card",
        headers={{"Authorization": f"Bearer {{key}}"}},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read()


def _fetch_compare_card(topic: str, generic_answer: str, memory_answer: str, used_beliefs: list[str]) -> bytes:
    key = _8mem_key()
    if not key:
        return b""
    payload = json.dumps({{
        "topic": topic,
        "generic_answer": generic_answer,
        "memory_answer": memory_answer,
        "used_beliefs": used_beliefs,
    }}).encode("utf-8")
    req = urllib.request.Request(
        f"{{API_URL}}/v1/compare/card",
        data=payload,
        headers={{"Authorization": f"Bearer {{key}}", "Content-Type": "application/json"}},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read()


def _context() -> dict:
    key = _8mem_key()
    req = urllib.request.Request(f"{{API_URL}}/v1/context", headers={{"Authorization": f"Bearer {{key}}"}})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _clean_memory_value(value: str) -> str:
    value = " ".join(str(value or "").strip().split())
    if not value:
        return ""
    lower = value.lower()
    artifact_prefixes = ("user is ", "timezone:", "language:")
    if lower.startswith(artifact_prefixes):
        return ""
    if lower.startswith("deleted memory"):
        return ""
    if "/v1/" in lower or "bearer" in lower or "api key" in lower or "localhost" in lower:
        return ""
    return value


def _active_beliefs(ctx: dict, limit: int = 10) -> list[str]:
    seen = set()
    out = []
    for item in ctx.get("beliefs", []):
        if not isinstance(item, dict):
            continue
        if item.get("status", "active") != "active":
            continue
        value = _clean_memory_value(item.get("value", ""))
        if not value:
            continue
        key = value.rstrip(".").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _recent_corrections(ctx: dict, limit: int = 5) -> list[str]:
    out = []
    for item in ctx.get("corrections", []):
        if not isinstance(item, dict):
            continue
        old = _clean_memory_value(item.get("old_value", ""))
        new = _clean_memory_value(item.get("new_value", ""))
        if not old or not new:
            continue
        date = (item.get("corrected_at") or item.get("created_at") or "").split("T")[0]
        prefix = f"{{date}}: " if date else ""
        out.append(f"{{prefix}}{{old}} -> {{new}}")
    return out[-limit:]


def _format_passport(ctx: dict) -> str:
    identity = ctx.get("identity", {{}})
    if not isinstance(identity, dict):
        identity = {{}}
    name = identity.get("display_name") or identity.get("name") or "User"
    timezone = identity.get("timezone") or ""
    role = identity.get("role") or identity.get("context") or ""
    subtitle = " - ".join(part for part in [timezone, role] if part)
    lines = [f"{{name}} - 8mem passport"]
    if subtitle:
        lines.append(subtitle)
    beliefs = _active_beliefs(ctx, limit=8)
    if beliefs:
        lines.extend(["", "Currently helping with"])
        lines.extend(f"- {{item}}" for item in beliefs)
    corrections = _recent_corrections(ctx, limit=3)
    if corrections:
        lines.extend(["", "Recent corrections"])
        lines.extend(f"- {{item}}" for item in corrections)
    lines.extend([
        "",
        "Trust check",
        "- Read-only commands do not save memory.",
        "- Corrections override older memories.",
        "- Ask \\"forget <memory>\\" anytime.",
    ])
    return "\\n".join(lines) if len(lines) > 1 else "No memory found yet."


def _refresh8mem(*_args, **_kwargs) -> str:
    script = Path.home() / ".hermes" / "agent-hooks" / "8mem-sync.sh"
    result = subprocess.run([str(script)], capture_output=True, text=True, timeout=15, check=False)
    if result.returncode == 0:
        return "Refreshed."
    return "Could not refresh 8mem."


def _passport(*_args, **_kwargs) -> str:
    try:
        ctx = _context()
    except Exception:
        return "8mem is not reachable right now."
    response = _format_passport(ctx)
    try:
        _send_telegram_photo(_fetch_passport_card(), caption="8mem passport")
    except Exception:
        pass
    return response


def _corrections(*_args, **_kwargs) -> str:
    corrections = _context().get("corrections", [])
    if not corrections:
        return "No corrections recorded yet."
    out = []
    for item in corrections[:12]:
        old = item.get("old_value") or ""
        new = item.get("new_value") or "-"
        if not old:
            continue
        date = (item.get("corrected_at") or "").split("T")[0]
        out.append(f"{{old}} -> {{new}} ({{date}})")
    return "\\n".join(out) if out else "No old -> new corrections recorded yet."


def _call_openai(topic: str, context: str) -> str:
    key = _openai_key()
    if not key:
        return "OpenAI API key unavailable."
    payload = {{
        "model": "gpt-5-mini-2025-08-07",
        "messages": [
            {{"role": "system", "content": "Answer shortly and plainly."}},
            {{"role": "user", "content": f"Context:\\n{{context}}\\n\\nQuestion: {{topic}}"}},
        ],
        "max_completion_tokens": 1500,
    }}
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={{"Authorization": f"Bearer {{key}}", "Content-Type": "application/json"}},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _compare(topic: str = "", *_args, **_kwargs) -> str:
    topic = topic.strip()
    if not topic:
        return "What topic should I compare? Send /compare followed by the topic. Example: /compare should I add one more launch feature tonight"
    ctx = _context()
    memory = ctx.get("system_prompt_injection") or ""
    generic = _call_openai(topic, "")
    shaped = _call_openai(topic, memory)
    used_beliefs = [line.strip("- ").strip() for line in memory.splitlines() if line.strip().startswith("- ")][:6]
    try:
        _send_telegram_photo(_fetch_compare_card(topic, generic, shaped, used_beliefs), caption=f"8mem Compare: {{topic[:48]}}")
    except Exception:
        pass
    return f"Without memory\\n{{generic}}\\n\\nWith 8mem\\n{{shaped}}\\n\\nUsed\\n- 8mem context"


def register(registry):
    for name, handler, description in [
        ("refresh8mem", _refresh8mem, "Fetch latest 8mem context"),
        ("passport", _passport, "Show 8mem memory"),
        ("corrections", _corrections, "Show correction history"),
        ("compare", _compare, "Generic vs memory-shaped answer"),
    ]:
        if hasattr(registry, "register_command"):
            registry.register_command(name, handler, description=description)
        elif hasattr(registry, "command"):
            registry.command(name, description=description)(handler)


def setup(registry):
    register(registry)
'''


def _hermes_plugin_yaml() -> str:
    return """name: 8mem
version: 0.2.0
description: 8mem memory commands for Hermes
commands:
  - refresh8mem
  - passport
  - corrections
  - compare
  - remember
  - correct
  - forget
  - brief
  - status
  - help
"""


def _hermes_rules_block(user_name: str, user_timezone: str) -> str:
    return f"""# Hermes Agent - Behavioral Rules

You are the user's personal AI assistant running via the Hermes runtime. Preserve the agent name configured by the user or by Hermes.
Your user is {user_name}.
Timezone: {user_timezone}.

Rules:
- No emoji. Ever.
- Address the user as {user_name}.
- Default: short and direct. Match the question length.
- After remember, correct, or forget: one confirmation line only. Then stop.
- Do not offer to do more, suggest related tasks, or add follow-up questions unless asked.
- Do not offer to save memory during unrelated action requests such as email, calendar, file, shell, or search tasks.
- Memory capture only runs when the user's message itself contains an explicit remember/correct/forget intent or a clear durable personal preference/operating principle.

8mem rules:
- 8mem is {user_name}'s own memory service, not an external third-party service.
- The injected 8mem context is the ground truth for user memory.
- `/passport`, `/compare`, `/corrections`, and `/refresh8mem` are read-only commands. They must never trigger memory save, profile update, import, dedupe, or retry-save behavior.
- For `/passport`, call only the installed 8mem plugin command and return its structured text/card result. Do not echo raw injected context. Do not use the memory tool. Do not save anything from the passport output.
- For bare `/compare` with no topic, ask for a topic with one example and do not send a compare card.
- For `/compare <topic>`, call only the installed 8mem plugin command and return its text/card result. Do not use the memory tool. Do not save anything from the compare output.
- If the user asks where a user fact came from and that fact is present in injected 8mem context, answer truthfully: it came from 8mem shared memory available to this agent and may have been saved through another connected agent.
- Do not call injected 8mem context "Hermes internal memory", "assistant persistent memory", or "profile memory". Do not explain old blocked or failed write attempts as the source of an active 8mem fact.
- Explicit remember/correct/forget is classified and written by the installed pre-LLM 8mem hook before you respond.
- MANUAL MEMORY fallback: only when no `[8mem]` directive is injected and the user clearly wants a durable memory operation, run terminal_tool with `$HOME/.hermes/agent-hooks/8mem-write.sh save "<exact fact>"`, `$HOME/.hermes/agent-hooks/8mem-write.sh forget "<exact fact or query>"`, or `$HOME/.hermes/agent-hooks/8mem-write.sh correct "<old value -> new value or corrected text>"`.
- Manual memory status handling is exact: `written` -> reply exactly `Saved.` Stop; `duplicate` -> reply exactly `Already in memory.` Stop; `skipped` -> reply exactly `Already tracked.` Stop; `deleted` -> reply exactly `Forgotten.` Stop; `not_found` -> reply exactly `No matching memory found.` Stop; `corrected` -> reply exactly `Updated.` Stop; `refinement_pending` -> show existing vs new and ask whether to update; if user says no, reply exactly `Kept existing.` Stop; if user says yes, call the correction endpoint with `old_text` set to existing and only reply `Updated.` after `status: corrected`; `conflict` -> show existing vs new and ask which to keep; if user keeps existing, reply exactly `Kept existing.` Stop; if user keeps new, call the correction endpoint with `old_text` set to the existing value and only reply `Updated.` after `status: corrected`.
- Never say `Saved.`, `Updated.`, or `Forgotten.` unless the exact 8mem API status confirms it: save `written`, correction `corrected`, forget `deleted`.
- Memory operations are internal 8mem state changes and must never use the external-action approval flow.
- Do not run the manual memory fallback for general questions, task requests, meetings, one-off plans, temporary facts, or read-only commands.
- A gateway hook may place natural high-trust memory candidates into the 8mem Memory Inbox. Pending candidates are not saved active memory.
- A natural statement about a recurring preference, work boundary, operating principle, or durable decision is a memory-capture candidate. Route it to one proposal before any maintenance, import, or diagnostic command.
- Inferred durable memories must be proposed first and saved only after user approval.
- `POST /v1/memory/propose` creates a pending candidate only. It does not save active memory, so call it immediately for a durable candidate before asking the user anything.
- Do NOT use the native memory tool for inferred preferences or operating principles. Use terminal_tool to call `POST /v1/memory/propose`.
- Do NOT run `8mem dedupe`, `8mem apply-import-facts`, imports, cleanup, or database maintenance when the user states a new memory candidate. Those commands are not memory capture.
- Do NOT ask a free-form confirmation, clarification, or native approval before the propose call. Do not ask the user to specify hours unless the statement is too ambiguous to form one candidate memory.
- If the memory tool is blocked for bulk/passport/context re-save, do not retry and do not tell the user about the internal block. Continue the original read-only command.
- If the memory tool is blocked for one inferred memory candidate, retry once with terminal_tool and the propose endpoint.
- Propose exactly one new user-stated fact. Never re-save existing passport, injected context, or multiple saved memories as a batch.
- Only after propose returns `status: proposed`, ask exactly `Save this to memory? Reply yes or no.` Commit only after yes using `POST /v1/memory/commit`.
- If the user says no, deny the proposal with `decision: denied`.
- Never say memory was saved to 8mem or to a profile unless an 8mem write or approved proposal commit succeeded.
- Temporary, weak, sensitive, or internal runtime details must not be saved.
- Do not search the internet for 8mem, OpenClaw, Engram, connected agent internals, or the user's personal projects.
- Use /passport, /compare, /corrections, and /refresh8mem through the installed 8mem plugin.
- For approved email sends, use `gog gmail send --to "<recipient>" --subject "<subject>" --body "<body>"`. Do not use sendmail, msmtp, mail, or Himalaya.

{_approval_flow_block("$HOME/.hermes/.env", use_wrapper_scripts=True)}

Red lines:
- Never execute destructive actions without explicit confirmation.
- Do not invent facts about the user not present in injected 8mem context.
- Do not expose file paths, ports, hostnames, API keys, JSON payloads, or implementation details in normal replies.
"""


def _write_executable(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    status = "updated" if not path.exists() or path.read_text(encoding="utf-8") != content else "unchanged"
    if status == "updated":
        path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return status


def _write_text_if_changed(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return "unchanged"
    path.write_text(content, encoding="utf-8")
    return "updated"


def _write_env_updates(path: Path, updates: dict[str, str]) -> str:
    clean_updates = {key: value for key, value in updates.items() if value}
    if not clean_updates:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    updated_lines: list[str] = []
    changed = False
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            updated_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in clean_updates:
            new_line = f'{key}="{clean_updates[key]}"'
            updated_lines.append(new_line)
            seen.add(key)
            changed = changed or line != new_line
        else:
            updated_lines.append(line)
    for key, value in clean_updates.items():
        if key not in seen:
            updated_lines.append(f'{key}="{value}"')
            changed = True
    if not path.exists() or changed:
        path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
        return "updated"
    return "unchanged"


def _hermes_email_gog_skill() -> str:
    return """# gog Email

Use `gog` for Gmail actions. Do not use Himalaya for 8mem/Hermes installs.

Rules:
- Ask for To, Subject, and Body if any are missing.
- Require 8mem approval before sending email.
- Use the preconfigured `GOG_ACCOUNT`; do not add an `--account` flag unless the user explicitly asks.
- Send with: `gog gmail send --to "<recipient>" --subject "<subject>" --body "<body>"`
- If gog reports `invalid_grant`, say the Gmail auth expired and ask the user to re-authenticate. Do not attempt self-repair.
"""


def _disable_hermes_himalaya_skill(hermes_home: Path) -> str:
    source = hermes_home / "skills" / "email" / "himalaya"
    target = hermes_home / "skills" / "email" / "himalaya.disabled"
    if not source.exists():
        return "skipped"
    if target.exists():
        return "skipped: himalaya.disabled already exists"
    source.rename(target)
    return "disabled"


def _configure_hermes_email_environment(hermes_home: Path) -> str:
    hermes_env_path = hermes_home / ".env"
    hermes_env = _read_env_file(hermes_env_path)
    gateway_env = _read_env_file(_default_openclaw_gateway_env_path())
    updates = {
        "GOG_KEYRING_PASSWORD": hermes_env.get("GOG_KEYRING_PASSWORD")
        or gateway_env.get("GOG_KEYRING_PASSWORD")
        or os.getenv("GOG_KEYRING_PASSWORD", "").strip(),
        "GOG_ACCOUNT": hermes_env.get("GOG_ACCOUNT") or gateway_env.get("GOG_ACCOUNT") or os.getenv("GOG_ACCOUNT", "").strip(),
        "TELEGRAM_BOT_TOKEN": hermes_env.get("TELEGRAM_BOT_TOKEN")
        or os.getenv("HERMES_TELEGRAM_BOT_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "TELEGRAM_APPROVAL_CHAT_ID": hermes_env.get("TELEGRAM_APPROVAL_CHAT_ID")
        or os.getenv("HERMES_TELEGRAM_APPROVAL_CHAT_ID", "").strip(),
    }
    return _write_env_updates(hermes_env_path, updates)


def _approval_test_values(bot_token_env_file: Path, *, require_agent_chat_id: bool = False) -> tuple[str, str, str]:
    runtime_env = _read_env_file(runtime_env_path())
    bot_env = _read_env_file(bot_token_env_file)
    local_api_key = runtime_env.get("EIGHTMEM_LOCAL_API_KEY", "")
    chat_id = bot_env.get("TELEGRAM_APPROVAL_CHAT_ID") or bot_env.get("TELEGRAM_ALLOWED_USER_ID") or ""
    if not chat_id and not require_agent_chat_id:
        chat_id = runtime_env.get("TELEGRAM_APPROVAL_CHAT_ID") or runtime_env.get("TELEGRAM_ALLOWED_USER_ID") or ""
    bot_token = bot_env.get("TELEGRAM_BOT_TOKEN") or bot_env.get("HERMES_TELEGRAM_BOT_TOKEN") or ""
    return local_api_key, chat_id, bot_token


def _run_setup_approval_verification(bot_token_env_file: Path, *, timeout_seconds: int = 60, require_agent_chat_id: bool = False) -> str:
    local_api_key, chat_id, bot_token = _approval_test_values(bot_token_env_file, require_agent_chat_id=require_agent_chat_id)
    if not local_api_key:
        return "skipped: missing local API key"
    if not chat_id:
        return "skipped: missing agent Telegram approval chat id" if require_agent_chat_id else "skipped: missing Telegram approval chat id"
    if not bot_token:
        return "skipped: missing Telegram bot token"

    typer.echo("Testing approval routing — tap Approve in your Telegram chat (60s)...")
    payload = json.dumps(
        {
            "action_type": "other",
            "summary": "8mem setup — tap Approve to confirm Telegram routing works",
            "ttl_seconds": 60,
            "payload": {
                "telegram_chat_id": chat_id,
                "telegram_bot_token": bot_token,
            },
        }
    ).encode("utf-8")
    try:
        request = Request(
            "http://127.0.0.1:8787/v1/approval",
            data=payload,
            headers={"Authorization": f"Bearer {local_api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        approval = body.get("approval") if isinstance(body, dict) else None
        approval_id = approval.get("approval_id") if isinstance(approval, dict) else None
        if not isinstance(approval_id, str) or not approval_id:
            return "warning: approval test did not return an approval id"
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        return f"warning: approval test could not start: {exc}"

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(3)
        try:
            request = Request(
                f"http://127.0.0.1:8787/v1/approval/{approval_id}",
                headers={"Authorization": f"Bearer {local_api_key}"},
                method="GET",
            )
            with urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            approval = body.get("approval") if isinstance(body, dict) else None
            status = approval.get("status") if isinstance(approval, dict) else None
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            continue
        if status == "approved":
            typer.echo("Approval routing confirmed. Setup complete.")
            return "confirmed"
        if status in {"denied", "expired"}:
            return f"warning: approval test {status}"
    typer.echo("Timed out — setup done but approval routing not verified. Check Telegram bot token.")
    return "timeout"


def _patch_hermes_config(config_path: Path) -> str:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text("{}\n", encoding="utf-8")
    try:
        from ruamel.yaml import YAML
    except ImportError as exc:
        raise typer.BadParameter("Hermes setup requires ruamel.yaml. Reinstall 8mem, then rerun setup.") from exc

    yaml = YAML()
    yaml.preserve_quotes = True
    config = yaml.load(config_path) or {}
    if not isinstance(config, dict):
        raise typer.BadParameter(f"{config_path}: expected a YAML object")
    before = json.dumps(config, sort_keys=True, default=str)

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise typer.BadParameter(f"{config_path}: hooks must be a YAML object")
    inject_path = str((Path.home() / ".hermes" / "agent-hooks" / "8mem-inject.sh").expanduser())
    guard_path = str((Path.home() / ".hermes" / "agent-hooks" / "8mem-guard.sh").expanduser())
    pre_llm = hooks.setdefault("pre_llm_call", [])
    pre_tool = hooks.setdefault("pre_tool_call", [])
    if not isinstance(pre_llm, list) or not isinstance(pre_tool, list):
        raise typer.BadParameter(f"{config_path}: hooks.pre_llm_call and hooks.pre_tool_call must be lists")
    if not any(isinstance(item, dict) and item.get("command") == inject_path for item in pre_llm):
        pre_llm.append({"command": inject_path, "timeout": 5})
    if not any(isinstance(item, dict) and item.get("command") == guard_path for item in pre_tool):
        pre_tool.append({"command": guard_path, "timeout": 3})

    memory = config.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise typer.BadParameter(f"{config_path}: memory must be a YAML object")
    memory["memory_enabled"] = False
    memory["user_profile_enabled"] = False
    config["hooks_auto_accept"] = True

    aux = config.setdefault("auxiliary", {})
    if isinstance(aux, dict):
        for task in ("compression", "title_generation", "session_search"):
            task_cfg = aux.get(task)
            if isinstance(task_cfg, dict) and task_cfg.get("provider") == "openai-codex":
                task_cfg["provider"] = "custom"
                task_cfg["base_url"] = "https://api.openai.com/v1"

    plugins = config.setdefault("plugins", {})
    if isinstance(plugins, dict):
        plugins["8mem"] = True

    after = json.dumps(config, sort_keys=True, default=str)
    if before == after:
        return "unchanged"
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.dump(config, handle)
    return "updated"


def _run_systemctl_user(args: list[str]) -> tuple[bool, str]:
    if not has_command("systemctl"):
        return False, "systemctl is not available"
    try:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message


def _generate_webhook_receiver(
    port: int,
    hmac_secret_env: str,
    runtime_output_paths: list[tuple[str, str]],
) -> str:
    outputs = [
        {"runtime": runtime_name, "path": context_file_path}
        for runtime_name, context_file_path in runtime_output_paths
        if runtime_name and context_file_path
    ]
    outputs_json = json.dumps(outputs, ensure_ascii=True, indent=4)
    template = '''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path


PORT = int(os.environ.get("EIGHTMEM_AGENT_SYNC_PORT", "__PORT__"))
SECRET_ENV = "__SECRET_ENV__"
SECRET_FILE = Path(os.environ.get("EIGHTMEM_WEBHOOK_SECRET_FILE", str(Path.home() / ".8mem" / "webhook-secret.txt")))
RUNTIME_OUTPUTS = __RUNTIME_OUTPUTS__


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


for _key, _value in _read_env(Path(os.environ.get("EIGHTMEM_AGENT_SYNC_ENV_FILE", str(Path.home() / ".8mem" / "agents" / "receiver.env")))).items():
    os.environ.setdefault(_key, _value)


def _load_secret() -> str:
    env_secret = os.environ.get(SECRET_ENV, "").strip()
    if env_secret:
        return env_secret
    try:
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _verify_signature(body: bytes, sig_header: str, secret: str) -> bool:
    if not secret or not sig_header:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


def _context_from_payload(payload: dict[str, object]) -> str:
    value = payload.get("system_prompt_injection")
    if isinstance(value, str):
        return value
    context = payload.get("context")
    if isinstance(context, dict):
        nested = context.get("system_prompt_injection")
        if isinstance(nested, str):
            return nested
    return ""


def _write_runtime_context(context_text: str) -> int:
    if not context_text:
        return 0
    written = 0
    for output in RUNTIME_OUTPUTS:
        path_value = output.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            continue
        target = Path(os.path.expandvars(path_value)).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(context_text, encoding="utf-8")
            written += 1
        except OSError:
            continue
    return written


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/8mem/sync":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        signature = self.headers.get("X-8mem-Signature", "")
        if not _verify_signature(body, signature, _load_secret()):
            self.send_response(401)
            self.end_headers()
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        written = _write_runtime_context(_context_from_payload(payload if isinstance(payload, dict) else {}))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "written": written}).encode("utf-8"))

    def log_message(self, fmt: str, *args: object) -> None:
        return


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"8mem agent sync receiver listening on 0.0.0.0:{PORT}")
    server.serve_forever()
'''
    return template.replace("__PORT__", str(port)).replace("__SECRET_ENV__", hmac_secret_env).replace("__RUNTIME_OUTPUTS__", outputs_json)


def _agent_sync_receiver_env(
    *,
    api_url: str,
    key_env_var: str,
    openclaw_context_file: Path | None,
) -> str:
    lines = [
        f"EIGHTMEM_AGENT_SYNC_PORT={shlex.quote(str(AGENT_SYNC_RECEIVER_PORT))}",
        f"EIGHTMEM_WEBHOOK_SECRET_FILE={shlex.quote(str(_agent_sync_secret_path()))}",
        f"EIGHTMEM_RUNTIME_ENV_PATH={shlex.quote(str(runtime_env_path()))}",
        f"EIGHTMEM_OPENCLAW_API_URL={shlex.quote(api_url)}",
        f"EIGHTMEM_OPENCLAW_KEY_ENV_VAR={shlex.quote(key_env_var)}",
    ]
    if openclaw_context_file is not None:
        lines.append(f"EIGHTMEM_OPENCLAW_CONTEXT_FILE={shlex.quote(str(openclaw_context_file))}")
    hermes_sync = Path.home() / ".hermes" / "agent-hooks" / "8mem-sync.sh"
    lines.append(f"EIGHTMEM_HERMES_SYNC_SCRIPT={shlex.quote(str(hermes_sync))}")
    return "\n".join(lines) + "\n"


def _agent_sync_systemd_unit(receiver_path: Path, env_path: Path) -> str:
    return f"""[Unit]
Description=8mem Agent Sync Receiver
After=network-online.target

[Service]
Type=simple
EnvironmentFile={env_path}
ExecStart=/usr/bin/env python3 {receiver_path}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _agent_sync_launchd_plist(receiver_path: Path, env_path: Path) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.8mem.webhook-receiver</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>{receiver_path}</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>EIGHTMEM_AGENT_SYNC_ENV_FILE</key>
    <string>{env_path}</string>
  </dict>
</dict>
</plist>
"""


def _start_launchd_user(plist_path: Path) -> tuple[bool, str]:
    if sys.platform != "darwin" or not has_command("launchctl"):
        return False, "launchd is not available"
    try:
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True, timeout=5, check=False)
        result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    message = (result.stderr or result.stdout).strip()
    return result.returncode == 0, message


def _is_local_api_url(api_url: str) -> bool:
    parsed = urlparse(api_url)
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _detect_agent_sync_url(api_url: str) -> str | None:
    configured = os.getenv("EIGHTMEM_AGENT_SYNC_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    agent_host = os.getenv("AGENT_HOST", "").strip()
    if agent_host and (_is_local_api_url(api_url) or agent_host not in {"127.0.0.1", "localhost", "::1"}):
        return f"http://{agent_host}:{AGENT_SYNC_RECEIVER_PORT}/8mem/sync"
    if _is_local_api_url(api_url):
        return f"http://127.0.0.1:{AGENT_SYNC_RECEIVER_PORT}/8mem/sync"
    if has_command("tailscale"):
        try:
            result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        ip = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        if ip:
            return f"http://{ip}:{AGENT_SYNC_RECEIVER_PORT}/8mem/sync"
    return None


def _runtime_key_value(key_env_var: str) -> str:
    return _read_env_file(runtime_env_path()).get(key_env_var, "")


def _agent_sync_secret_value() -> str:
    secret_path = _agent_sync_secret_path()
    if secret_path.exists():
        existing = secret_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    return secrets.token_hex(32)


def _register_agent_sync_connector(
    *,
    api_url: str,
    key_env_var: str,
    connector_url: str,
    secret: str,
) -> tuple[str, str | None]:
    if _is_local_api_url(api_url):
        connector, error = register_connector(
            connector_id=AGENT_SYNC_CONNECTOR_ID,
            url=connector_url,
            description="Local agent sync receiver",
            secret=secret,
        )
        if connector is None:
            return f"warning: {error}", None
        return "registered", connector.secret

    api_key = _runtime_key_value(key_env_var)
    if not api_key:
        return f"skipped: missing {key_env_var}", None
    payload = json.dumps(
        {
            "id": AGENT_SYNC_CONNECTOR_ID,
            "label": AGENT_SYNC_CONNECTOR_ID,
            "url": connector_url,
            "description": "Local agent sync receiver",
            "secret": secret,
        }
    ).encode("utf-8")
    request = Request(
        f"{api_url.rstrip('/')}/v1/connectors",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        fallback = Request(
            f"{api_url.rstrip('/')}/v1/connectors/register",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(fallback, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            return f"warning: connector registration failed: {exc}", None
    secret = body.get("secret")
    return "registered" if isinstance(secret, str) and secret else "warning: connector registration returned no secret", secret if isinstance(secret, str) else None


def _configure_agent_sync_receiver(
    *,
    api_url: str,
    key_env_var: str,
    openclaw_context_file: Path | None,
    register_webhook: bool,
) -> dict[str, str]:
    # Clean up legacy service name before creating the canonical one
    if has_command("systemctl"):
        legacy_service_path = _systemd_user_dir() / LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME
        if legacy_service_path.exists():
            _run_systemctl_user(["stop", LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME])
            _run_systemctl_user(["disable", LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME])
            try:
                legacy_service_path.unlink(missing_ok=True)
            except OSError:
                pass

    receiver_path = _agent_sync_receiver_path()
    env_path = _agent_sync_receiver_env_path()
    secret_path = _agent_sync_secret_path()
    secret_value = _agent_sync_secret_value()
    output_paths: list[tuple[str, str]] = [("hermes", "$HOME/.hermes/8mem-context.md")]
    if openclaw_context_file is not None:
        output_paths.append(("openclaw", str(openclaw_context_file)))
    else:
        existing_openclaw_context = _read_env_file(env_path).get("EIGHTMEM_OPENCLAW_CONTEXT_FILE", "")
        if existing_openclaw_context:
            output_paths.append(("openclaw", existing_openclaw_context))

    receiver_status = _write_executable(
        receiver_path,
        _generate_webhook_receiver(
            AGENT_SYNC_RECEIVER_PORT,
            "EIGHTMEM_WEBHOOK_SECRET",
            output_paths,
        ),
    )
    env_status = _write_text_if_changed(
        env_path,
        _agent_sync_receiver_env(api_url=api_url, key_env_var=key_env_var, openclaw_context_file=openclaw_context_file),
    )
    service_status = "skipped"
    if has_command("systemctl"):
        service_path = _systemd_user_dir() / AGENT_SYNC_RECEIVER_SERVICE_NAME
        try:
            service_status = _write_text_if_changed(service_path, _agent_sync_systemd_unit(receiver_path, env_path))
        except OSError as exc:
            service_status = f"manual: {exc}"
    elif sys.platform == "darwin":
        service_path = Path.home() / "Library" / "LaunchAgents" / "com.8mem.webhook-receiver.plist"
        try:
            service_status = _write_text_if_changed(service_path, _agent_sync_launchd_plist(receiver_path, env_path))
        except OSError as exc:
            service_status = f"manual: {exc}"
    else:
        service_status = "skipped: no supported service manager"

    connector_status = "skipped"
    secret_status = "unchanged" if secret_path.exists() else "created"
    connector_url = _detect_agent_sync_url(api_url)
    if register_webhook and connector_url:
        connector_status, registered_secret = _register_agent_sync_connector(
            api_url=api_url,
            key_env_var=key_env_var,
            connector_url=connector_url,
            secret=secret_value,
        )
        if registered_secret:
            secret_path.write_text(registered_secret + "\n", encoding="utf-8")
            secret_status = "updated"
    elif register_webhook:
        connector_status = "skipped: set EIGHTMEM_AGENT_SYNC_URL or install Tailscale"

    if not secret_path.exists():
        secret_path.write_text(secret_value + "\n", encoding="utf-8")
        secret_status = "created"
    secret_path.chmod(0o600)

    if has_command("systemctl"):
        ok, message = _run_systemctl_user(["daemon-reload"])
        systemd_reload = "done" if ok else f"manual: {message}"
        ok, message = _run_systemctl_user(["enable", "--now", AGENT_SYNC_RECEIVER_SERVICE_NAME])
        systemd_service = "enabled" if ok else f"manual: {message}"
    elif sys.platform == "darwin":
        systemd_reload = "skipped: launchd"
        plist_path = Path.home() / "Library" / "LaunchAgents" / "com.8mem.webhook-receiver.plist"
        if plist_path.exists():
            ok, message = _start_launchd_user(plist_path)
            systemd_service = "started" if ok else f"manual: {message}"
        else:
            systemd_service = "manual: launchd plist was not written"
    else:
        systemd_reload = "skipped: no supported service manager"
        systemd_service = f"manual: run {_agent_sync_receiver_path()}"

    return {
        "receiver": receiver_status,
        "env": env_status,
        "secret": secret_status,
        "service": service_status,
        "systemd_reload": systemd_reload,
        "systemd_service": systemd_service,
        "connector": connector_status,
    }


def _configure_hermes(
    *,
    config_path: Path,
    project_dir: Path | None,
    api_url: str,
    key_env_var: str,
    register_webhook: bool,
    restart_gateway: bool,
) -> dict[str, str]:
    if not config_path.exists():
        raise typer.BadParameter(f"{config_path} does not exist")

    hermes_home = Path.home() / ".hermes"
    hooks_dir = hermes_home / "agent-hooks"
    candidate_hook_dir = hermes_home / "hooks" / "8mem-candidate"
    plugin_dir = hermes_home / "plugins" / "8mem"
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    sync_path = hooks_dir / "8mem-sync.sh"
    result: dict[str, str] = {
        "config": _patch_hermes_config(config_path),
        "inject": _write_executable(hooks_dir / "8mem-inject.sh", _hermes_inject_script()),
        "sync": _write_executable(sync_path, _hermes_sync_script(api_url, key_env_var)),
        "write": _write_executable(hooks_dir / "8mem-write.sh", _hermes_write_script(api_url, key_env_var)),
        "guard": _write_executable(hooks_dir / "8mem-guard.sh", _hermes_guard_script(api_url, key_env_var)),
        "approval": _write_executable(hooks_dir / "8mem-approval.sh", _hermes_approval_script()),
        "resolve": _write_executable(hooks_dir / "8mem-resolve.sh", _hermes_resolve_script()),
        "candidate_hook_yaml": _write_text_if_changed(candidate_hook_dir / "HOOK.yaml", _hermes_candidate_hook_yaml()),
        "candidate_hook_handler": _write_text_if_changed(
            candidate_hook_dir / "handler.py",
            _hermes_candidate_hook_handler(api_url, key_env_var),
        ),
        "plugin_init": _write_text_if_changed(plugin_dir / "__init__.py", _hermes_plugin_init(api_url, key_env_var)),
        "plugin_yaml": _write_text_if_changed(plugin_dir / "plugin.yaml", _hermes_plugin_yaml()),
        "env": _configure_hermes_email_environment(hermes_home),
        "gog_skill": _write_text_if_changed(hermes_home / "skills" / "email" / "gog" / "SKILL.md", _hermes_email_gog_skill()),
        "himalaya": _disable_hermes_himalaya_skill(hermes_home),
    }

    home = str(Path.home())
    result["systemd_service"] = _write_text_if_changed(
        systemd_dir / "hermes-8mem-sync.service",
        f"""[Unit]
Description=Hermes 8mem Context Sync
After=network-online.target

[Service]
Type=oneshot
ExecStart={home}/.hermes/agent-hooks/8mem-sync.sh
""",
    )
    result["systemd_timer"] = _write_text_if_changed(
        systemd_dir / "hermes-8mem-sync.timer",
        """[Unit]
Description=Hermes 8mem Context Sync every 30 minutes
Requires=hermes-8mem-sync.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
Unit=hermes-8mem-sync.service

[Install]
WantedBy=timers.target
""",
    )

    try:
        context = build_engram_context()
    except Exception:
        context = {}
    identity = context.get("identity") if isinstance(context, dict) else {}
    user_name = "the user"
    user_timezone = "the user's timezone"
    if isinstance(identity, dict):
        user_name = str(identity.get("display_name") or identity.get("name") or user_name)
        user_timezone = str(identity.get("timezone") or user_timezone)
    result["soul_md"] = _write_hermes_markdown_block(hermes_home / "SOUL.md", _hermes_soul_block())
    if project_dir is not None:
        result["hermes_md"] = _write_hermes_markdown_block(project_dir / "HERMES.md", _hermes_rules_block(user_name, user_timezone))
    else:
        result["hermes_md"] = "skipped: Hermes project directory not found"

    ok, message = _run_systemctl_user(["daemon-reload"])
    result["systemd_reload"] = "done" if ok else f"manual: {message}"
    ok, message = _run_systemctl_user(["enable", "--now", "hermes-8mem-sync.timer"])
    result["timer"] = "enabled" if ok else f"manual: {message}"
    try:
        subprocess.run([str(sync_path)], capture_output=True, text=True, timeout=15, check=False)
        result["initial_sync"] = "attempted"
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["initial_sync"] = f"manual: {exc}"

    if register_webhook:
        connector, error = register_connector(
            connector_id=HERMES_CONNECTOR_ID,
            url=f"{_default_hermes_gateway_url()}/8mem/sync",
            description="Hermes agent",
        )
        result["connector"] = "registered" if connector is not None else f"warning: {error}"
    else:
        result["connector"] = "skipped"

    result["telegram_commands"] = _register_telegram_commands_from_env(hermes_home / ".env")
    agent_sync = _configure_agent_sync_receiver(
        api_url=api_url,
        key_env_var=key_env_var,
        openclaw_context_file=None,
        register_webhook=register_webhook,
    )
    result["agent_sync"] = ", ".join(f"{key} {value}" for key, value in agent_sync.items())

    if restart_gateway:
        ok, message = _run_systemctl_user(["restart", "hermes-gateway"])
        result["restart"] = "restarted" if ok else f"manual: {message}"
    else:
        result["restart"] = "skipped"
    result["approval_test"] = _run_setup_approval_verification(hermes_home / ".env", require_agent_chat_id=True)
    return result


def _print_hermes_receipt(result: dict[str, str]) -> None:
    typer.echo("")
    typer.echo("Hermes wired:")
    typer.echo(f"  config.yaml    -> {result['config']}")
    typer.echo(f"  8mem-inject.sh -> {result['inject']}")
    typer.echo(f"  8mem-sync.sh   -> {result['sync']}")
    typer.echo(f"  8mem-write.sh  -> {result['write']}")
    typer.echo(f"  8mem-guard.sh  -> {result['guard']}")
    typer.echo(f"  approval flow  -> {result['approval']}, {result['resolve']}")
    typer.echo(f"  candidate hook -> {result['candidate_hook_yaml']}, {result['candidate_hook_handler']}")
    typer.echo(f"  plugin         -> {result['plugin_init']}, {result['plugin_yaml']}")
    typer.echo(f"  email env/skill -> {result['env']}, {result['gog_skill']}, himalaya {result['himalaya']}")
    typer.echo(f"  HERMES.md      -> {result['hermes_md']}")
    typer.echo(f"  systemd        -> {result['systemd_service']}, {result['systemd_timer']}, {result['timer']}")
    typer.echo(f"  connector      -> {result['connector']}")
    typer.echo(f"  agent sync     -> {result['agent_sync']}")
    typer.echo(f"  Telegram / menu -> {result['telegram_commands']}")
    typer.echo(f"  Hermes         -> {result['restart']}")
    typer.echo(f"  approval test  -> {result['approval_test']}")
    typer.echo("")
    typer.echo("Test it in your Hermes agent:")
    typer.echo("  /refresh8mem")
    typer.echo("  /passport")
    typer.echo("  /corrections")
    typer.echo("  /compare coffee")


def _configure_openclaw(
    *,
    workspace: Path,
    config_path: Path,
    api_url: str,
    key_env_var: str,
    restart_gateway: bool,
    register_webhook: bool,
) -> dict[str, str]:
    before_payload = _load_openclaw_json(config_path)
    inline_buttons_before = _openclaw_inline_buttons_state(before_payload)
    session_memory_before = _openclaw_session_memory_state(before_payload)
    agents_path = workspace / "AGENTS.md"
    commands_path = workspace / OPENCLAW_COMMANDS_FILE_NAME
    card_path = workspace / OPENCLAW_CARD_FILE_NAME
    legacy_commands_path = workspace / OPENCLAW_LEGACY_COMMANDS_FILE_NAME
    heartbeat_path = workspace / "HEARTBEAT.md"
    soul_path = workspace / "SOUL.md"
    memory_api_path = workspace / "MEMORY-API.md"
    write_hook_path, write_hook_script, inject_hook_path, inject_hook_script = _openclaw_hook_targets(config_path)
    agents_existed_before = agents_path.exists()
    commands_existed_before = commands_path.exists()
    card_existed_before = card_path.exists()
    legacy_commands_existed_before = legacy_commands_path.exists()
    heartbeat_existed_before = heartbeat_path.exists()
    memory_api_existed_before = memory_api_path.exists()
    inject_hook_status = _write_executable(inject_hook_path, _openclaw_inject_script(api_url, key_env_var))
    agents_content, agents_status = _managed_markdown_update(agents_path, _openclaw_agents_block(api_url, key_env_var))
    commands_content, commands_status = _managed_markdown_update(commands_path, _openclaw_agents_commands_block())
    card_content, card_status = _managed_markdown_update(card_path, _openclaw_card_block(api_url, key_env_var))
    write_hook_status = _write_executable(write_hook_path, _openclaw_write_script(api_url, key_env_var))
    legacy_commands_content, legacy_commands_changed = _remove_managed_block(
        legacy_commands_path.read_text(encoding="utf-8") if legacy_commands_path.exists() else ""
    )
    legacy_commands_status = "remove migrated block" if legacy_commands_changed else "unchanged"
    heartbeat_content, heartbeat_status = _managed_markdown_update(heartbeat_path, _openclaw_heartbeat_block(api_url, key_env_var))
    soul_content, soul_status = _managed_markdown_update(soul_path, _openclaw_soul_block())
    memory_api_content, memory_api_status = _managed_markdown_update(memory_api_path, _openclaw_memory_api_block(api_url, workspace, key_env_var))
    flow_updates: dict[str, tuple[Path, str, str]] = {}
    for file_name, block in _openclaw_memory_flow_blocks(api_url, key_env_var).items():
        path = workspace / "memory" / file_name
        content, status = _managed_markdown_update(path, block)
        flow_updates[file_name] = (path, content, status)
    _validate_openclaw_bootstrap_file_size(agents_path, agents_content)
    _validate_openclaw_bootstrap_file_size(commands_path, commands_content)
    _validate_openclaw_bootstrap_file_size(card_path, card_content)
    _validate_openclaw_bootstrap_file_size(heartbeat_path, heartbeat_content)
    _validate_openclaw_bootstrap_file_size(memory_api_path, memory_api_content)
    agents_status = _write_validated_managed_markdown(agents_path, agents_content, agents_status)
    commands_status = _write_validated_managed_markdown(commands_path, commands_content, commands_status)
    card_status = _write_validated_managed_markdown(card_path, card_content, card_status)
    if legacy_commands_changed:
        if legacy_commands_content.strip():
            legacy_commands_path.write_text(legacy_commands_content, encoding="utf-8")
            legacy_commands_status = "removed migrated block"
        else:
            legacy_commands_path.unlink()
            legacy_commands_status = "removed migrated file"
    heartbeat_status = _write_validated_managed_markdown(heartbeat_path, heartbeat_content, heartbeat_status)
    soul_status = _write_validated_managed_markdown(soul_path, soul_content, soul_status)
    memory_api_status = _write_validated_managed_markdown(memory_api_path, memory_api_content, memory_api_status)
    flow_statuses = {
        file_name: _write_validated_managed_markdown(path, content, status)
        for file_name, (path, content, status) in flow_updates.items()
    }
    agent_sync = _configure_agent_sync_receiver(
        api_url=api_url,
        key_env_var=key_env_var,
        openclaw_context_file=workspace / "memory" / "8mem-context.md",
        register_webhook=register_webhook,
    )
    config_status = _patch_openclaw_json(
        config_path,
        write_hook_script=write_hook_script,
        inject_hook_script=inject_hook_script,
    )
    _write_openclaw_manifest(
        workspace=workspace,
        config_path=config_path,
        api_url=api_url,
        inline_buttons_before=inline_buttons_before,
        session_memory_before=session_memory_before,
        agents_existed_before=agents_existed_before,
        commands_existed_before=commands_existed_before,
        card_existed_before=card_existed_before,
        legacy_commands_existed_before=legacy_commands_existed_before,
        heartbeat_existed_before=heartbeat_existed_before,
        memory_api_existed_before=memory_api_existed_before,
    )
    restart_status = "skipped"
    if restart_gateway:
        ok, message = _restart_openclaw_gateway()
        restart_status = "restarted" if ok else f"manual: {message}"
    telegram_commands_status = _register_telegram_commands_from_env(_default_openclaw_gateway_env_path())
    approval_test_status = (
        "skipped: OpenClaw Telegram runtimes may not forward inline approval callbacks; "
        "external-action approvals include manual fallback approval:approved:<id>"
    )
    return {
        "workspace": str(workspace),
        "agents": agents_status,
        "commands": commands_status,
        "card": card_status,
        "inject_hook": inject_hook_status,
        "write_hook": write_hook_status,
        "legacy_commands": legacy_commands_status,
        "heartbeat": heartbeat_status,
        "soul_md": soul_status,
        "memory_api": memory_api_status,
        "memory_flows": ", ".join(f"{name} {status}" for name, status in flow_statuses.items()),
        "agent_sync": ", ".join(f"{key} {value}" for key, value in agent_sync.items()),
        "openclaw_json": config_status,
        "telegram_commands": telegram_commands_status,
        "restart": restart_status,
        "approval_test": approval_test_status,
    }


def _openclaw_manifest_path() -> Path:
    return runtime_home() / OPENCLAW_MANIFEST_NAME


def _write_openclaw_manifest(
    *,
    workspace: Path,
    config_path: Path,
    api_url: str,
    inline_buttons_before: dict[str, object],
    session_memory_before: dict[str, object],
    agents_existed_before: bool,
    commands_existed_before: bool,
    card_existed_before: bool,
    legacy_commands_existed_before: bool,
    heartbeat_existed_before: bool,
    memory_api_existed_before: bool,
) -> None:
    path = _openclaw_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
    else:
        payload = {}
    payload.setdefault("mode", "openclaw")
    payload.setdefault("workspace", str(workspace))
    payload.setdefault("openclaw_config", str(config_path))
    payload.setdefault("api_url", api_url)
    payload.setdefault("inlineButtonsBeforeInstall", inline_buttons_before)
    payload.setdefault("sessionMemoryBeforeInstall", session_memory_before)
    payload.setdefault("agentsExistedBeforeInstall", agents_existed_before)
    payload.setdefault("memoryCommandsExistedBeforeInstall", commands_existed_before)
    payload.setdefault("memoryCardExistedBeforeInstall", card_existed_before)
    payload.setdefault("legacyCommandsExistedBeforeInstall", legacy_commands_existed_before)
    payload.setdefault("heartbeatExistedBeforeInstall", heartbeat_existed_before)
    payload.setdefault("memoryApiExistedBeforeInstall", memory_api_existed_before)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_openclaw_manifest() -> dict[str, object] | None:
    path = _openclaw_manifest_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _agent_sync_integration_present() -> bool:
    return _hermes_integration_present() or _load_openclaw_manifest() is not None


def _agent_sync_connector_check() -> dict[str, str] | None:
    if not _agent_sync_integration_present():
        return None
    connectors = [connector for connector in list_connectors(include_disabled=True) if connector.id == AGENT_SYNC_CONNECTOR_ID]
    if not connectors:
        return _doctor_check(
            "agent_sync_connector",
            "warn",
            f"Agent sync connector `{AGENT_SYNC_CONNECTOR_ID}` is not registered. Real-time memory sharing may lag until agents refresh manually. Fix: rerun `8mem setup --mode hermes` or `8mem setup --mode openclaw`.",
        )
    connector = connectors[-1]
    if not connector.enabled:
        return _doctor_check(
            "agent_sync_connector",
            "warn",
            f"Agent sync connector `{AGENT_SYNC_CONNECTOR_ID}` is disabled. Real-time memory sharing may lag until agents refresh manually. Fix: rerun setup or re-enable the connector.",
        )
    return _doctor_check("agent_sync_connector", "pass", f"Agent sync connector `{AGENT_SYNC_CONNECTOR_ID}` is registered at {connector.url}")


def _agent_sync_receiver_reachability_check() -> dict[str, str] | None:
    if not _agent_sync_integration_present():
        return None
    connectors = [connector for connector in list_connectors() if connector.id == AGENT_SYNC_CONNECTOR_ID]
    if not connectors:
        return None
    connector = connectors[-1]
    payload = {"event": "doctor.ping", "user_id": "doctor", "timestamp": int(time.time())}
    try:
        request = build_signed_request(connector, payload)
        with urlopen(request, timeout=2) as response:
            status_code = getattr(response, "status", response.getcode())
    except (OSError, URLError) as exc:
        return _doctor_check(
            "agent_sync_receiver",
            "warn",
            f"Agent sync receiver is not reachable at {connector.url}: {exc}. Real-time memory sharing may lag until agents refresh manually. Fix: start `{AGENT_SYNC_RECEIVER_SERVICE_NAME}` or rerun setup.",
        )
    if 200 <= int(status_code) < 300:
        return _doctor_check("agent_sync_receiver", "pass", f"Agent sync receiver accepted a signed health ping at {connector.url}")
    return _doctor_check(
        "agent_sync_receiver",
        "warn",
        f"Agent sync receiver returned HTTP {status_code} at {connector.url}. Real-time memory sharing may lag until agents refresh manually. Fix: restart `{AGENT_SYNC_RECEIVER_SERVICE_NAME}` or rerun setup.",
    )


def _receiver_port_conflict_check() -> dict[str, str] | None:
    if not _agent_sync_integration_present():
        return None
    if not has_command("systemctl"):
        return None
    legacy_path = _systemd_user_dir() / LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME
    if legacy_path.exists():
        return _doctor_check(
            "receiver_port_conflict",
            "warn",
            f"Legacy service `{LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME}` still exists and may hold port {AGENT_SYNC_RECEIVER_PORT}. "
            f"Fix: systemctl --user stop {LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME} && "
            f"systemctl --user disable {LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME} && "
            f"rm ~/.config/systemd/user/{LEGACY_AGENT_SYNC_RECEIVER_SERVICE_NAME} && "
            f"8mem setup",
        )
    return None


def _openclaw_approval_callbacks_check() -> dict[str, str] | None:
    if _load_openclaw_manifest() is None:
        return None
    return _doctor_check(
        "openclaw_approval_callbacks",
        "warn",
        "OpenClaw integration is present. Some OpenClaw Telegram runtimes do not forward inline approval callbacks, so Approve/Deny buttons for external actions may expire. Core 8mem memory features still work, and generated approval instructions include the manual fallback `approval:approved:<id>` or `approval:denied:<id>`. Fix: update OpenClaw to a runtime with 8mem approval callback forwarding.",
    )


def _openclaw_memory_hook_check() -> dict[str, str] | None:
    manifest = _load_openclaw_manifest()
    if manifest is None:
        return None
    config_value = manifest.get("openclaw_config")
    if not isinstance(config_value, str) or not config_value.strip():
        return _doctor_check(
            "openclaw_memory_hook",
            "warn",
            "OpenClaw integration is present, but the OpenClaw config path is missing from the 8mem manifest. "
            "Fix: rerun `8mem setup --mode openclaw`.",
        )
    config_path = Path(config_value).expanduser()
    try:
        payload = _load_openclaw_json(config_path)
    except typer.BadParameter as exc:
        return _doctor_check("openclaw_memory_hook", "warn", f"Could not inspect OpenClaw hook config: {exc}")
    hooks = payload.get("hooks")
    external = hooks.get("external") if isinstance(hooks, dict) else None
    entries = external.get("entries") if isinstance(external, dict) else None
    write_entry = entries.get("8mem-memory-write") if isinstance(entries, dict) else None
    inject_entry = entries.get("8mem-context-inject") if isinstance(entries, dict) else None
    internal = hooks.get("internal") if isinstance(hooks, dict) else None
    internal_entries = internal.get("entries") if isinstance(internal, dict) else None
    session_memory_entry = internal_entries.get("session-memory") if isinstance(internal_entries, dict) else None
    if (
        not isinstance(write_entry, dict)
        or write_entry.get("trigger") != "pre_llm_call"
        or not isinstance(inject_entry, dict)
        or inject_entry.get("trigger") != "pre_llm_call"
        or (inject_entry.get("match") if isinstance(inject_entry.get("match"), dict) else {}).get("type") != "context"
    ):
        return _doctor_check(
            "openclaw_memory_hook",
            "warn",
            "OpenClaw deterministic memory hook config is missing. Core memory APIs still work, but natural-language saves or live-memory reads may depend on model behavior. "
            "Fix: rerun `8mem setup --mode openclaw`.",
        )
    if not isinstance(session_memory_entry, dict) or session_memory_entry.get("enabled") is not False:
        return _doctor_check(
            "openclaw_memory_hook",
            "warn",
            "OpenClaw session-memory is not disabled. Stale OpenClaw session facts can compete with canonical 8mem memory. "
            "Fix: rerun `8mem setup --mode openclaw`.",
        )

    status, detail = _openclaw_pre_llm_hook_runtime_status()
    if status == "supported":
        return _doctor_check(
            "openclaw_memory_hook",
            "pass",
            f"{detail}; deterministic 8mem writes and live context injection are configured",
        )
    if status == "missing":
        return _doctor_check(
            "openclaw_memory_hook",
            "warn",
            f"{detail}. Core memory APIs still work, but deterministic natural-language remember/correct/forget needs the OpenClaw runtime integration. "
            "Fix: run `8mem runtime-fix openclaw --apply`, then rebuild or restart OpenClaw.",
        )
    return _doctor_check(
        "openclaw_memory_hook",
        "warn",
        f"OpenClaw deterministic memory hook config is installed, but runtime capability could not be verified: {detail}. "
        "Core memory APIs still work. If natural-language saves are inconsistent, update OpenClaw or run "
        "`8mem runtime-fix openclaw --apply`.",
    )


def _context_freshness_check(*, name: str, path: Path, refresh_hint: str) -> dict[str, str]:
    if not path.exists():
        return _doctor_check(name, "warn", f"Agent context file is missing at {path}. Fix: {refresh_hint}.")
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return _doctor_check(name, "warn", f"Could not inspect agent context file {path}: {exc}. Fix: {refresh_hint}.")
    if not text:
        return _doctor_check(name, "warn", f"Agent context file is empty at {path}. Fix: {refresh_hint}.")
    age_seconds = max(0.0, time.time() - stat.st_mtime)
    if age_seconds > AGENT_CONTEXT_STALE_SECONDS:
        age_minutes = int(age_seconds // 60)
        return _doctor_check(name, "warn", f"Agent context file is stale ({age_minutes} minutes old) at {path}. Fix: {refresh_hint}.")
    return _doctor_check(name, "pass", f"Agent context file is fresh at {path}")


def _openclaw_context_freshness_check() -> dict[str, str] | None:
    manifest = _load_openclaw_manifest()
    if manifest is None:
        return None
    workspace_value = manifest.get("workspace")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        return _doctor_check(
            "openclaw_context_freshness",
            "warn",
            "OpenClaw integration is present, but the installed workspace path is missing from the 8mem manifest. Fix: rerun `8mem setup --mode openclaw`.",
        )
    context_path = Path(workspace_value).expanduser() / "memory" / "8mem-context.md"
    return _context_freshness_check(
        name="openclaw_context_freshness",
        path=context_path,
        refresh_hint="send `/refresh8mem` in OpenClaw or rerun `8mem setup --mode openclaw`",
    )


def _hermes_context_freshness_check() -> dict[str, str] | None:
    if not _hermes_integration_present():
        return None
    context_path = Path.home() / ".hermes" / "8mem-context.md"
    return _context_freshness_check(
        name="hermes_context_freshness",
        path=context_path,
        refresh_hint="send `/refresh8mem` in Hermes or rerun `8mem setup --mode hermes`",
    )


def _memory_health_check() -> dict[str, str]:
    try:
        dedup = preview_memory_dedup()
        tensions = get_structured_tensions()
    except Exception as exc:
        return _doctor_check("memory_health", "warn", f"Could not inspect memory health: {exc}. Fix: run `8mem doctor` again after setup.")

    exact_count = int(dedup.get("exact_duplicate_count", 0) or 0) if isinstance(dedup, dict) else 0
    near_count = int(dedup.get("near_duplicate_count", 0) or 0) if isinstance(dedup, dict) else 0
    tension_count = len(tensions) if isinstance(tensions, list) else 0
    issues: list[str] = []
    if exact_count:
        issues.append(f"{exact_count} exact duplicate memory entr{'y' if exact_count == 1 else 'ies'}")
    if near_count:
        issues.append(f"{near_count} near-duplicate review item{'s' if near_count != 1 else ''}")
    if tension_count:
        issues.append(f"{tension_count} possible conflict{'s' if tension_count != 1 else ''}")
    if issues:
        return _doctor_check(
            "memory_health",
            "warn",
            "Memory needs review: "
            + ", ".join(issues)
            + ". No automatic cleanup was applied. Fix: review memory in the UI or use explicit correction/forget commands.",
        )
    return _doctor_check("memory_health", "pass", "No exact duplicates or structured memory conflicts detected")


def _uninstall_openclaw(
    *,
    workspace: Path,
    config_path: Path,
    restart_gateway: bool,
    inline_buttons_before: dict[str, object] | None = None,
    session_memory_before: dict[str, object] | None = None,
    agents_existed_before: bool = True,
    commands_existed_before: bool = True,
    card_existed_before: bool = True,
    legacy_commands_existed_before: bool = True,
    heartbeat_existed_before: bool = True,
    memory_api_existed_before: bool = True,
) -> dict[str, str]:
    agents_status = _remove_managed_markdown(workspace / "AGENTS.md", remove_empty_file=not agents_existed_before)
    commands_status = _remove_managed_markdown(workspace / OPENCLAW_COMMANDS_FILE_NAME, remove_empty_file=not commands_existed_before)
    card_status = _remove_managed_markdown(workspace / OPENCLAW_CARD_FILE_NAME, remove_empty_file=not card_existed_before)
    legacy_commands_status = _remove_managed_markdown(
        workspace / OPENCLAW_LEGACY_COMMANDS_FILE_NAME,
        remove_empty_file=not legacy_commands_existed_before,
    )
    heartbeat_status = _remove_managed_markdown(workspace / "HEARTBEAT.md", remove_empty_file=not heartbeat_existed_before)
    memory_api_status = _remove_managed_markdown(workspace / "MEMORY-API.md", remove_empty_file=not memory_api_existed_before)
    flow_statuses = {
        file_name: _remove_managed_markdown(workspace / "memory" / file_name, remove_empty_file=True)
        for file_name in OPENCLAW_MEMORY_FLOW_FILES
    }
    context_path = workspace / "memory" / "8mem-context.md"
    if context_path.exists():
        context_path.unlink()
        context_status = "removed"
    else:
        context_status = "missing"
    config_status = _restore_openclaw_json(
        config_path,
        inline_buttons=inline_buttons_before,
        session_memory=session_memory_before,
    )
    manifest_path = _openclaw_manifest_path()
    if manifest_path.exists():
        manifest_path.unlink()
    restart_status = "skipped"
    if restart_gateway:
        ok, message = _restart_openclaw_gateway()
        restart_status = "restarted" if ok else f"manual: {message}"
    return {
        "workspace": str(workspace),
        "agents": agents_status,
        "commands": commands_status,
        "card": card_status,
        "legacy_commands": legacy_commands_status,
        "heartbeat": heartbeat_status,
        "memory_api": memory_api_status,
        "memory_flows": ", ".join(f"{name} {status}" for name, status in flow_statuses.items()),
        "context": context_status,
        "openclaw_json": config_status,
        "restart": restart_status,
    }


def _normalize_http_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    if normalized.lower() in {"y", "yes", "n", "no"}:
        return None
    if "://" not in normalized:
        normalized = f"http://{normalized}"
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return normalized


def _safe_ollama_base_url(value: str | None) -> str:
    return _normalize_http_base_url(value) or DEFAULT_BASE_URL


def _normalize_public_https_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    if normalized.lower() in {"y", "yes", "n", "no"}:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return normalized


def _normalize_model_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() in {"y", "yes", "n", "no"}:
        return None
    if any(char.isspace() for char in normalized):
        return None
    return normalized


def _safe_model_name(value: str | None) -> str:
    return _normalize_model_name(value) or DEFAULT_MODEL


def _prompt_ollama_base_url(default_url: str, max_attempts: int = 2) -> str:
    for attempt in range(max_attempts):
        entered_base = _prompt_optional(f"Ollama base URL [{default_url}]")
        if not entered_base:
            return default_url
        normalized = _normalize_http_base_url(entered_base)
        if normalized:
            return normalized
        remaining = max_attempts - attempt - 1
        message = "Ollama URL must be like http://localhost:11434. Press Enter to use the default."
        if remaining:
            typer.secho(message, fg=typer.colors.YELLOW)
        else:
            typer.secho("Ollama setup skipped because the URL was invalid.", fg=typer.colors.YELLOW)
            return default_url
    return default_url


def _prompt_model_name(default_model: str, max_attempts: int = 2) -> str:
    for attempt in range(max_attempts):
        entered_model = _prompt_optional(f"Default local model [{default_model}]")
        if not entered_model:
            return default_model
        normalized = _normalize_model_name(entered_model)
        if normalized:
            return normalized
        remaining = max_attempts - attempt - 1
        message = "Model must look like qwen3:1.7b or qwen2.5:14b. Press Enter to use the default."
        if remaining:
            typer.secho(message, fg=typer.colors.YELLOW)
        else:
            typer.secho("Using default local model because the model name was invalid.", fg=typer.colors.YELLOW)
            return default_model
    return default_model


def _looks_like_telegram_token(token: str) -> bool:
    prefix, separator, suffix = token.partition(":")
    return bool(separator and prefix.isdigit() and len(suffix) >= 6)


def _normalize_telegram_token(token: str | None) -> str | None:
    if token is None:
        return None
    normalized = token.strip()
    if not normalized:
        return None
    return normalized if _looks_like_telegram_token(normalized) else None


def _prompt_telegram_token(max_attempts: int = 2) -> str | None:
    for attempt in range(max_attempts):
        token = _prompt_optional("Telegram bot token from BotFather", hide_input=True)
        if not token:
            typer.echo("Telegram setup: skipped. Add it later with: 8mem setup --mode telegram")
            return None
        normalized = _normalize_telegram_token(token)
        if normalized:
            return normalized
        remaining = max_attempts - attempt - 1
        if remaining:
            typer.secho("Telegram token does not look like a BotFather token. Try again or press Enter to skip.", fg=typer.colors.YELLOW)
        else:
            typer.secho("Telegram setup skipped because the token was invalid. Rerun `8mem setup` when ready.", fg=typer.colors.YELLOW)
    return None


def _prompt_telegram_forward_url(max_attempts: int = 2) -> str | None:
    typer.echo("Telegram needs a public HTTPS URL only if you want Telegram to send messages to this local 8mem server.")
    typer.echo("Examples: https://your-ngrok-domain.ngrok-free.app, https://your-device.tailnet.ts.net, or your own HTTPS domain.")
    typer.echo("Press Enter to skip webhook setup now. Add it later with: 8mem setup --mode telegram")
    for attempt in range(max_attempts):
        entered_forward = _prompt_optional("Public HTTPS URL")
        if not entered_forward:
            typer.echo("Telegram webhook: skipped. Add it later with: 8mem setup --mode telegram")
            return None
        normalized = _normalize_public_https_url(entered_forward)
        if normalized:
            return normalized
        remaining = max_attempts - attempt - 1
        message = "Telegram public URL must look like https://your-domain.example. Press Enter to skip webhook setup."
        if remaining:
            typer.secho(message, fg=typer.colors.YELLOW)
        else:
            typer.secho("Telegram webhook skipped because the public URL was invalid.", fg=typer.colors.YELLOW)
            return None
    return None


def _doctor_check(name: str, status: str, message: str) -> dict[str, str]:
    return {"name": name, "status": status, "message": message}


def _telegram_webhook_warning(info: dict[str, Any]) -> str:
    description = info.get("description")
    if isinstance(description, str) and description.strip():
        return (
            f"Telegram webhook is not registered: {description.strip()}. "
            "Fix: run `8mem setup --mode telegram` after starting your public HTTPS tunnel."
        )
    return (
        "Telegram webhook is not registered. "
        "Fix: run `8mem setup --mode telegram` after starting your public HTTPS tunnel."
    )


def _print_openclaw_receipt(result: dict[str, str], *, detected: bool) -> None:
    typer.echo("")
    typer.echo("8mem installed.")
    typer.echo("")
    if detected:
        typer.echo("OpenClaw detected - wired automatically:")
    else:
        typer.echo("OpenClaw wired:")
    typer.echo(f"  AGENTS.md     -> 8mem integration block {result['agents']}")
    typer.echo(f"  {OPENCLAW_COMMANDS_FILE_NAME} -> 8mem command rules {result['commands']}")
    typer.echo(f"  {OPENCLAW_CARD_FILE_NAME} -> 8mem card helpers {result['card']}")
    typer.echo(f"  8mem-inject.sh -> {result.get('inject_hook', 'skipped')}")
    typer.echo(f"  8mem-write.sh -> {result.get('write_hook', 'skipped')}")
    if result.get("legacy_commands") not in {None, "unchanged", "missing"}:
        typer.echo(f"  {OPENCLAW_LEGACY_COMMANDS_FILE_NAME} -> {result['legacy_commands']}")
    typer.echo(f"  HEARTBEAT.md  -> 8mem context refresh {result['heartbeat']}")
    typer.echo(f"  SOUL.md       -> 8mem hook directives {result.get('soul_md', 'skipped')}")
    typer.echo(f"  MEMORY-API.md -> 8mem command reference {result['memory_api']}")
    typer.echo(f"  memory API flows -> {result.get('memory_flows', 'skipped')}")
    typer.echo(f"  agent sync    -> {result['agent_sync']}")
    typer.echo(f"  openclaw.json -> inline buttons {result['openclaw_json']}")
    typer.echo(f"  Telegram / menu -> {result.get('telegram_commands', 'skipped')}")
    typer.echo(f"  OpenClaw      -> {result['restart']}")
    typer.echo(f"  approval test -> {result['approval_test']}")
    typer.echo("")
    typer.echo("Your agent now remembers you.")
    typer.echo("")
    typer.echo("Test it:")
    typer.echo("  /passport")
    typer.echo("  /compare coffee")
    typer.echo("  remember I prefer bullet points")
    typer.echo("")
    typer.echo("To undo:")
    typer.echo("  8mem uninstall --mode openclaw")


def _offer_openclaw_runtime_fix(*, non_interactive: bool, restart_gateway: bool) -> None:
    runtime_dir = _detect_openclaw_runtime_dir()
    status, detail = _openclaw_pre_llm_hook_runtime_status(runtime_dir)
    if status == "supported":
        typer.echo(f"  OpenClaw runtime hook -> supported ({runtime_dir})")
        return
    if runtime_dir is None:
        typer.secho(
            "  OpenClaw runtime hook -> not verified. Core 8mem features work. "
            "For deterministic natural-language saves, update OpenClaw or run "
            "`8mem runtime-fix openclaw --apply` after installing a supported OpenClaw runtime.",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(f"  OpenClaw runtime hook -> integration required ({detail})", fg=typer.colors.YELLOW)
    command = f"8mem runtime-fix openclaw --apply --runtime-dir {shlex.quote(str(runtime_dir))}"
    packaged_runtime = _is_openclaw_package_dir(runtime_dir)
    if packaged_runtime:
        typer.echo("  Installing required deterministic memory integration for packaged OpenClaw.")
    else:
        if non_interactive:
            typer.echo(f"  Source checkout detected. Apply the runtime integration explicitly: {command}")
            return
        if not typer.confirm("Apply the OpenClaw deterministic memory-hook integration to this source checkout now?", default=True):
            typer.echo(f"  Skipped. Core 8mem remains installed, but deterministic natural-language writes require: {command}")
            return
    try:
        statuses = _apply_openclaw_runtime_fix(runtime_dir)
    except (OSError, typer.BadParameter) as exc:
        typer.secho(f"  OpenClaw runtime enhancement skipped safely: {exc}", fg=typer.colors.YELLOW)
        typer.echo(f"  Core 8mem features remain installed. Retry later with: {command}")
        return
    typer.echo("  OpenClaw runtime enhancement applied:")
    for path, path_status in statuses.items():
        typer.echo(f"    {path} -> {path_status}")
    if restart_gateway:
        ok, message = _restart_openclaw_gateway()
        typer.echo(f"  OpenClaw runtime restart -> {'restarted' if ok else f'manual: {message}'}")
    verified_status, verified_detail = _openclaw_pre_llm_hook_runtime_status(runtime_dir)
    if verified_status == "supported":
        typer.echo(f"  OpenClaw runtime verification -> passed ({verified_detail})")
    else:
        typer.secho(f"  OpenClaw runtime verification -> warning ({verified_detail})", fg=typer.colors.YELLOW)
    typer.echo("  Run `8mem doctor` after rebuilding OpenClaw if your installation uses a source checkout.")


@app.command()
def setup(
    telegram_token: str | None = typer.Option(None, "--telegram-token", help="Telegram bot token from BotFather"),
    webhook_secret: str | None = typer.Option(None, "--webhook-secret", help="Telegram webhook secret"),
    telegram_forward_url: str | None = typer.Option(None, "--telegram-forward-url", help="Public HTTPS base URL for Telegram webhook"),
    llm_base_url: str | None = typer.Option(None, "--llm-base-url", help="Ollama base URL"),
    llm_model: str | None = typer.Option(None, "--llm-model", help="Default local model name"),
    runtime_api_key: str | None = typer.Option(None, "--runtime-api-key", help="Bearer key for /v1 runtime APIs"),
    mode: str | None = typer.Option(None, "--mode", help="Setup path: browser, telegram, both, openclaw, hermes, or skip"),
    openclaw_workspace: Path | None = typer.Option(None, "--openclaw-workspace", help="OpenClaw workspace directory containing AGENTS.md and HEARTBEAT.md"),
    openclaw_config: Path | None = typer.Option(None, "--openclaw-config", help="OpenClaw openclaw.json path"),
    hermes_config: Path | None = typer.Option(None, "--hermes-config", help="Hermes config.yaml path"),
    hermes_project_dir: Path | None = typer.Option(None, "--hermes-project-dir", help="Hermes project directory where HERMES.md should be written"),
    eightmem_api_url: str | None = typer.Option(None, "--eightmem-api-url", help="8mem API base URL OpenClaw should call"),
    eightmem_api_key: str | None = typer.Option(None, "--eightmem-api-key", help="Bearer key for a remote primary 8mem API; stored as EIGHTMEM_OPENCLAW_API_KEY"),
    agent_host: str | None = typer.Option(None, "--agent-host", help="Agent machine IP or hostname for webhook sync receiver"),
    restart_openclaw: bool = typer.Option(True, "--restart-openclaw/--no-restart-openclaw", help="Restart openclaw-gateway after wiring OpenClaw files"),
    restart_hermes: bool = typer.Option(True, "--restart-hermes/--no-restart-hermes", help="Restart hermes-gateway after wiring Hermes files"),
    register_connectors: bool = typer.Option(True, "--register-connectors/--no-register-connectors", help="Register wired agent connectors for real-time 8mem webhook pushes"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Do not prompt; write only supplied/default values"),
    skip_telegram: bool = typer.Option(False, "--skip-telegram", help="Initialize without Telegram config"),
    skip_llm_check: bool = typer.Option(False, "--skip-llm-check", help="Do not probe Ollama during setup"),
    register_webhook: bool = typer.Option(True, "--register-webhook/--no-register-webhook", help="Register Telegram webhook when token and public URL are provided"),
    show_status: bool = typer.Option(True, "--status/--no-status", help="Print setup status lines"),
    show_next_steps: bool = typer.Option(True, "--next-steps/--no-next-steps", help="Print setup next steps"),
) -> None:
    """Guided first-run setup for local 8mem."""
    load_project_env()
    home, mem = ensure_runtime_dirs()
    created = copy_default_templates(mem)
    telegram_keys = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_FORWARD_URL"}

    current_base_url = _safe_ollama_base_url(resolve_base_url(llm_base_url))
    current_model = _safe_model_name(resolve_default_model(llm_model))
    normalized_mode = _normalize_setup_mode(mode)
    if mode and normalized_mode is None:
        typer.secho("Invalid --mode. Use browser, telegram, both, openclaw, hermes, or skip.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    openclaw_auto_detected = bool(not non_interactive and normalized_mode is None and _standard_openclaw_config_exists())
    hermes_auto_detected = bool(not non_interactive and normalized_mode is None and _standard_hermes_config_exists())
    both_auto_detected = openclaw_auto_detected and hermes_auto_detected
    setup_mode = normalized_mode or ("openclaw" if (openclaw_auto_detected and not both_auto_detected) else "hermes" if (hermes_auto_detected and not both_auto_detected) else "browser" if non_interactive else None)
    if non_interactive and setup_mode in {"openclaw", "hermes", "skip"}:
        skip_telegram = True
        skip_llm_check = True
    clear_telegram_config = False

    if not non_interactive:
        typer.echo("8mem setup")
        typer.echo("Press Enter to accept the recommended default.")
        if both_auto_detected and normalized_mode is None:
            wire_both = typer.confirm("OpenClaw and Hermes both found. Wire 8mem into both?", default=True)
            setup_mode = "both_agents" if wire_both else _prompt_setup_mode()
        setup_mode = setup_mode or _prompt_setup_mode()
        wants_telegram = setup_mode in {"telegram", "both"} and not skip_telegram
        if setup_mode == "browser":
            skip_telegram = True
            typer.echo("Browser UI selected. You can save memory locally now. Telegram can be added later with `8mem setup --mode telegram`.")
        elif setup_mode == "telegram":
            typer.echo("Telegram selected.")
            _print_telegram_setup_prereqs()
        elif setup_mode == "both":
            typer.echo("Browser UI + Telegram selected.")
            _print_telegram_setup_prereqs()
        elif setup_mode == "both_agents":
            skip_telegram = True
            skip_llm_check = True
            typer.echo("Wiring 8mem into OpenClaw and Hermes.")
        elif setup_mode == "openclaw":
            skip_telegram = True
            skip_llm_check = True
            if not openclaw_auto_detected:
                typer.echo("OpenClaw selected. 8mem will wire memory rules into your OpenClaw workspace.")
        elif setup_mode == "hermes":
            skip_telegram = True
            skip_llm_check = True
            if not hermes_auto_detected:
                typer.echo("Hermes selected. 8mem will wire memory hooks and slash commands into Hermes.")
        else:
            skip_telegram = True
            skip_llm_check = True
            typer.echo("Optional setup skipped. You can run `8mem setup` later.")

        if wants_telegram:
            if telegram_token is None:
                telegram_token = _prompt_telegram_token()
            if telegram_token is None:
                skip_telegram = True
            else:
                if webhook_secret is None:
                    entered_secret = _prompt_optional("Telegram webhook secret (blank = generate one)")
                    webhook_secret = entered_secret or None
                if telegram_forward_url is None:
                    telegram_forward_url = _prompt_telegram_forward_url()

        if setup_mode not in {"skip", "openclaw", "hermes", "both_agents"} and not skip_llm_check:
            configure_llm = typer.confirm("Set up local Ollama now for chat replies?", default=False)
            if configure_llm:
                if llm_base_url is None:
                    current_base_url = _prompt_ollama_base_url(current_base_url)
                if llm_model is None:
                    current_model = _prompt_model_name(current_model)
            else:
                skip_llm_check = True
        if setup_mode in {"openclaw", "hermes", "both_agents"} and agent_host is None:
            agent_host = _prompt_agent_host()

    normalized_base_url = _normalize_http_base_url(current_base_url)
    if normalized_base_url is None:
        typer.secho("Ollama setup skipped because the base URL was invalid. Use a URL like http://localhost:11434.", fg=typer.colors.YELLOW)
        current_base_url = resolve_base_url(None)
        skip_llm_check = True
    else:
        current_base_url = normalized_base_url

    normalized_telegram_token = _normalize_telegram_token(telegram_token)
    if telegram_token and normalized_telegram_token is None:
        typer.secho("Telegram setup skipped because the token was blank or invalid. Rerun `8mem setup` when ready.", fg=typer.colors.YELLOW)
        skip_telegram = True
        clear_telegram_config = True
    telegram_token = normalized_telegram_token
    if not telegram_token:
        skip_telegram = True
    if telegram_forward_url and not skip_telegram:
        normalized_forward_url = _normalize_public_https_url(telegram_forward_url)
        if normalized_forward_url is None:
            typer.secho("Telegram webhook skipped because the public URL was invalid. Use a URL like https://your-domain.example.", fg=typer.colors.YELLOW)
            telegram_forward_url = None
        else:
            telegram_forward_url = normalized_forward_url

    if webhook_secret is None and not skip_telegram and telegram_token:
        webhook_secret = secrets.token_urlsafe(24)
    if runtime_api_key is None:
        runtime_api_key = os.getenv("EIGHTMEM_LOCAL_API_KEY") or secrets.token_urlsafe(32)
    normalized_agent_host = (agent_host or os.getenv("AGENT_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    normalized_openclaw_api_url: str | None = None
    normalized_openclaw_api_key = (eightmem_api_key or os.getenv("EIGHTMEM_OPENCLAW_API_KEY") or "").strip()
    if setup_mode in {"openclaw", "both_agents"}:
        normalized_openclaw_api_url = _normalize_openclaw_api_url(eightmem_api_url or os.getenv("EIGHTMEM_OPENCLAW_API_URL") or "http://127.0.0.1:8787")
        if normalized_openclaw_api_url is None:
            typer.secho("OpenClaw setup failed: --eightmem-api-url must be like http://127.0.0.1:8787.", fg=typer.colors.RED)
            raise typer.Exit(code=2)
        key_env_var = _openclaw_key_env_var_for_api_url(normalized_openclaw_api_url, normalized_openclaw_api_key)
        if key_env_var == "EIGHTMEM_OPENCLAW_API_KEY" and not normalized_openclaw_api_key:
            typer.secho(
                "OpenClaw setup failed: remote primary memory needs --eightmem-api-key or EIGHTMEM_OPENCLAW_API_KEY.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)
    if setup_mode in {"hermes", "both_agents"}:
        normalized_openclaw_api_url = _normalize_openclaw_api_url(eightmem_api_url or os.getenv("EIGHTMEM_OPENCLAW_API_URL") or "http://127.0.0.1:8787")
        if normalized_openclaw_api_url is None:
            typer.secho("Hermes setup failed: --eightmem-api-url must be like http://127.0.0.1:8787.", fg=typer.colors.RED)
            raise typer.Exit(code=2)
        key_env_var = _openclaw_key_env_var_for_api_url(normalized_openclaw_api_url, normalized_openclaw_api_key)
        if key_env_var == "EIGHTMEM_OPENCLAW_API_KEY" and not normalized_openclaw_api_key:
            typer.secho(
                "Hermes setup failed: remote primary memory needs --eightmem-api-key or EIGHTMEM_OPENCLAW_API_KEY.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)

    values = {
        "EIGHTMEM_HOME": str(home),
        "EIGHTMEM_LOCAL_API_KEY": runtime_api_key,
        "OLLAMA_BASE_URL": current_base_url,
        "DEFAULT_LLM_FALLBACK_MODEL": current_model,
        "AGENT_HOST": normalized_agent_host,
    }
    if normalized_openclaw_api_url:
        values["EIGHTMEM_OPENCLAW_API_URL"] = normalized_openclaw_api_url
    if normalized_openclaw_api_key:
        values["EIGHTMEM_OPENCLAW_API_KEY"] = normalized_openclaw_api_key
    if telegram_token and not skip_telegram:
        values["TELEGRAM_BOT_TOKEN"] = telegram_token
    if webhook_secret and not skip_telegram:
        values["TELEGRAM_WEBHOOK_SECRET"] = webhook_secret
    if telegram_forward_url and not skip_telegram:
        values["TELEGRAM_FORWARD_URL"] = telegram_forward_url

    config_path = write_runtime_env(values)
    if skip_telegram and clear_telegram_config:
        config_path = remove_runtime_env_keys(telegram_keys)
        for key in telegram_keys:
            os.environ.pop(key, None)
    for key, value in values.items():
        os.environ[key] = value
    load_project_env()

    if show_status:
        typer.echo(f"Runtime ready: {home}")
        typer.echo(f"Memory folder: {mem}")
        typer.echo(f"Config file: {config_path}")
        if created:
            typer.echo(f"Created memory templates: {len(created)}")
        else:
            typer.echo("Memory templates: already present")
    if skip_telegram:
        if show_status:
            typer.echo("Telegram setup: skipped. Add it later with: 8mem setup --mode telegram")
    elif telegram_token and telegram_forward_url and register_webhook:
        try:
            config = load_telegram_config()
            webhook_url = normalize_telegram_webhook_url(telegram_forward_url)
            result = set_telegram_webhook(config, forward_url=telegram_forward_url)
            if show_status:
                if result.get("ok"):
                    typer.echo(f"Telegram webhook: registered at {webhook_url}")
                else:
                    typer.secho(f"Telegram webhook: warning - {_telegram_webhook_warning(result)}", fg=typer.colors.YELLOW)
        except (TelegramConfigError, TelegramSendError) as exc:
            if show_status:
                typer.secho(f"Telegram webhook: warning - {exc}", fg=typer.colors.YELLOW)
    elif telegram_token and not telegram_forward_url and show_status:
        typer.secho(
            "Telegram token saved, but Telegram is not active yet because no public HTTPS URL was provided.",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Browser/local memory works now. Add the Telegram webhook later with: 8mem setup --mode telegram")

    if show_status:
        if skip_llm_check:
            typer.echo("LLM check: skipped")
        else:
            ok, error = check_ollama_backend(base_url=current_base_url)
            if ok:
                typer.echo("LLM check: passed")
            else:
                typer.secho(f"LLM check: warning - {error}", fg=typer.colors.YELLOW)

    if show_next_steps:
        typer.echo("")
        typer.echo("What to do next:")
        typer.echo("1. Run: 8mem doctor")
        typer.echo("2. Run: 8mem start")
        typer.echo("3. Open browser UI: http://127.0.0.1:8787/")
        typer.echo("4. Save your first memory from Start Here -> Remember this")
        typer.echo("")
        typer.echo("What works now:")
        typer.echo("- Browser dashboard, memory save, memory mirror, import, and local memory test")
        if skip_telegram:
            typer.echo("- Telegram is not configured yet. Add it later with: 8mem setup --mode telegram")
        if telegram_token:
            if telegram_forward_url:
                typer.echo("- Telegram webhook is configured. Start 8mem, then send a message to your bot.")
            else:
                typer.echo("- Telegram token is saved, but Telegram will not reply until you add a public HTTPS URL.")
                typer.echo("  When your ngrok/Tailscale/Cloudflare URL is ready, rerun: 8mem setup --mode telegram")
        typer.echo("")
        typer.echo("Agent runtimes:")
        openclaw_detected_now = _standard_openclaw_config_exists()
        hermes_detected_now = _standard_hermes_config_exists()
        if setup_mode in {"openclaw", "hermes", "both_agents"}:
            typer.echo("- Agent wiring selected. See setup receipt below.")
        else:
            if openclaw_detected_now:
                typer.echo("- OpenClaw detected. To connect it later, run: 8mem setup --mode openclaw")
            else:
                typer.echo("- OpenClaw not detected. If you install OpenClaw later, run: 8mem setup --mode openclaw")
            if hermes_detected_now:
                typer.echo("- Hermes detected. To connect it later, run: 8mem setup --mode hermes")
            else:
                typer.echo("- Hermes not detected. If you install Hermes later, run: 8mem setup --mode hermes")
        typer.echo("")
        typer.echo(f"Advanced: local API key is stored in {config_path}")
    if setup_mode in {"openclaw", "both_agents"}:
        api_url = normalized_openclaw_api_url or "http://127.0.0.1:8787"
        key_env_var = _openclaw_key_env_var_for_api_url(api_url, normalized_openclaw_api_key)
        config_path = openclaw_config.expanduser() if openclaw_config is not None else _default_openclaw_config_path()
        workspace = _normalize_openclaw_workspace(openclaw_workspace, config_path=config_path)
        _offer_openclaw_runtime_fix(non_interactive=non_interactive, restart_gateway=False)
        try:
            openclaw_result = _configure_openclaw(
                workspace=workspace,
                config_path=config_path,
                api_url=api_url,
                key_env_var=key_env_var,
                restart_gateway=restart_openclaw,
                register_webhook=register_connectors,
            )
        except typer.BadParameter as exc:
            typer.secho(f"OpenClaw setup failed: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
        _print_openclaw_receipt(openclaw_result, detected=openclaw_auto_detected)
    if setup_mode in {"hermes", "both_agents"}:
        api_url = normalized_openclaw_api_url or "http://127.0.0.1:8787"
        key_env_var = _openclaw_key_env_var_for_api_url(api_url, normalized_openclaw_api_key)
        config_path = hermes_config.expanduser() if hermes_config is not None else _default_hermes_config_path()
        project_dir = hermes_project_dir.expanduser() if hermes_project_dir is not None else _detect_hermes_project_dir()
        try:
            hermes_result = _configure_hermes(
                config_path=config_path,
                project_dir=project_dir,
                api_url=api_url,
                key_env_var=key_env_var,
                register_webhook=register_connectors,
                restart_gateway=restart_hermes,
            )
        except typer.BadParameter as exc:
            typer.secho(f"Hermes setup failed: {exc}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
        _print_hermes_receipt(hermes_result)


@app.command("runtime-fix")
def runtime_fix(
    target: str = typer.Argument(..., help="Runtime target: openclaw"),
    runtime_dir: Path | None = typer.Option(
        None,
        "--runtime-dir",
        "--source-dir",
        help="OpenClaw source checkout or packaged npm runtime to inspect or enhance",
    ),
    apply_fix: bool = typer.Option(False, "--apply", help="Apply the runtime integration after validation"),
    restart_openclaw: bool = typer.Option(True, "--restart-openclaw/--no-restart-openclaw", help="Restart openclaw-gateway after applying the enhancement"),
) -> None:
    """Inspect or explicitly apply validated third-party runtime integrations."""
    if target.strip().lower() != "openclaw":
        typer.secho("Invalid runtime target. Use openclaw.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    resolved_runtime = runtime_dir.expanduser() if runtime_dir is not None else _detect_openclaw_runtime_dir()
    if resolved_runtime is None:
        typer.secho(
            "OpenClaw source checkout or packaged npm runtime was not detected. "
            "Pass `--runtime-dir /path/to/openclaw`. "
            "Core 8mem memory APIs still work, but deterministic natural-language writes need this runtime integration.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=2)
    status, detail = _openclaw_pre_llm_hook_runtime_status(resolved_runtime)
    typer.echo(detail)
    if status == "supported":
        typer.echo("OpenClaw deterministic memory-hook support is already present.")
        return
    if not apply_fix:
        typer.echo(f"Preview only. Apply with: 8mem runtime-fix openclaw --apply --runtime-dir {shlex.quote(str(resolved_runtime))}")
        return
    try:
        statuses = _apply_openclaw_runtime_fix(resolved_runtime)
    except (OSError, typer.BadParameter) as exc:
        typer.secho(f"OpenClaw runtime enhancement failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    typer.echo("OpenClaw runtime enhancement applied:")
    for path, path_status in statuses.items():
        typer.echo(f"  {path} -> {path_status}")
    if restart_openclaw:
        ok, message = _restart_openclaw_gateway()
        typer.echo(f"OpenClaw restart -> {'restarted' if ok else f'manual: {message}'}")
    typer.echo("Run `8mem doctor`. Rebuild OpenClaw first if your installation uses a source checkout.")


@app.command("uninstall")
def uninstall(
    mode: str = typer.Option(..., "--mode", help="Uninstall target: openclaw"),
    openclaw_workspace: Path | None = typer.Option(None, "--openclaw-workspace", help="OpenClaw workspace directory"),
    openclaw_config: Path | None = typer.Option(None, "--openclaw-config", help="OpenClaw openclaw.json path"),
    restart_openclaw: bool = typer.Option(True, "--restart-openclaw/--no-restart-openclaw", help="Restart openclaw-gateway after uninstall"),
) -> None:
    """Remove 8mem integration from a supported target."""
    normalized_mode = _normalize_setup_mode(mode)
    if normalized_mode != "openclaw":
        typer.secho("Invalid --mode. Use openclaw.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    load_project_env()
    ensure_runtime_dirs()
    manifest = _load_openclaw_manifest()
    manifest_workspace = manifest.get("workspace") if manifest else None
    manifest_config = manifest.get("openclaw_config") if manifest else None
    inline_buttons_before = manifest.get("inlineButtonsBeforeInstall") if manifest else None
    session_memory_before = manifest.get("sessionMemoryBeforeInstall") if manifest else None
    agents_existed_before = manifest.get("agentsExistedBeforeInstall") if manifest else None
    commands_existed_before = manifest.get("memoryCommandsExistedBeforeInstall") if manifest else None
    card_existed_before = manifest.get("memoryCardExistedBeforeInstall") if manifest else None
    legacy_commands_existed_before = manifest.get("legacyCommandsExistedBeforeInstall") if manifest else None
    if legacy_commands_existed_before is None and manifest:
        legacy_commands_existed_before = manifest.get("commandsExistedBeforeInstall")
    heartbeat_existed_before = manifest.get("heartbeatExistedBeforeInstall") if manifest else None
    memory_api_existed_before = manifest.get("memoryApiExistedBeforeInstall") if manifest else None
    config_path = (
        openclaw_config.expanduser()
        if openclaw_config is not None
        else Path(str(manifest_config)).expanduser()
        if isinstance(manifest_config, str) and manifest_config
        else _default_openclaw_config_path()
    )
    workspace = (
        _normalize_openclaw_workspace(openclaw_workspace, config_path=config_path)
        if openclaw_workspace is not None
        else Path(str(manifest_workspace)).expanduser()
        if isinstance(manifest_workspace, str) and manifest_workspace
        else _normalize_openclaw_workspace(None, config_path=config_path)
    )
    result = _uninstall_openclaw(
        workspace=workspace,
        config_path=config_path,
        restart_gateway=restart_openclaw,
        inline_buttons_before=inline_buttons_before if isinstance(inline_buttons_before, dict) else None,
        session_memory_before=session_memory_before if isinstance(session_memory_before, dict) else None,
        agents_existed_before=agents_existed_before if isinstance(agents_existed_before, bool) else True,
        commands_existed_before=commands_existed_before if isinstance(commands_existed_before, bool) else True,
        card_existed_before=card_existed_before if isinstance(card_existed_before, bool) else True,
        legacy_commands_existed_before=legacy_commands_existed_before if isinstance(legacy_commands_existed_before, bool) else True,
        heartbeat_existed_before=heartbeat_existed_before if isinstance(heartbeat_existed_before, bool) else True,
        memory_api_existed_before=memory_api_existed_before if isinstance(memory_api_existed_before, bool) else True,
    )
    typer.echo("8mem OpenClaw integration removed:")
    typer.echo(f"  AGENTS.md     -> {result['agents']}")
    typer.echo(f"  {OPENCLAW_COMMANDS_FILE_NAME} -> {result['commands']}")
    typer.echo(f"  {OPENCLAW_CARD_FILE_NAME} -> {result['card']}")
    typer.echo(f"  {OPENCLAW_LEGACY_COMMANDS_FILE_NAME} -> {result['legacy_commands']}")
    typer.echo(f"  HEARTBEAT.md  -> {result['heartbeat']}")
    typer.echo(f"  MEMORY-API.md -> {result['memory_api']}")
    typer.echo(f"  memory API flows -> {result.get('memory_flows', 'skipped')}")
    typer.echo(f"  context file  -> {result['context']}")
    typer.echo(f"  openclaw.json -> {result['openclaw_json']}")
    typer.echo(f"  OpenClaw      -> {result['restart']}")


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    skip_llm_check: bool = typer.Option(False, "--skip-llm-check", help="Do not probe Ollama"),
) -> None:
    """Diagnose whether local 8mem is ready to run."""
    load_project_env()
    checks: list[dict[str, str]] = []
    home: Path | None = None
    mem: Path | None = None

    try:
        home, mem = ensure_runtime_dirs()
        checks.append(_doctor_check("runtime_home", "pass", str(home)))
    except Exception as exc:
        checks.append(
            _doctor_check(
                "runtime_home",
                "fail",
                f"Cannot create runtime home: {exc}. Fix: check write permissions or set EIGHTMEM_HOME to a writable directory.",
            )
        )

    if mem is not None:
        created = copy_default_templates(mem)
        files = sorted(path.name for path in mem.glob("*.md"))
        if files:
            message = f"{len(files)} memory files present"
            if created:
                message += f"; created {len(created)} missing templates"
            checks.append(_doctor_check("memory_files", "pass", message))
        else:
            checks.append(_doctor_check("memory_files", "fail", "No memory markdown files found. Fix: run `8mem init` or `8mem setup`."))

    config_path = runtime_env_path()
    if config_path.exists():
        checks.append(_doctor_check("runtime_config", "pass", str(config_path)))
    else:
        checks.append(_doctor_check("runtime_config", "warn", f"Missing {config_path}. Fix: run `8mem setup`."))

    try:
        config = load_telegram_config()
        if _looks_like_telegram_token(config.bot_token):
            checks.append(_doctor_check("telegram", "pass", "Telegram bot token configured"))
        else:
            checks.append(
                _doctor_check(
                    "telegram",
                    "warn",
                    "Invalid TELEGRAM_BOT_TOKEN format. Fix: run `8mem setup --mode telegram` when you have a BotFather token.",
                )
            )
    except TelegramConfigError as exc:
        checks.append(
            _doctor_check(
                "telegram",
                "warn",
                f"{exc}. Fix: run `8mem setup --mode telegram`, or ignore this if you only use the local UI/OpenClaw integration.",
            )
        )

    forward_url = os.getenv("TELEGRAM_FORWARD_URL", "").strip()
    if forward_url:
        try:
            config = load_telegram_config()
            expected_url = normalize_telegram_webhook_url(forward_url)
            info = get_telegram_webhook_info(config)
            actual_url = ""
            if isinstance(info.get("result"), dict):
                actual = info["result"].get("url")
                actual_url = actual if isinstance(actual, str) else ""
            if info.get("ok") and actual_url == expected_url:
                checks.append(_doctor_check("telegram_webhook", "pass", f"Webhook registered at {actual_url}"))
            elif info.get("ok") and actual_url:
                checks.append(
                    _doctor_check(
                        "telegram_webhook",
                        "warn",
                        f"Webhook points to {actual_url}, expected {expected_url}. Fix: rerun `8mem setup --mode telegram` with the current public HTTPS URL.",
                    )
                )
            else:
                checks.append(
                    _doctor_check(
                        "telegram_webhook",
                        "warn",
                        _telegram_webhook_warning(info),
                    )
                )
        except (TelegramConfigError, TelegramSendError) as exc:
            checks.append(_doctor_check("telegram_webhook", "warn", f"{exc}. Fix: check the bot token and public HTTPS URL, then rerun `8mem setup --mode telegram`."))
    else:
        checks.append(
            _doctor_check(
                "telegram_webhook",
                "warn",
                "Missing TELEGRAM_FORWARD_URL; inbound Telegram messages will not reach this local server. Fix: run `8mem setup --mode telegram` with an ngrok, Tailscale HTTPS, or domain URL. Ignore if using local UI/OpenClaw only.",
            )
        )

    api_key = os.getenv("EIGHTMEM_LOCAL_API_KEY", "local_key")
    if api_key == "local_key":
        checks.append(_doctor_check("runtime_api_auth", "warn", "Using default local_key; acceptable for local/internal use only. Fix: run `8mem setup` before exposing the runtime API."))
    else:
        checks.append(_doctor_check("runtime_api_auth", "pass", "Runtime API bearer key configured"))

    hermes_callback_check = _hermes_approval_callbacks_check()
    if hermes_callback_check is not None:
        checks.append(hermes_callback_check)
    openclaw_callback_check = _openclaw_approval_callbacks_check()
    if openclaw_callback_check is not None:
        checks.append(openclaw_callback_check)
    openclaw_memory_hook_check = _openclaw_memory_hook_check()
    if openclaw_memory_hook_check is not None:
        checks.append(openclaw_memory_hook_check)
    hermes_context_check = _hermes_context_freshness_check()
    if hermes_context_check is not None:
        checks.append(hermes_context_check)
    openclaw_context_check = _openclaw_context_freshness_check()
    if openclaw_context_check is not None:
        checks.append(openclaw_context_check)
    port_conflict_check = _receiver_port_conflict_check()
    if port_conflict_check is not None:
        checks.append(port_conflict_check)
    agent_sync_connector_check = _agent_sync_connector_check()
    if agent_sync_connector_check is not None:
        checks.append(agent_sync_connector_check)
    agent_sync_receiver_check = _agent_sync_receiver_reachability_check()
    if agent_sync_receiver_check is not None:
        checks.append(agent_sync_receiver_check)
    checks.append(_memory_health_check())

    if sqlite_vec_available():
        checks.append(_doctor_check("semantic_retrieval", "pass", "sqlite-vec available for optional local semantic retrieval"))
    else:
        checks.append(
            _doctor_check(
                "semantic_retrieval",
                "warn",
                "sqlite-vec not installed; exact SQLite/Markdown memory still works. Optional fix: install `8mem[semantic]`.",
            )
        )

    if skip_llm_check:
        checks.append(_doctor_check("llm_backend", "warn", "Skipped Ollama probe"))
    else:
        ok, error = check_ollama_backend()
        if ok:
            checks.append(_doctor_check("llm_backend", "pass", f"Ollama reachable at {resolve_base_url()}"))
        else:
            model_hint = resolve_default_model()
            checks.append(_doctor_check("llm_backend", "warn", f"{error or 'Ollama backend unavailable'}. Fix: start Ollama and run `ollama pull {model_hint}`, or update OLLAMA_BASE_URL/DEFAULT_LLM_FALLBACK_MODEL."))

    try:
        context = build_engram_context()
        if context.get("system_prompt_injection"):
            checks.append(_doctor_check("v1_context", "pass", "Engram v0.1 context builds with system_prompt_injection"))
        else:
            checks.append(_doctor_check("v1_context", "fail", "Context missing system_prompt_injection. Fix: run `8mem init` or `8mem setup`, then rerun `8mem doctor`."))
    except Exception as exc:
        checks.append(_doctor_check("v1_context", "fail", f"Context build failed: {exc}. Fix: run `8mem init` or `8mem setup`, then rerun `8mem doctor`."))

    failed = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warn"]
    result: dict[str, Any] = {
        "ok": not failed,
        "status": "ready" if not failed else "not_ready",
        "runtime_home": str(home) if home else None,
        "checks": checks,
        "summary": {
            "passed": sum(1 for item in checks if item["status"] == "pass"),
            "warnings": len(warnings),
            "failed": len(failed),
        },
    }

    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo("8mem doctor")
        for item in checks:
            label = item["status"].upper()
            typer.echo(f"[{label}] {item['name']}: {item['message']}")
        typer.echo("")
        typer.echo(f"Status: {result['status']} ({len(warnings)} warnings, {len(failed)} failures)")

    if failed:
        raise typer.Exit(code=1)


@app.command()
def analyze(chat_export: Path) -> None:
    """Analyze chat export and update memory files."""
    mem = _prepare_runtime()
    copy_default_templates(mem)
    try:
        stats = analyze_chat_export(chat_export, mem)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        typer.secho(f"Analyze failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Analyzed: {chat_export}")
    for name, count in stats.items():
        typer.echo(f"- {name}: +{count} new entries")


@app.command("prepare-import")
def prepare_import(
    input_file: Path = typer.Argument(..., exists=True, readable=True, help="Text/markdown file to prepare for future import"),
    source_kind: str = typer.Option("text", "--source-kind", help="Source type label"),
    max_words: int = typer.Option(220, "--max-words", help="Maximum words per chunk"),
    overlap_words: int = typer.Option(30, "--overlap-words", help="Approximate overlap words between chunks"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable local import-prep cache"),
) -> None:
    """Prepare long text for future memory ingestion by chunking and caching it locally."""
    text = input_file.read_text(encoding="utf-8")
    document, from_cache = prepare_import_document(
        text,
        source_kind=source_kind,
        source_id=str(input_file),
        max_words=max_words,
        overlap_words=overlap_words,
        use_cache=not no_cache,
    )
    preview = build_import_preview(document)
    typer.echo(f"Prepared import: {input_file}")
    typer.echo(f"Source kind: {preview['source_kind']}")
    typer.echo(f"Chunks: {preview['chunk_count']}")
    typer.echo(f"Total words: {preview['total_words']}")
    typer.echo(f"Cache: {'hit' if from_cache else 'miss'}")
    typer.echo("")
    typer.echo("Chunk preview:")
    for item in preview["chunks"][:5]:
        typer.echo(f"- chunk {item['chunk_idx']}: {item['word_count']} words")
        typer.echo(f"  {item['preview']}")


@app.command("extract-import-facts")
def extract_import_facts(
    input_file: Path = typer.Argument(..., exists=True, readable=True, help="Text/markdown file to extract review candidates from"),
    source_kind: str = typer.Option("text", "--source-kind", help="Source type label"),
    max_words: int = typer.Option(220, "--max-words", help="Maximum words per chunk"),
    overlap_words: int = typer.Option(30, "--overlap-words", help="Approximate overlap words between chunks"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable local import-prep cache"),
) -> None:
    """Extract review-first memory fact candidates from long imported text."""
    text = input_file.read_text(encoding="utf-8")
    document, candidates, from_cache = prepare_import_candidates(
        text,
        source_kind=source_kind,
        source_id=str(input_file),
        max_words=max_words,
        overlap_words=overlap_words,
        use_cache=not no_cache,
    )
    review = build_candidate_review(candidates)
    typer.echo(f"Prepared import facts: {input_file}")
    typer.echo(f"Chunks: {len(document.chunks)}")
    typer.echo(f"Candidate facts: {len(candidates)}")
    typer.echo(f"Cache: {'hit' if from_cache else 'miss'}")
    typer.echo("")
    if not review:
        typer.echo("No candidate memory facts found.")
        return
    typer.echo("Review candidates:")
    for file_name, items in review.items():
        typer.echo(f"{file_name}")
        for item in items[:8]:
            typer.echo(f"- {item['text']} (confidence {item['confidence']})")


@app.command("apply-import-facts")
def apply_import_facts(
    input_file: Path = typer.Argument(..., exists=True, readable=True, help="Text/markdown file to extract and write approved facts from"),
    source_kind: str = typer.Option("text", "--source-kind", help="Source type label"),
    max_words: int = typer.Option(220, "--max-words", help="Maximum words per chunk"),
    overlap_words: int = typer.Option(30, "--overlap-words", help="Approximate overlap words between chunks"),
    min_confidence: float = typer.Option(0.6, "--min-confidence", help="Minimum candidate confidence to apply"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable local import-prep cache"),
) -> None:
    """Apply extracted import candidates into the normal 8mem memory files."""
    _prepare_runtime()
    text = input_file.read_text(encoding="utf-8")
    _, candidates, from_cache = prepare_import_candidates(
        text,
        source_kind=source_kind,
        source_id=str(input_file),
        max_words=max_words,
        overlap_words=overlap_words,
        use_cache=not no_cache,
    )
    approved = [item for item in candidates if item.confidence >= min_confidence]
    if not approved:
        typer.echo(f"No candidate facts met min confidence {min_confidence}.")
        return

    updates: dict[str, set[str]] = {}
    for item in approved:
        updates.setdefault(item.file_name, set()).add(item.text)

    stats = apply_memory_updates(updates)
    typer.echo(f"Applied import facts: {input_file}")
    typer.echo(f"Cache: {'hit' if from_cache else 'miss'}")
    for name in sorted(updates):
        typer.echo(f"- {name}: +{stats.get(name, 0)} new entries")


@app.command()
def mirror() -> None:
    """Print AI Believes About You summary."""
    mem = _prepare_runtime()
    copy_default_templates(mem)
    typer.echo(build_mirror_text(mem))


@app.command()
def forget(
    query: str = typer.Argument(..., help="Text to find and remove from saved memory"),
    file_name: str | None = typer.Option(None, "--file", help="Optional memory file, for example BELIEFS.md"),
    user_id: str | None = typer.Option(None, "--user-id", help="Optional user-scoped memory ID"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete matching entries without an interactive confirmation"),
) -> None:
    """Remove saved memory entries with confirmation and audit trail."""
    _prepare_runtime()
    matches = find_forget_candidates(query, user_id=user_id, file_name=file_name)
    if not matches:
        typer.echo(f'No saved memory matched "{query}".')
        return

    typer.echo("Matching saved memory:")
    for idx, item in enumerate(matches, start=1):
        typer.echo(f"{idx}. {item['file_name']}: {item['value']}")
    if not yes and not typer.confirm("Delete these memory entries?", default=False):
        typer.echo("Forget cancelled.")
        return

    deleted = forget_memory_entries(query, user_id=user_id, file_name=file_name)
    if not deleted:
        typer.echo("No matching memory was deleted.")
        return
    typer.echo(f"Deleted {len(deleted)} memory entr{'y' if len(deleted) == 1 else 'ies'}.")
    typer.echo("Deletion was added to EVOLUTION/audit history and /corrections.")


@app.command("export-context")
def export_context(output: Path | None = typer.Option(None, "--output", "-o")) -> None:
    """Compile memory markdown into context block."""
    mem = _prepare_runtime()
    copy_default_templates(mem)
    compiled = compile_context(mem)
    typer.echo(compiled)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(compiled, encoding="utf-8")
        typer.echo(f"Saved context to: {output}")


@app.command("register")
def register(
    url: str = typer.Option(..., "--url", help="Connector webhook URL, for example https://agent.example.com/8mem/sync"),
    connector_id: str = typer.Option(..., "--id", help="Stable connector id, for example openclaw-agent"),
    description: str | None = typer.Option(None, "--description", help="Optional human-readable connector description"),
    user_id: str | None = typer.Option(None, "--user-id", help="Optional user-scoped memory id"),
) -> None:
    """Register an agent connector for real-time 8mem webhook pushes."""
    load_project_env()
    ensure_runtime_dirs()
    connector, error = register_connector(
        connector_id=connector_id,
        url=url,
        description=description,
        user_id=user_id,
    )
    if connector is None:
        typer.secho(f"Connector registration failed: {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo("Connector registered.")
    typer.echo(f"ID: {connector.id}")
    typer.echo(f"URL: {connector.url}")
    typer.echo(f"Secret: {connector.secret}")
    typer.echo("Store this secret in the connector. 8mem will not print it again.")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8787) -> None:
    """Launch local web UI."""
    mem = _prepare_runtime()
    copy_default_templates(mem)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command()
def start(
    host: str = typer.Option("0.0.0.0", "--host", help="Host interface to bind"),
    port: int = typer.Option(8787, "--port", help="Port to listen on"),
    foreground: bool = typer.Option(False, "--foreground", "--fg", help="Run in the current terminal like `8mem serve`"),
) -> None:
    """Start the local 8mem server in the background by default."""
    load_project_env()
    home, mem = ensure_runtime_dirs()
    copy_default_templates(mem)
    if foreground:
        serve(host=host, port=port)
        return

    systemd_service = _systemd_user_service()
    if systemd_service is not None:
        if _systemd_service_active(systemd_service):
            pid = systemd_service.get("MainPID") or "-"
            typer.echo(f"8mem already running at http://{host}:{port} (systemd, pid {pid})")
            typer.echo("Managed by: systemctl --user status 8mem.service")
            return
        ok, message = _run_systemd_user_action("start")
        if not ok:
            typer.secho(f"Could not start systemd service {SYSTEMD_SERVICE_NAME}: {message}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        ready, ready_message = _wait_for_readyz(host, port)
        if ready:
            typer.echo(f"8mem running at http://{host}:{port} (systemd)")
        else:
            typer.secho(f"8mem systemd service started but readiness is not confirmed: {ready_message}", fg=typer.colors.YELLOW)
        typer.echo("Managed by: systemctl --user status 8mem.service")
        return

    pid_path = _pid_file()
    log_path = _log_file()
    existing_pid = _read_pid(pid_path)
    if _pid_is_running(existing_pid):
        typer.echo(f"8mem already running at http://{host}:{port} (pid {existing_pid})")
        typer.echo(f"PID file: {pid_path}")
        typer.echo(f"Log file: {log_path}")
        return
    if existing_pid is not None:
        pid_path.unlink(missing_ok=True)
    if _port_is_in_use(host, port):
        typer.secho(
            f"Port {port} is already in use on {host}. Stop the old process first, then run `8mem start` again.",
            fg=typer.colors.RED,
        )
        typer.echo("Find it with: lsof -nP -iTCP:%s -sTCP:LISTEN" % port)
        typer.echo("If it is an old unmanaged 8mem process, stop it with: kill <PID>")
        raise typer.Exit(code=1)

    command = [
        sys.executable,
        "-m",
        "eightmem.cli.main",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    env = os.environ.copy()
    env["EIGHTMEM_HOME"] = str(home)
    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    ok, message = _wait_for_readyz(host, port)
    if ok:
        typer.echo(f"8mem running at http://{host}:{port}")
    else:
        typer.secho(f"8mem started but readiness is not confirmed: {message}", fg=typer.colors.YELLOW)
    typer.echo(f"PID file: {pid_path}")
    typer.echo(f"Log file: {log_path}")


@app.command()
def status(
    host: str = typer.Option("0.0.0.0", "--host", help="Host used for readyz probe"),
    port: int = typer.Option(8787, "--port", help="Port used for readyz probe"),
) -> None:
    """Show background server status."""
    load_project_env()
    ensure_runtime_dirs()
    systemd_service = _systemd_user_service()
    if systemd_service is not None:
        systemd_running = _systemd_service_active(systemd_service)
        ready, ready_message = _probe_readyz(host, port)
        state = "running (systemd)" if systemd_running else "stopped (systemd)"
        typer.echo(f"Status: {state}")
        typer.echo(f"PID: {systemd_service.get('MainPID') or '-'}")
        typer.echo(f"Port: {port}")
        typer.echo(f"URL: http://{host}:{port}")
        typer.echo(f"readyz: {'pass' if ready else 'fail'} - {ready_message}")
        typer.echo("Managed by: systemctl --user status 8mem.service")
        return

    pid_path = _pid_file()
    log_path = _log_file()
    pid = _read_pid(pid_path)
    running = _pid_is_running(pid)
    ready, ready_message = _probe_readyz(host, port)
    state = "running" if running else "stopped"
    if pid and not running:
        state = "stopped (stale pid file)"

    typer.echo(f"Status: {state}")
    typer.echo(f"PID: {pid if pid else '-'}")
    typer.echo(f"Port: {port}")
    typer.echo(f"URL: http://{host}:{port}")
    typer.echo(f"readyz: {'pass' if ready else 'fail'} - {ready_message}")
    typer.echo(f"PID file: {pid_path}")
    typer.echo(f"Log file: {log_path}")


@app.command()
def stop(timeout_seconds: float = typer.Option(8.0, "--timeout", help="Seconds to wait for clean shutdown")) -> None:
    """Stop the background 8mem server."""
    load_project_env()
    ensure_runtime_dirs()
    systemd_service = _systemd_user_service()
    if systemd_service is not None:
        if not _systemd_service_active(systemd_service):
            typer.echo("8mem is not running (systemd service is stopped).")
            return
        ok, message = _run_systemd_user_action("stop")
        if not ok:
            typer.secho(f"Could not stop systemd service {SYSTEMD_SERVICE_NAME}: {message}", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.echo("8mem stopped (systemd).")
        return

    pid_path = _pid_file()
    pid = _read_pid(pid_path)
    if not pid:
        typer.echo("8mem is not running (no pid file).")
        return
    if not _pid_is_running(pid):
        pid_path.unlink(missing_ok=True)
        typer.echo("8mem is not running (removed stale pid file).")
        return

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            pid_path.unlink(missing_ok=True)
            typer.echo(f"8mem stopped (pid {pid}).")
            return
        time.sleep(0.2)

    typer.secho(f"8mem did not stop within {timeout_seconds:.1f}s; pid {pid} may still be running.", fg=typer.colors.YELLOW)


@app.command("heartbeat-check")
def heartbeat_check(
    user_id: str | None = typer.Option(None, "--user-id", help="Optional user-scoped runtime"),
    now_iso: str | None = typer.Option(None, "--now", help="Override current time in ISO format for testing"),
) -> None:
    """Run one heartbeat scan against local file-backed signal connectors."""
    _prepare_runtime()
    ensure_governance_policy(user_id=user_id)
    if now_iso:
        from datetime import datetime

        now = datetime.fromisoformat(now_iso)
    else:
        now = None
    result = run_heartbeat(now=now, user_id=user_id)
    typer.echo(f"checked_at: {result.checked_at}")
    typer.echo(f"scanned_signals: {result.scanned_signals}")
    typer.echo(f"state_path: {result.state_path}")
    typer.echo("")
    if not result.notifications and not result.deferred and not result.suppressed:
        typer.echo("No signals found.")
        return
    if result.notifications:
        typer.echo("Notify now:")
        for item in result.notifications:
            typer.echo(f"- [{item.source}] {item.title} ({item.urgency})")
            typer.echo(f"  {item.reason}")
    if result.deferred:
        typer.echo("Deferred/digest:")
        for item in result.deferred:
            typer.echo(f"- [{item.source}] {item.title} ({item.delivery_mode}, {item.urgency})")
            typer.echo(f"  {item.reason}")
    if result.suppressed:
        typer.echo("Suppressed:")
        for item in result.suppressed:
            typer.echo(f"- [{item.source}] {item.title} ({item.urgency})")
            typer.echo(f"  {item.reason}")


@app.command("explain-action-policy")
def explain_action_policy(
    action_name: str = typer.Argument(..., help="Action name like email_send or public_post"),
    urgency: str = typer.Option("normal", "--urgency", help="critical/high/normal/low"),
    user_id: str | None = typer.Option(None, "--user-id", help="Optional user-scoped runtime"),
    now_iso: str | None = typer.Option(None, "--now", help="Override current time in ISO format for testing"),
) -> None:
    """Explain how governance would treat an action right now."""
    _prepare_runtime()
    ensure_governance_policy(user_id=user_id)
    if now_iso:
        from datetime import datetime

        now = datetime.fromisoformat(now_iso)
    else:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
    decision = evaluate_action(action_name=action_name, urgency=urgency, now=now, user_id=user_id)
    typer.echo(f"capability: {decision.capability}")
    typer.echo(f"permission: {decision.permission}")
    typer.echo(f"notification: {decision.notification}")
    typer.echo(f"urgency: {decision.urgency}")
    typer.echo(f"requires_confirmation: {decision.requires_confirmation}")
    typer.echo(f"reason: {decision.reason}")


@app.command("dedupe")
def dedupe_memory(
    user_id: str | None = typer.Option(None, "--user-id", help="Optional user-scoped runtime"),
    apply_changes: bool = typer.Option(
        False,
        "--apply",
        help="Remove exact duplicate active memories. Near duplicates are review-only.",
    ),
) -> None:
    """Preview or apply safe memory deduplication."""
    _prepare_runtime()
    result = apply_memory_dedup(user_id=user_id) if apply_changes else preview_memory_dedup(user_id=user_id)
    scope = user_id or "default"
    typer.echo(f"8mem dedupe: {scope}")
    typer.echo(f"Exact duplicates removable: {result['exact_duplicate_count']}")
    typer.echo(f"Near duplicates for review: {result['near_duplicate_count']}")

    exact_duplicates = result.get("exact_duplicates", [])
    if exact_duplicates:
        typer.echo("")
        typer.echo("Exact duplicates:")
        for action in exact_duplicates:
            keep = action["keep"]
            typer.echo(f"  keep {keep['file']}: {keep['value']}")
            for item in action["remove"]:
                typer.echo(f"  remove {item['file']}: {item['value']}")

    near_duplicates = result.get("near_duplicates", [])
    if near_duplicates:
        typer.echo("")
        typer.echo("Near duplicates require review:")
        for item in near_duplicates[:10]:
            left = item["left"]
            right = item["right"]
            typer.echo(f"  {item['similarity']}: {left['file']} :: {left['value']}")
            typer.echo(f"       {right['file']} :: {right['value']}")
        if len(near_duplicates) > 10:
            typer.echo(f"  ... {len(near_duplicates) - 10} more")

    if apply_changes:
        typer.echo("")
        typer.echo(f"Removed: {result['removed_count']}")
    else:
        typer.echo("")
        typer.echo("Dry run only. Re-run with --apply to remove exact duplicates.")


@app.command("test-ollama")
def test_ollama(
    model: str | None = typer.Option(None, "--model", help="Ollama model name"),
    base_url: str | None = typer.Option(None, "--base-url", help="Ollama base URL"),
    prompt: str = typer.Option("Reply with exactly: TEST_OK", "--prompt", help="Prompt to test"),
    include_memory: bool = typer.Option(
        True,
        "--include-memory/--no-memory",
        help="Prepend 8mem context to the test prompt",
    ),
    timeout_seconds: int = typer.Option(120, "--timeout", help="Request timeout in seconds"),
) -> None:
    """Verify local model connectivity and output parsing through Ollama."""
    mem = _prepare_runtime()
    copy_default_templates(mem)

    chosen_model = resolve_default_model(model)
    chosen_base_url = resolve_base_url(base_url)
    payload_prompt = prompt
    if include_memory:
        payload_prompt = (
            "Use the memory context if relevant, then follow the user prompt.\n\n"
            f"{compile_context(mem)}\n"
            f"User prompt: {prompt}"
        )

    try:
        result = generate_text(
            model=chosen_model,
            prompt=payload_prompt,
            base_url=chosen_base_url,
            timeout_seconds=timeout_seconds,
            num_predict=120,
        )
    except OllamaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"base_url: {chosen_base_url}")
    typer.echo(f"model: {chosen_model}")
    typer.echo(f"text_source: {result.text_source}")
    typer.echo(f"done: {result.done}")
    typer.echo(f"done_reason: {result.done_reason}")
    typer.echo(f"eval_count: {result.eval_count}")
    typer.echo("")
    typer.echo(result.text)


@app.command("ask-ollama")
def ask_ollama(
    question: str = typer.Argument(..., help="User question to answer with memory"),
    model: str | None = typer.Option(None, "--model", help="Ollama model name"),
    base_url: str | None = typer.Option(None, "--base-url", help="Ollama base URL"),
    include_memory: bool = typer.Option(
        True,
        "--include-memory/--no-memory",
        help="Include compiled 8mem context before the question",
    ),
    timeout_seconds: int = typer.Option(180, "--timeout", help="Request timeout in seconds"),
) -> None:
    """Ask a local model with optional 8mem context preloaded."""
    mem = _prepare_runtime()
    copy_default_templates(mem)

    chosen_model = resolve_default_model(model)
    chosen_base_url = resolve_base_url(base_url)
    prompt = question
    if include_memory:
        prompt = (
            "Use the memory context below to answer accurately. Keep the response concise.\n\n"
            f"{compile_context(mem)}\n"
            f"User question: {question}"
        )

    try:
        result = generate_text(
            model=chosen_model,
            prompt=prompt,
            base_url=chosen_base_url,
            timeout_seconds=timeout_seconds,
        )
    except OllamaError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if os.getenv("EIGHTMEM_DEBUG"):
        typer.echo(f"[debug] base_url={chosen_base_url} model={chosen_model} source={result.text_source}")
    typer.echo(result.text)


if __name__ == "__main__":
    app()

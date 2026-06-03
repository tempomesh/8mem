import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import type { OpenClawConfig } from "../../config/config.js";
import { defaultRuntime } from "../../runtime.js";
import type { TemplateContext } from "../templating.js";

const execFileAsync = promisify(execFile);
const DEFAULT_TIMEOUT_MS = 10_000;

type ExternalHookEntry = {
  enabled?: boolean;
  script?: string;
  trigger?: string;
  timeoutMs?: number;
  timeout?: number;
  match?: {
    type?: string;
    patterns?: string[];
  };
};

type ExternalHooksConfig = {
  enabled?: boolean;
  entries?: Record<string, ExternalHookEntry>;
};

export function extract8memHookActionAndFact(message: string): {
  action: "save" | "forget" | "correct" | "context";
  fact: string;
} | undefined {
  const trimmed = message.trim();
  const lowered = trimmed.toLowerCase();

  for (const prefix of ["remember ", "from now on ", "always ", "do not "]) {
    if (lowered.startsWith(prefix)) {
      return { action: "save", fact: trimmed.slice(prefix.length).trim() };
    }
  }

  for (const prefix of ["forget ", "/forget "]) {
    if (lowered.startsWith(prefix)) {
      return { action: "forget", fact: trimmed.slice(prefix.length).trim() };
    }
  }

  for (const prefix of ["correct that ", "correct ", "update that ", "update "]) {
    if (lowered.startsWith(prefix)) {
      return { action: "correct", fact: trimmed.slice(prefix.length).trim() };
    }
  }

  const questionPrefixes = ["what ", "who ", "when ", "where ", "why ", "how ", "does ", "do ", "can ", "could ", "would ", "should ", "is ", "are "];
  if (questionPrefixes.some((prefix) => lowered.startsWith(prefix)) || trimmed.includes("?")) {
    return undefined;
  }

  const declarativePrefixes = ["my favorite ", "my favourite ", "i prefer ", "i like ", "i drink "];
  if (declarativePrefixes.some((prefix) => lowered.startsWith(prefix))) {
    return { action: "save", fact: trimmed };
  }

  return undefined;
}

export function messageMatchesExternalHook(message: string, hook: ExternalHookEntry): boolean {
  if (hook.match?.type?.toLowerCase() === "always") {
    return true;
  }
  const patterns = hook.match?.patterns;
  if (!Array.isArray(patterns) || patterns.length === 0) {
    return false;
  }
  const lowered = message.toLowerCase();
  return patterns.some((pattern) => {
    const normalized = pattern.trim().toLowerCase();
    return normalized.length > 0 && lowered.includes(normalized);
  });
}

function expandHome(input: string): string {
  if (input === "~") {
    return os.homedir();
  }
  if (input.startsWith("~/")) {
    return path.join(os.homedir(), input.slice(2));
  }
  return input;
}

function resolveExternalHooks(config: OpenClawConfig): ExternalHooksConfig | undefined {
  return (config.hooks as unknown as { external?: ExternalHooksConfig } | undefined)?.external;
}

export async function runExternalPreLlmHooks(params: {
  config: OpenClawConfig;
  message: string;
  sessionKey?: string;
  sessionId?: string;
  workspaceDir: string;
  sessionCtx: TemplateContext;
}): Promise<string | undefined> {
  const external = resolveExternalHooks(params.config);
  if (external?.enabled !== true || !external.entries) {
    return undefined;
  }

  const message = params.message.trim();
  if (!message) {
    return undefined;
  }

  const outputs: string[] = [];
  for (const [name, hook] of Object.entries(external.entries)) {
    if (hook?.enabled !== true || hook.trigger !== "pre_llm_call" || !hook.script?.trim()) {
      continue;
    }
    if (!messageMatchesExternalHook(message, hook)) {
      continue;
    }

    const matchType = hook.match?.type?.toLowerCase();
    const isContextHook = matchType === "always" || matchType === "context";
    const extracted = isContextHook ? { action: "context" as const, fact: message } : extract8memHookActionAndFact(message);
    if (!extracted?.fact) {
      continue;
    }
    const { action, fact } = extracted;

    const timeoutMs = Math.max(1, hook.timeoutMs ?? hook.timeout ?? DEFAULT_TIMEOUT_MS);
    const scriptPath = expandHome(hook.script.trim());
    try {
      const result = await execFileAsync(scriptPath, [action, fact], {
        timeout: timeoutMs,
        windowsHide: true,
        env: {
          ...process.env,
          OPENCLAW_8MEM_HOOK_NAME: name,
          OPENCLAW_8MEM_SESSION_KEY: params.sessionKey ?? "",
          OPENCLAW_8MEM_SESSION_ID: params.sessionId ?? "",
          OPENCLAW_8MEM_WORKSPACE_DIR: params.workspaceDir,
          OPENCLAW_8MEM_PROVIDER: params.sessionCtx.Provider ?? "",
          OPENCLAW_8MEM_CHANNEL: params.sessionCtx.OriginatingChannel ?? "",
        },
      });
      const stdout = String(result.stdout ?? "").trim();
      if (stdout) {
        outputs.push(`[8mem] ${stdout}`);
      }
    } catch (err) {
      defaultRuntime.log(
        `external pre_llm_call hook ${name} failed; continuing without injection: ${String(err)}`,
      );
    }
  }

  return outputs.length > 0 ? outputs.join("\n\n") : undefined;
}

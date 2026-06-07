# Agent Integrations

8mem works standalone first.

OpenClaw and Hermes are optional agent runtimes. 8mem does not bundle them.

## Integration Model

```text
8mem memory
  -> exported context
  -> agent runtime
  -> explicit remember/correct/forget writes
  -> 8mem memory
```

The runtime does the agent work. 8mem owns the memory.

## What 8mem Adds

| Runtime | What 8mem adds |
|---|---|
| OpenClaw | Local memory context injection and explicit remember/correct/forget handling. |
| Hermes | Local memory context injection and Telegram/plugin command support where available. |
| Telegram | Direct user-facing memory commands and cards. |
| Browser UI | Local memory dashboard, import, review, and memory test. |

## If OpenClaw Is Already Installed

Run:

```bash
8mem setup --mode openclaw
8mem doctor
```

Then restart or redeploy OpenClaw if doctor tells you to.

## If Hermes Is Already Installed

Run:

```bash
8mem setup --mode hermes
8mem doctor
```

Then restart or redeploy Hermes if doctor tells you to.

## If You Install OpenClaw Or Hermes Later

8mem setup only wires runtimes that exist at setup time.

If you install a runtime later, rerun the matching setup:

```bash
8mem setup --mode openclaw
```

or:

```bash
8mem setup --mode hermes
```

Then:

```bash
8mem doctor
```

## Runtime Files Are Generated Locally

8mem does not ship private runtime files such as:

```text
AGENTS.md
SOUL.md
HEARTBEAT.md
MEMORY-API.md
```

Those files can contain local paths, runtime-specific instructions, private agent state, or secrets.

8mem generates only the needed integration blocks during setup.

## Read-Only Commands

These should inspect memory without saving new memory:

```text
/passport
/compare <topic>
/corrections
/refresh8mem
```

Memory writes should be explicit:

```text
remember ...
correct ...
forget ...
```

## Removing An Integration

OpenClaw integration cleanup:

```bash
8mem uninstall --mode openclaw
```

This removes only 8mem-managed OpenClaw integration files/blocks.

It does not uninstall OpenClaw.

Package uninstall is separate:

```bash
pipx uninstall 8mem
```

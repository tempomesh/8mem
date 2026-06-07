# Getting Started With 8mem

8mem is a local-first memory layer for AI agents.

Tagline:

```text
Agent memory your AI can finally keep.
Visible. Correctable. Portable. Shared.
```

This guide is for first-time users who want to install 8mem, save memory, inspect it, and optionally connect Telegram, OpenClaw, or Hermes later.

## What You Get First

The default 8mem install gives you:

| Part | What it does | Required? |
|---|---|---|
| Local browser UI | Save, inspect, correct, and forget memory on your machine. | Yes |
| Local API | Lets tools and runtimes read/write memory. | Yes |
| Local memory files | Human-readable memory stored under `~/.8mem`. | Yes |
| Ollama connection | Optional local model replies in the browser memory test. | Optional |
| Telegram bot | Chat with 8mem from Telegram. | Optional |
| OpenClaw/Hermes adapters | Share memory with agent runtimes. | Optional |

You do not need Telegram, OpenClaw, or Hermes to start using 8mem.

## Install Option 1: Curl Installer

Recommended for most users:

```bash
curl -fsSL https://8mem.com/install.sh | bash
```

Then follow the prompts.

After install, run:

```bash
8mem doctor
8mem start
```

Open:

```text
http://127.0.0.1:8787
```

If your shell cannot find `8mem`, use the full path printed by the installer, or open a new terminal.

## Install Option 2: pipx

Recommended if you already use Python tools:

```bash
pipx install 8mem
8mem setup
8mem doctor
8mem start
```

If `pipx` is missing:

macOS:

```bash
brew install pipx
pipx ensurepath
```

Linux / Raspberry Pi:

```bash
sudo apt update
sudo apt install -y pipx
pipx ensurepath
```

Open a new terminal after `pipx ensurepath`.

## First Setup Choice

During `8mem setup`, choose how you want to use 8mem first:

| Choice | Pick this if |
|---|---|
| Browser UI only | You want the easiest local start. |
| Telegram bot | You already have a BotFather token and public HTTPS webhook URL. |
| Browser UI and Telegram | You want both local dashboard and Telegram. |
| OpenClaw integration | OpenClaw is already installed on this machine. |
| Hermes integration | Hermes is already installed on this machine. |
| Skip optional setup | You only want files initialized now. |

If you are unsure, choose Browser UI only.

You can add everything else later.

## Save Your First Memory

Open:

```text
http://127.0.0.1:8787
```

Go to `Start Here`.

Use `Remember this`.

Examples:

```text
I prefer short direct replies.
I am building a local-first AI memory tool.
Do not use emojis in professional answers.
```

Then check:

| Page | What to look for |
|---|---|
| Memory Passport | Counts should increase after memory is saved. |
| What AI Knows | Shows clean memory summary. |
| Memory Library | Shows the human-readable memory sections. |
| Memory Test | Tests how a local model responds with saved memory attached. |

## Important: Memory Test Is Not A Save Box

The `Memory Test` page asks a local model with your saved memory attached.

It does not automatically save every sentence you type.

To save memory, use one of these:

| Place | Save command |
|---|---|
| Browser UI | `Start Here` -> `Remember this` |
| Telegram | `remember I prefer short replies` |
| Import page | Upload or sample conversation data |

If you type `I am a student` only in Memory Test, the model may answer using that prompt, but the memory count may still stay `0`.

## Optional: Local Model Setup

8mem can use Ollama for local model replies.

Install Ollama:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Pull a small model:

```bash
ollama pull <model-name>
```

Then configure 8mem:

```bash
8mem setup --llm-model <your-installed-ollama-model>
8mem doctor
```

Example small model: `qwen3:1.7b`. You can use any installed Ollama model. 8mem should point to a model that exists on your machine.

Check installed models:

```bash
ollama list
```

## Optional: Telegram Later

If you did not configure Telegram during first setup:

```bash
8mem setup --mode telegram
8mem doctor
8mem start
```

Telegram needs:

| Requirement | Why |
|---|---|
| BotFather token | Identifies your Telegram bot. |
| Public HTTPS URL | Lets Telegram reach your local 8mem server. |
| Running 8mem server | Receives Telegram webhook messages. |

See [Telegram Setup](TELEGRAM_SETUP.md).

## Optional: OpenClaw Or Hermes Later

If you install OpenClaw after 8mem:

```bash
8mem setup --mode openclaw
8mem doctor
```

If you install Hermes after 8mem:

```bash
8mem setup --mode hermes
8mem doctor
```

8mem does not bundle OpenClaw or Hermes. It only adds local integration files when those runtimes exist on your machine.

See [Agent Integrations](INTEGRATIONS.md).

## Daily Commands

```bash
8mem start
8mem status
8mem doctor
8mem stop
```

Browser UI:

```text
http://127.0.0.1:8787
```

## If Something Feels Broken

Run:

```bash
8mem doctor
```

Then check [Troubleshooting](TROUBLESHOOTING.md).

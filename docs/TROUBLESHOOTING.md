# Troubleshooting

Run this first:

```bash
8mem doctor
```

Doctor tells you what is ready, what is optional, and what needs setup.

## Common First-Run Issues

| Problem | What it usually means | Fix |
|---|---|---|
| `8mem: command not found` | Your shell cannot see the install path yet. | Open a new terminal, run `pipx ensurepath`, or use the full path printed by install. |
| Browser opens but memory counts are `0` | No memory has been saved yet. | Go to `Start Here` -> `Remember this`, or use Telegram `remember ...`. |
| Memory Test answers but counts stay `0` | Memory Test is read-only prompt testing, not memory saving. | Save from `Start Here`, Telegram, or Import. |
| `Sample file not found` | Sample data asset is unavailable in that install. | Upgrade 8mem, or save your first memory manually from `Start Here`. |
| Chat/test panel resets when switching pages | The browser UI is not a permanent chat transcript. | Saved memory persists; test chat history may refresh. |
| Ollama model error | 8mem points to a model not installed locally. | Run `ollama list`, then `8mem setup --llm-model <installed-model>`. |
| Telegram bot does not reply | Webhook or tunnel is not configured. | See [Telegram Setup](TELEGRAM_SETUP.md). |
| OpenClaw/Hermes not detected | Those runtimes are not installed yet. | Install them first, then run `8mem setup --mode openclaw` or `8mem setup --mode hermes`. |

## Memory Shows 0 Entries

This is normal on a fresh install.

8mem does not invent memory.

Save your first memory:

Browser:

```text
Start Here -> Remember this
```

Telegram:

```text
remember I prefer short direct replies
```

Then refresh the dashboard or run:

```bash
8mem doctor
```

## Local Model Is Wrong Or Missing

Check installed Ollama models:

```bash
ollama list
```

Pull a small model:

```bash
ollama pull <model-name>
```

Configure 8mem:

```bash
8mem setup --llm-model <your-installed-ollama-model>
8mem doctor
8mem start
```

If you prefer another model:

```bash
8mem setup --llm-model <another-installed-ollama-model>
```

Use a model that appears in `ollama list`.

## Browser Works But Telegram Does Not

Browser UI uses local access:

```text
http://127.0.0.1:8787
```

Telegram needs public HTTPS access.

Run:

```bash
8mem setup --mode telegram
8mem doctor
8mem start
```

If your tunnel URL changes, rerun Telegram setup.

## Telegram Setup Was Skipped

If you pressed Enter without a public HTTPS URL, setup saves what it can but does not register the Telegram webhook.

This means the bot may exist, but messages will not reach 8mem yet.

Fix when your HTTPS URL is ready:

```bash
8mem setup --mode telegram
8mem doctor
8mem start
```

## OpenClaw Or Hermes Installed Later

If 8mem was installed first, then OpenClaw or Hermes was installed later, rerun setup for the runtime.

OpenClaw:

```bash
8mem setup --mode openclaw
8mem doctor
```

Hermes:

```bash
8mem setup --mode hermes
8mem doctor
```

8mem does not automatically modify runtimes that were installed after the original 8mem setup.

## Port 8787 Is Already In Use

Check status:

```bash
8mem status
```

Stop and restart:

```bash
8mem stop
8mem start
```

Or run on another port:

```bash
8mem start --port 8790
```

## Where 8mem Stores Data

Default local runtime:

```text
~/.8mem
```

Memory files:

```text
~/.8mem/memory
```

Runtime config:

```text
~/.8mem/.env
```

Do not share `~/.8mem/.env`. It can contain local API keys, bot tokens, and webhook secrets.

## Clean Reinstall

Safe reinstall that keeps memory:

```bash
8mem stop
pipx uninstall 8mem
pipx install 8mem
8mem setup
8mem start
```

Full delete, including local memory:

```bash
8mem stop
pipx uninstall 8mem
rm -rf ~/.8mem
```

Only run `rm -rf ~/.8mem` if you intentionally want to delete local 8mem memory.

## Still Stuck

Run:

```bash
8mem doctor
8mem status
```

Then include:

| Include | Do not include |
|---|---|
| OS and Python version | Bot token |
| Install method: curl, pipx, or pip | Local API key |
| `8mem doctor` output with secrets removed | `~/.8mem/.env` contents |
| What you expected and what happened | Private memory contents |

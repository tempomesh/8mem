# Telegram Setup

Telegram is optional.

Use 8mem in the browser first if you want the simplest start. Add Telegram when you have the bot token and public HTTPS URL ready.

## What Telegram Needs

| Requirement | Example | Why it matters |
|---|---|---|
| Telegram bot token | From BotFather | Lets 8mem send and receive Telegram messages. |
| Public HTTPS URL | `https://your-domain.ngrok-free.app` | Telegram cannot call `localhost` on your laptop. |
| Running 8mem server | `8mem start` | The webhook needs a live local server. |
| Local API key | Stored in `~/.8mem/.env` | Protects local 8mem API calls. |

Do not paste your bot token, webhook secret, or local API key into public issues, screenshots, or social posts.

## Step 1: Create A Bot In BotFather

In Telegram, open `@BotFather`.

Send:

```text
/newbot
```

Follow BotFather's prompts.

Copy the bot token.

The token usually looks like:

```text
123456789:ABCDEF_your_token_here
```

## Step 2: Create A Public HTTPS URL

Telegram must send messages to a public HTTPS URL.

Common options:

| Tool | Good for |
|---|---|
| ngrok | Quick local testing. |
| Cloudflare Tunnel | More stable public tunnel. |
| Tailscale Funnel | Private device/network users. |
| Your own HTTPS domain | Production setup. |

Example with ngrok:

```bash
ngrok http 8787
```

Copy the HTTPS URL from ngrok.

Example:

```text
https://abc123.ngrok-free.app
```

## Step 3: Run 8mem Telegram Setup

```bash
8mem setup --mode telegram
```

Paste:

| Prompt | What to enter |
|---|---|
| Telegram bot token from BotFather | Your BotFather token |
| Telegram webhook secret | Press Enter to generate one |
| Public HTTPS URL | Your ngrok/Cloudflare/Tailscale/domain URL |
| Ollama setup | Optional local model setup |

Then run:

```bash
8mem doctor
8mem start
```

## Step 4: Test In Telegram

Open your bot in Telegram.

Send:

```text
/start
```

Then test memory:

```text
remember I prefer short direct replies
what do you remember about me?
```

Useful commands:

```text
/passport
/compare write a short update in my style
/corrections
forget I prefer short direct replies
```

## If You Skipped Webhook Setup

If setup says webhook registration was skipped, Telegram will not send messages to your local 8mem server yet.

This is expected until you provide a public HTTPS URL.

Fix:

```bash
8mem setup --mode telegram
8mem doctor
8mem start
```

## If The Bot Does Not Reply

Check:

```bash
8mem doctor
8mem status
```

Common causes:

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot receives messages but no reply | 8mem server is stopped | Run `8mem start` |
| Doctor says webhook not registered | Public HTTPS URL missing | Run `8mem setup --mode telegram` with tunnel URL |
| Webhook points to old URL | ngrok URL changed | Rerun setup with the new URL |
| Telegram token invalid | Wrong BotFather token | Rerun setup with the correct token |
| Browser works but Telegram does not | Tunnel/webhook issue | Check public URL and rerun setup |

## Localhost Is Not Enough

This works in your browser:

```text
http://127.0.0.1:8787
```

But Telegram cannot reach that address from the internet.

Telegram needs a public HTTPS URL that forwards to your local 8mem server.

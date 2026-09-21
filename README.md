# Wolfram-style Telegram Bot

A Telegram bot that answers like Wolfram Alpha. Send it **text** (equations, unit
conversions, science/math questions) or a **photo** (handwritten equations,
textbook problems, diagrams, charts, screenshots) and it replies with a compact,
structured answer:

```
📥 Input      one-line interpretation of the question
✅ Result     the direct answer
📊 Details    up to 6 bullets of reasoning / formulas
💡 Notes      assumptions and edge cases (omitted when not useful)
```

Answers use Unicode math (√ ∫ π ∞ ≈ ≤ ≥ ∑ ∆ θ × ÷ ± x² a₁) — never LaTeX. Any
LaTeX the model still emits is converted to Unicode before sending.

## Features

- Text and vision in one model (images are inlined as base64 data URLs)
- Structured, bold-headed answers rendered with Telegram MarkdownV2
- LaTeX → Unicode conversion (`\frac{a}{b}` → `(a)/(b)`, `\sqrt{x}` → `√(x)`, …)
- Safe MarkdownV2 escaping with automatic plain-text fallback if Telegram rejects a message
- Automatic chunking under Telegram's 4096-character limit
- Images are downscaled/re-encoded client-side so large photos never blow up the request
- Retries with exponential backoff (1s, 2s, 4s) on 429 and 5xx responses
- Short per-chat conversation memory (`/reset` to clear)

## Requirements

- Python 3.10+
- A Telegram bot token
- A Nebius Token Factory (AI Studio) API key

## Setup

```bash
git clone <your-repo-url>
cd wolframalpha-telegram

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env        # Windows: copy .env.example .env
# edit .env and fill in your keys

python main.py
```

The bot runs in long-polling mode; no public URL or webhook is needed. Stop it
with `Ctrl+C`.

## Getting the keys

**Telegram bot token**

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot` and follow the prompts (name, then a username ending in `bot`).
3. BotFather replies with a token like `123456789:AAH...`. Put it in
   `TELEGRAM_BOT_TOKEN`.
4. Optional: `/setdescription`, `/setabouttext`, and `/setcommands` with:
   ```
   start - Welcome message
   help - How to use the bot
   reset - Clear conversation memory
   ```

**Nebius API key**

1. Sign up at [Nebius AI Studio / Token Factory](https://studio.nebius.ai/).
2. Go to **API keys** and create a new key.
3. Put it in `NEBIUS_API_KEY`.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from BotFather |
| `NEBIUS_API_KEY` | yes | — | Nebius API key |
| `NEBIUS_BASE_URL` | no | `https://api.studio.nebius.ai/v1` | OpenAI-compatible endpoint |
| `NEBIUS_MODEL` | no | `deepseek-ai/DeepSeek-V4.1-Flash` | Model id used for text |
| `NEBIUS_VISION_MODEL` | no | same as `NEBIUS_MODEL` | Model id used for photos |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` for verbose logs |
| `HISTORY_TURNS` | no | `4` | Past Q/A pairs kept per chat (`0` disables) |

The bot starts only if both required variables are set; otherwise it exits with
a clear `RuntimeError`.

## Swapping the model

The client is a plain OpenAI-compatible wrapper, so any Nebius catalog model
works — just change `NEBIUS_MODEL` and restart:

```env
NEBIUS_MODEL=deepseek-ai/DeepSeek-V4.1-Flash
```

Photo support needs a **vision-capable** model. The default,
`deepseek-ai/DeepSeek-V4.1-Flash`, handles both text and images, so no extra
configuration is needed.

If you switch `NEBIUS_MODEL` to a text-only model, keep photos working by
pointing the vision path at a multimodal model:

```env
NEBIUS_MODEL=deepseek-ai/DeepSeek-V4-Pro
NEBIUS_VISION_MODEL=google/gemma-3-27b-it
```

Leave `NEBIUS_VISION_MODEL` unset to send both text and images to the same
model. List the ids your account can actually use with:

```bash
curl -H "Authorization: Bearer $NEBIUS_API_KEY" https://api.studio.nebius.ai/v1/models
```

Because the endpoint is OpenAI-compatible, `NEBIUS_BASE_URL` can also point at
any other OpenAI-style provider.

## Project layout

| File | Role |
| --- | --- |
| `main.py` | Telegram handlers, image preprocessing, lifecycle |
| `ai_client.py` | Async Nebius client (text + vision), retries, `AIError` |
| `formatter.py` | LaTeX → Unicode, MarkdownV2 escaping, message chunking |
| `prompts.py` | System prompt that enforces the answer structure |
| `config.py` | Environment loading and validation |

## Notes and limits

- Telegram caps downloads at 20 MB; larger photos are rejected by Telegram itself.
- Photos are resized to a 1600 px long edge and re-encoded as JPEG (quality
  stepped down until under 3 MB) before being base64-encoded.
- API keys and full user messages are never written to the logs; only lengths,
  chat ids, and short error excerpts are.
- On a Nebius outage the bot retries three times and then replies with a
  friendly message instead of crashing.

## Always-on (Windows)

Four helper scripts keep the bot running like a service, no admin rights needed:

| Script | What it does |
| --- | --- |
| `install_autostart.bat` | Starts the bot now and on every Windows login |
| `uninstall_autostart.bat` | Removes the login entry |
| `run_bot.bat` | Runs the bot in a restart loop, logging to `bot.log` |
| `stop_bot.bat` | Stops the loop and the bot |

`install_autostart.bat` writes a small launcher to your Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\wolfram-telegram-bot.vbs`)
that runs `run_bot.bat` with no visible console window.

The restart loop re-reads `.env` every time it restarts, so editing a key takes
effect within ~10 seconds without touching anything else. Watch it with:

```
type bot.log
```

`setup_botfather.py` pushes the command menu, description and about text over
the Bot API - run it once, or again after editing those values.

## Deployment

Any always-on host works (VPS, Fly.io, Railway, a container). Set the same
environment variables there and run `python main.py`. A minimal systemd unit:

```ini
[Unit]
Description=Wolfram-style Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/wolframalpha-telegram
EnvironmentFile=/opt/wolframalpha-telegram/.env
ExecStart=/opt/wolframalpha-telegram/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Run only one instance per bot token — Telegram allows a single long-polling
consumer at a time.

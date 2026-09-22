<div align="center">

---

## The problem this solves

Ask any LLM a General Studies question and it will answer confidently. It will also
be **wrong** a meaningful fraction of the time — and wrong in the same clean, formatted,
authoritative voice it uses when it is right. For exam preparation that is worse than
useless: you memorise the wrong amendment number and only find out in the hall.

This bot never asks the model what it knows. It asks the model **what is printed on
the page you just photographed**.

```
You:  /add polity
You:  📷 [photo of a page from Laxmikanth]

Bot:  ✅ Added 6 questions.
      Subject: Polity
      Topics: Fundamental Rights, Right to Property, Constitutional Remedies
```

Every question, every option, and every explanation must be answerable from your page
alone. If the page is blurry, upside down, or is just a chapter index, the bot returns
nothing and tells you why rather than inventing filler.

---

## Two modes, one bot

### 1. 🎯 Drill — your books become a question bank

| Step            | What happens                                                     |
| --------------- | ---------------------------------------------------------------- |
| `/add polity` | Arms add-mode for 15 minutes                                     |
| 📷 Send a page  | Vision model reads it, writes up to 8 MCQs grounded in that page |
| `/quiz`       | Serves questions with A/B/C/D inline buttons                     |
| Tap an answer   | Instant ✅/❌, correct option highlighted, explanation shown     |
| `/stats`      | Accuracy per subject with progress bars                          |
| `/weak`       | Your worst topics, lowest accuracy first                         |
| `/daily 6`    | 10 questions pushed to you at 6 AM IST, every day                |

Answers feed a **spaced-repetition scheduler**. Get one wrong and it returns in an hour.
Get it right repeatedly and it drifts out to 12h → 24h → 3d → 1w → 2w → 30d.

### 2. 🔬 Solve — send any question, get an answer sized to it

The paper is 200 MCQs in about three hours, so answers are built for speed, not
completeness. The reply shape adapts to the question:

**Recall or one step** — the answer, then one line:

```
96
10% = 64, 5% = 32, so 15% = 64 + 32 = 96.
```

**Method matters** — the answer, then the working *the way a person actually does
it*, then the trap:

```
3900

⚡ 15% down then 20% up → 85 × 12 = 1020, so 1.02 — a net 2% gain
   So 2% of the original = 78 → 1% = 39 → 100% = 3900
   Check: 3900 → 3315 → 3978, which is 78 more (about 25 sec)

🎯 Net change is not −15 + 20 = +5%. Multiply the factors.
```

Note what it does *not* say: `78 ÷ 0.02 = 3900`. Nobody divides by 0.02. The prompt
enforces mental-arithmetic habits across every subject — get to the unit then scale,
build percentages from 10% and 5%, clear decimals before multiplying, treat ratios as
parts, round then correct. Rates, not formulas: *"A does 1/12 a day, B does 2/12,
together 3/12 — flip it, 4 days."*

**Explain or compare** — a one-sentence answer, then at most four bullets.

**Anything not about the exam** — plain sentences, like a normal chat. No headers,
no markers. Exam scaffolding is reserved for exam questions.

Answers use real Unicode math — `√ ∫ π ∞ ≈ ≤ ≥ ∑ ∆ θ × ÷ ± x² a₁` — never LaTeX.
Any LaTeX the model still emits gets converted before it reaches Telegram:

| Model writes | You see |
|---|---|
| `\frac{-b \pm \sqrt{\Delta}}{2a}` | `(-b ± √(∆))/(2a)` |
| `x^{2} + H_{2}O` | `x² + H₂O` |
| `$$\int_0^\infty$$` | `∫₀^(∞)` |

---

## Quickstart

```bash
git clone https://github.com/warpirate/wolframalpha-telegram
cd wolframalpha-telegram

python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

cp .env.example .env            # Windows: copy .env.example .env
# fill in TELEGRAM_BOT_TOKEN and NEBIUS_API_KEY

python setup_botfather.py       # pushes the command menu (run once)
python main.py
```

Long-polling — no webhook, no public URL, no port to open.

<details>
<summary><b>Getting the two keys</b></summary>

**Telegram bot token** — open [@BotFather](https://t.me/BotFather), send `/newbot`,
choose a display name and a username ending in `bot`. He replies with
`123456789:AAH...`. That token is full control of the bot: keep it in `.env`
(already gitignored) and `/revoke` it if it ever leaks.

**Nebius API key** — sign up at [Nebius AI Studio](https://studio.nebius.ai/),
open **API keys**, create one.

</details>

---

## Commands

| Command                         | What it does                                       |
| ------------------------------- | -------------------------------------------------- |
| `/add [subject]`              | Next photos become questions                       |
| `/done`                       | Leave add-mode                                     |
| `/quiz [subject] [n]`         | `/quiz polity 15` — defaults to 10, any subject |
| `/stats`                      | Accuracy overall, today, and per subject           |
| `/weak`                       | Weakest topics, lowest accuracy first              |
| `/bank`                       | How many questions you have stored                 |
| `/daily 6` / `/daily 21 20` | Daily quiz at an hour (IST), optional count        |
| `/nodaily`                    | Cancel it                                          |
| `/reset`                      | Clear the solver's short conversation memory       |

Sending a photo **without** `/add` solves it instead of banking it.

---

## How it works

```
                  ┌──────────────┐
   Telegram ─────▶│   main.py    │  handlers, add-mode routing, JobQueue
                  └──────┬───────┘
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
  ┌───────────┐   ┌─────────────┐    ┌─────────────┐
  │  mcq.py   │   │ ai_client.py│    │   quiz.py   │
  │ page ──▶  │──▶│  Nebius     │    │ keyboards,  │
  │ MCQs      │   │  (OpenAI-   │    │ scoring,    │
  └─────┬─────┘   │  compatible)│    │ rendering   │
        │         └─────────────┘    └──────┬──────┘
        ▼                                   │
  ┌───────────┐                             │
  │   db.py   │◀────────────────────────────┘
  │  SQLite   │  questions · attempts · review schedule · prefs
  └───────────┘

  formatter.py  ── LaTeX→Unicode, MarkdownV2 escaping, 4096-char chunking
```

### Design notes worth knowing

These are the non-obvious things that cost real debugging time:

<details>
<summary><b>Reasoning tokens silently eat your answer</b></summary>

`DeepSeek-V4.1-Flash` is a reasoning model, and its hidden reasoning tokens are billed
against `max_tokens`. With a 1500-token budget, an open-ended question ("give me a hard
percentage problem") burns the **entire** allowance thinking and returns
`content: ""` with `finish_reason: "length"` — a successful HTTP 200 with no answer in it.

The fix is three-part: a 4000-token default budget, and if it *still* starves, one
automatic retry with a doubled budget and `reasoning_effort: "none"`. The client also
deliberately refuses to fall back to `message.reasoning_content` — that field holds raw
chain of thought (*"We need answer user asks..."*) and must never reach a user.

</details>

<details>
<summary><b>MarkdownV2 is unforgiving</b></summary>

One unescaped `.` and Telegram rejects the whole message. So everything gets escaped —
then a small allowlist un-escapes exactly the four section headers we *want* bold, via
a regex anchored to the emoji. Chunking never splits between a backslash and the
character it escapes, and every send has a plain-text fallback if Telegram still objects.

Verified against topic names containing `*`, `_`, backticks and brackets.

</details>

<details>
<summary><b>Models don't return the JSON shape you asked for</b></summary>

Asked for `{"questions": [...]}`, this model returns `{"type": "json_object", "content": [...]}`.
The parser walks the response recursively looking for the first list of question-shaped
dicts, strips code fences, and falls back to a regex bracket-match. Then it validates:
exactly 4 options, no duplicate or blank options, `correct_index` in range, no duplicate
question text.

</details>

<details>
<summary><b>Images are normalised before they cost you tokens</b></summary>

Photos are downscaled to a 1600 px long edge and re-encoded as JPEG, stepping quality
down until under 3 MB, all in a worker thread so the event loop never blocks. A
6000×4000 PNG becomes a ~10 KB JPEG the model reads just as well.

</details>

---

## Configuration

| Variable                | Required | Default                             | Purpose                            |
| ----------------------- | -------- | ----------------------------------- | ---------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | ✅       | —                                  | From BotFather                     |
| `NEBIUS_API_KEY`      | ✅       | —                                  | From Nebius AI Studio              |
| `NEBIUS_BASE_URL`     |          | `https://api.studio.nebius.ai/v1` | Any OpenAI-compatible endpoint     |
| `NEBIUS_MODEL`        |          | `deepseek-ai/DeepSeek-V4.1-Flash` | Text model                         |
| `NEBIUS_VISION_MODEL` |          | same as above                       | Photo model                        |
| `LOG_LEVEL`           |          | `INFO`                            | `DEBUG` for verbose              |
| `HISTORY_TURNS`       |          | `4`                               | Solver memory depth,`0` disables |

Placeholder values from `.env.example` are rejected at startup, so the bot fails loudly
instead of booting and then 401-ing on every question.

**Swapping models.** The default handles both text and vision. If you point
`NEBIUS_MODEL` at a text-only model, set `NEBIUS_VISION_MODEL` separately to keep photos
working. List what your account can actually use:

```bash
curl -H "Authorization: Bearer $NEBIUS_API_KEY" https://api.studio.nebius.ai/v1/models
```

---

## Running it always-on

### Windows — no admin needed

| Script                      | Does                             |
| --------------------------- | -------------------------------- |
| `install_autostart.bat`   | Starts now, and on every login   |
| `uninstall_autostart.bat` | Removes the login entry          |
| `run_bot.bat`             | Restart loop, logs to`bot.log` |
| `stop_bot.bat`            | Stops everything                 |

The loop re-reads `.env` on each restart, so editing a key takes effect within ~10
seconds. Watch with `type bot.log`.

### Linux / cloud — one command

[`deploy/DEPLOY.md`](deploy/DEPLOY.md) walks through hosting it free and forever on
Oracle Cloud's Always Free tier. On any Ubuntu box:

```bash
curl -fsSL https://raw.githubusercontent.com/warpirate/wolframalpha-telegram/main/deploy/setup.sh | bash
```

That installs Python, adds swap, builds a virtualenv, and registers a hardened
systemd unit that restarts on failure and survives reboots. `deploy/update.sh` pulls
new code and restarts.

**No credit card?** [`deploy/DEPLOY-RENDER.md`](deploy/DEPLOY-RENDER.md) covers Render
plus Neon Postgres, both card-free. The bot switches backend on `DATABASE_URL`:

```
DATABASE_URL set    -> Postgres  (hosts with no persistent disk)
DATABASE_URL unset  -> SQLite    (local, unchanged)
```

Serverless platforms (Vercel, Netlify) and Telegram-bot sandboxes (TeleBotHost and
similar) will **not** work: this is a long-running Python process that needs pip
packages, a scheduler, and calls that run 20+ seconds. Those platforms offer none of
the four.

> Run **one** instance per bot token. Telegram allows a single long-polling consumer.

---

## Project layout

| File                   | Role                                                                 |
| ---------------------- | -------------------------------------------------------------------- |
| `main.py`            | Handlers, add-mode routing, image preprocessing, JobQueue, lifecycle |
| `ai_client.py`       | Async Nebius client, retry/backoff, reasoning-starvation recovery    |
| `mcq.py`             | Page → MCQs, defensive JSON parsing, validation                     |
| `db.py`              | SQLite: question bank, attempts, review scheduling, preferences      |
| `quiz.py`            | Session flow, inline keyboards, scoring, MarkdownV2 rendering        |
| `formatter.py`       | LaTeX→Unicode, MarkdownV2 escaping, chunking                        |
| `exam_prompts.py`    | Grounding prompt and subject taxonomy                                |
| `prompts.py`         | Solver system prompt                                                 |
| `config.py`          | Env loading, validation, placeholder detection                       |
| `setup_botfather.py` | Pushes command menu and descriptions over the Bot API                |

Your question bank lives in `exam.db` (gitignored — it is personal data).

---

## Honest limitations

- **Question quality tracks page quality.** A crisp, flat, well-lit page produces good
  questions. A dim angled photo of a two-page spread produces mush.
- **Current affairs are out of scope.** No live news source, and the model has a
  knowledge cutoff. Photographing a monthly current-affairs magazine works; asking the
  bot "what happened this week" does not.
- **The bot only knows what you feed it.** It has no syllabus coverage map and will not
  tell you what you haven't studied.
- **Open-ended questions take ~20 s** because reasoning runs before any output. Straight
  computation is ~6 s.
- **Single-user by design.** Data is keyed by Telegram user id and it works fine for a
  few people, but there is no auth, quota, or admin layer.

---

## Roadmap

- [ ] Previous-year question import
- [ ] Timed full-length mock tests with a real 200-question paper structure
- [ ] Syllabus coverage map — what you've drilled vs what the exam covers
- [ ] Wolfram MCP integration for exact computation ([server](https://www.wolfram.com/artificial-intelligence/mcp/cloud/) is free and needs no auth)
- [ ] Telugu support for the SI language paper
- [ ] Export the bank to Anki

---

## Contributing

Issues and PRs welcome. The codebase is plain asyncio with no framework magic —
`main.py` is the only place handlers are registered, and every module is importable and
testable on its own.

## License

MIT. The exam content you generate belongs to you and to the publishers of the books you
photograph; this tool makes no claim to it.

<div align="center">
<sub>Built for one person's exam. Useful for anyone with a textbook and a phone.</sub>
</div>

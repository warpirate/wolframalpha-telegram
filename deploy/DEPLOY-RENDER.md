# Deploying free, with no credit card

Render + Neon. Both sign up with GitHub or email, neither asks for a card.

This is the fallback for when Oracle Cloud's card verification fails. It is slightly
more moving parts than a VM, but it is genuinely free and genuinely always-on.

| Piece | Job | Cost |
|---|---|---|
| **Render** | Runs the bot | Free (750 instance-hours/month — one service 24/7 fits) |
| **Neon** | Holds the question bank | Free (500 MB Postgres) |
| **UptimeRobot** | Stops Render sleeping | Free |

## Why the database is separate

Render's free tier has **no persistent disk**. The container's filesystem resets on
every restart and every deploy. If `exam.db` lived there, your entire question bank
would vanish on a redeploy — which defeats the point of the bot.

So the bot supports two backends:

```
DATABASE_URL set    → Postgres   (cloud)
DATABASE_URL unset  → SQLite     (laptop, unchanged)
```

Same code, same behaviour. You do not have to choose one forever.

---

## 1. Neon — the database

1. Sign up at [neon.tech](https://neon.tech) with GitHub. No card.
2. **Create project** → name it `prep-bot`, region **AWS ap-southeast-1 (Singapore)**
   (closest to India of the free options).
3. On the dashboard, copy the **connection string**. It looks like:

   ```
   postgresql://neondb_owner:XXXXXX@ep-xxx-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
   ```

Keep that tab open — you need the string in step 2.

> Treat it like a password. It is full read/write access to your question bank.

---

## 2. Render — the bot

1. Sign up at [render.com](https://render.com) with GitHub. No card.
2. **New → Web Service** → connect `warpirate/wolframalpha-telegram`.
3. Render reads [`render.yaml`](../render.yaml) and fills in the build and start
   commands. Confirm:
   - Runtime: **Python 3**
   - Build: `pip install -r requirements.txt`
   - Start: `python main.py`
   - Instance type: **Free**
4. Add **Environment Variables**:

   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `NEBIUS_API_KEY` | from Nebius AI Studio |
   | `DATABASE_URL` | the Neon string from step 1 |

5. **Create Web Service**. First build takes 2–4 minutes.

In the logs you want:

```
Storage backend: postgres
Postgres pool ready
Health endpoint listening on port 10000
Application started
```

`Storage backend: postgres` is the line that matters. If it says `sqlite`,
`DATABASE_URL` did not reach the process — recheck the env var spelling.

---

## 3. Stop the laptop bot ⚠️

**Telegram allows exactly one long-polling consumer per token.** Two instances and
both die with `Conflict: terminated by other getUpdates`.

On your laptop:

```
stop_bot.bat
uninstall_autostart.bat
```

---

## 4. UptimeRobot — stop the sleeping

Render's free tier spins a service down after **15 minutes** without an HTTP
request. A sleeping bot misses messages and misses the 6 AM quiz.

The bot serves a health endpoint at `/` precisely so something can poke it.

1. Copy your Render URL: `https://prep-bot-xxxx.onrender.com`
2. Sign up at [uptimerobot.com](https://uptimerobot.com) — free, no card.
3. **Add New Monitor**:
   - Type: **HTTP(s)**
   - URL: your Render URL
   - **Monitoring interval: 5 minutes**
4. Save.

750 free instance-hours per month covers one service running continuously
(~730 hours), so keeping it awake stays inside the free allowance.

---

## Verify

Message the bot. `/start` should answer in a second or two.

```
/add polity     →  photograph a page
/bank           →  confirms the questions were stored
```

Then, to prove persistence actually works: **Manual Deploy → Deploy latest commit**
in Render, wait for the restart, and run `/bank` again. The count should be
unchanged. That is the whole reason the database lives at Neon.

---

## Day-to-day

| Task | How |
|---|---|
| Logs | Render dashboard → **Logs** (live tail) |
| Redeploy | Push to `main` — Render auto-deploys |
| Restart | **Manual Deploy → Restart service** |
| Back up questions | Neon dashboard → **Backups**, or `pg_dump "$DATABASE_URL"` |

## Known rough edges

- **Cold start after sleep.** If UptimeRobot ever misses, the first message takes
  ~30 seconds while the container wakes.
- **Free Postgres idles.** Neon suspends a free database after inactivity; the first
  query wakes it in a second or two. `asyncpg` reconnects on its own.
- **Render's free build minutes are finite.** Pushing to `main` twenty times a day
  can exhaust them. Batch your commits.
- **One process only.** Scaling to 2 instances would run two pollers and break
  everything. Leave it at 1.

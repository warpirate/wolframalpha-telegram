# Deploying to Oracle Cloud (Always Free)

Runs the bot 24/7 at no cost. Oracle's Always Free tier does not expire and is not
a trial. A card is required at signup for identity verification; the Always Free
resources are not billed against it.

Total time: about 30 minutes, most of it waiting for the VM to provision.

---

## What you get

| | |
|---|---|
| Cost | ₹0/month, indefinitely |
| Uptime | 24/7, survives reboots via systemd |
| Storage | A real disk, so `exam.db` and your question bank persist |
| Scheduler | The 6 AM daily quiz fires whether or not your laptop is on |

---

## 1. Create the Oracle account

Sign up at [cloud.oracle.com](https://www.oracle.com/cloud/free/). Pick the **home
region** closest to you — `India South (Hyderabad)` or `India West (Mumbai)`.

> The home region cannot be changed later, and Always Free capacity is per-region.

After signup, make sure the account shows **Always Free** eligibility. If it ever
prompts to "upgrade to Pay As You Go", decline — upgrading ends the free resources.

---

## 2. Create the VM

**Menu → Compute → Instances → Create instance**

| Setting | Value |
|---|---|
| Name | `prep-bot` |
| Image | **Canonical Ubuntu 24.04** (change from the Oracle Linux default) |
| Shape | **VM.Standard.E2.1.Micro** — marked *Always Free eligible* |
| SSH keys | **Generate a key pair for me** → download **both** files |

### Which shape to pick

- **`VM.Standard.E2.1.Micro`** (AMD, 1 CPU, 1 GB RAM) — you get 2 of these free.
  Almost always available. **Use this.** 1 GB is plenty; the setup script adds swap.
- **`VM.Standard.A1.Flex`** (ARM, up to 4 CPU / 24 GB free) — far more powerful, but
  frequently returns **"Out of host capacity"** in Indian regions. Not worth the fight
  for a Telegram bot.

If you see *"Out of host capacity"*, switch to the other availability domain, or try
again later. The AMD micro shape rarely has this problem.

Save the downloaded **private key** somewhere safe — it is the only way in.

---

## 3. Connect

Note the instance's **Public IP address** from the console, then from your laptop:

```bash
# Windows (PowerShell or Git Bash), from wherever you saved the key
chmod 600 ssh-key-*.key          # Git Bash only; skip on PowerShell
ssh -i ssh-key-2026-09-22.key ubuntu@<PUBLIC_IP>
```

Username is `ubuntu` for Canonical Ubuntu images. Accept the host fingerprint prompt.

<details>
<summary>Permission denied?</summary>

The key file must not be world-readable. On Windows, right-click the key →
Properties → Security → Advanced → Disable inheritance → remove every entry except
your own user.

</details>

---

## 4. Install the bot

On the VM:

```bash
curl -fsSL https://raw.githubusercontent.com/warpirate/wolframalpha-telegram/main/deploy/setup.sh | bash
```

It installs Python, adds a 1 GB swap file, clones the repo, builds a virtualenv, and
then **stops** to tell you the `.env` is still a template. That is expected.

```bash
nano ~/prep-bot/.env
```

Fill in:

```env
TELEGRAM_BOT_TOKEN=<your token from BotFather>
NEBIUS_API_KEY=<your Nebius key>
```

`Ctrl+O`, `Enter`, `Ctrl+X` to save and exit. Then run the installer again:

```bash
bash ~/prep-bot/deploy/setup.sh
```

This time it installs the systemd service and starts the bot.

> The `.env` is created with `chmod 600` — readable only by you.

---

## 5. Stop the laptop instance ⚠️

**Telegram allows exactly one long-polling consumer per bot token.** Two running
instances means both break with `Conflict: terminated by other getUpdates`.

On your laptop, before or right after the cloud bot starts:

```
stop_bot.bat
uninstall_autostart.bat
```

---

## 6. Verify

```bash
journalctl -u prep-bot -f
```

You want:

```
Starting exam-prep bot…
Nebius client ready (model=deepseek-ai/DeepSeek-V4.1-Flash, ...)
Restored 0 daily quiz schedule(s)
Application started
```

Then message the bot on Telegram. `/start` should answer within a second or two.

`Ctrl+C` stops following the logs — it does not stop the bot.

---

## Day-to-day

| Task | Command |
|---|---|
| Live logs | `journalctl -u prep-bot -f` |
| Recent errors | `journalctl -u prep-bot -p err -n 50` |
| Restart | `sudo systemctl restart prep-bot` |
| Stop | `sudo systemctl stop prep-bot` |
| Deploy new code | `bash ~/prep-bot/deploy/update.sh` |
| Back up your questions | `scp -i key.key ubuntu@<IP>:~/prep-bot/exam.db .` |

Back up `exam.db` occasionally. It holds every question you have photographed and
every answer you have given, and it exists in exactly one place.

---

## Notes

**Timezone.** The daily quiz uses an explicit IST offset in code, so it fires at the
right local time regardless of the VM's clock. No configuration needed.

**No inbound ports.** The bot only makes outbound HTTPS connections to Telegram and
Nebius. Leave Oracle's security lists alone — nothing needs opening, which is one
less thing to get wrong.

**Idle reclamation.** Oracle may reclaim Always Free *compute* instances that are
idle for 7 days — but this applies to the ARM A1 shapes, and a bot that polls
Telegram continuously is not idle. Your instance will not be reclaimed for inactivity.

**If the VM dies.** Everything is reproducible: create a new instance, run
`setup.sh`, restore `exam.db` from your backup. That is the whole recovery procedure.

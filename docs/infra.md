# Infrastructure

## Target (deployed 2026-11-26)

* GCP Compute Engine `e2-micro` (2 shared vCPU, 1 GB), region `us-central1`, Debian 13 (trixie),
  30 GB **standard** persistent disk. Always Free regions: `us-central1`, `us-west1`, `us-east1`.
* 2 GB swap file, journald capped at 200 MB.
* One resident unit (`jobpipe-bot`) and oneshot units on timers (`jobpipe-queue` every minute,
  `jobpipe-backup` 03:00 and `jobpipe-retention` 04:00 Almaty, i.e. 22:00 and 23:00 UTC).

## Free Tier terms: verify at deploy time

Free Tier terms change. Check the current
[Google Cloud Free Tier page](https://cloud.google.com/free/docs/free-cloud-features#compute) and
record what it says here, with the date:

| item | expected | verified on / result |
|---|---|---|
| e2-micro instance-hours in the free regions | 1 instance per month | the VM form's estimate shows the list price ($6.11/month); check Billing → Reports a day after creation |
| standard persistent disk | 30 GB-month | 2026-11-26: the form shows $0.00 for 30 GB standard |
| outbound data transfer | Standard network tier: "200 GB / mo free in every region" (shown in the form) | 2026-11-26: Standard tier selected |
| external IPv4 address on the free VM | may be billed separately | _check Billing → Reports_ |
| Cloud Storage for backups | 5 GB-month in US regions | _todo_ |

Keep a budget alert (1 USD) on the billing account so any charge is noticed at once.

## Creating the VM (console choices that matter)

| section | setting | why |
|---|---|---|
| Machine configuration | series E2, `e2-micro` | the only free machine type |
| Advanced → provisioning model | **Standard**, not Spot | Spot VMs can be stopped at any time |
| Advanced → on host maintenance | Migrate VM instance; automatic restart **On** | survives maintenance and crashes |
| OS and storage | Debian 13, **Standard persistent disk**, 30 GB | Balanced is not free |
| Data protection | **No backups** | snapshots are billed; the DB is backed up nightly by jobpipe |
| Networking | no HTTP/HTTPS firewall rules; external IPv4 Ephemeral; **Standard** network tier | nothing listens publicly; Standard tier has the larger free egress |
| Observability | Ops Agent **off** | it uses ~100 MB of the 1 GB |
| Security | default service account, default access scopes | enough for now; GCS write comes with backup upload |

## Install

SSH in from the console, then:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/zhandosayupov/job-pipeline.git /tmp/jp
sudo bash /tmp/jp/ops/provision.sh
```

Then add the secrets and the owner id, and start the bot:

```bash
sudo nano /etc/jobpipe/secrets.env                    # TELEGRAM_BOT_TOKEN=..., GEMINI_API_KEY=...
sudo nano /var/lib/jobpipe/config/settings.yaml       # telegram.owner_id: <your numeric id>
sudo systemctl enable --now jobpipe-bot.service
curl -s localhost:8080/healthz; echo
```

A shell helper for the owner (put it in `~/.bashrc`):

```bash
jp() { sudo -u jobpipe env JOBPIPE_HOME=/var/lib/jobpipe JOBPIPE_SECRETS=/etc/jobpipe/secrets.env \
  /opt/jobpipe/.venv/bin/jobpipe "$@"; }
```

## Gemini key

Create the key in AI Studio in a project **without** billing: the key list must show "Free tier".
A project linked to a billing account is on a paid tier and needs a prepaid credit balance; with
$0 it does not answer. Free-tier prompts may be used for training, hence `trains_on_data: true`.

Model ids change; list them before editing `llm.yaml`:

```bash
sudo bash -c 'source /etc/jobpipe/secrets.env; printf "url = \"https://generativelanguage.googleapis.com/v1beta/openai/models\"\nsilent\nheader = \"Authorization: Bearer %s\"\n" "$GEMINI_API_KEY" | curl --config -' | grep -o '"id": *"[^"]*flash[^"]*"'
```

The key goes to curl on stdin (`--config -`; `printf` is a bash builtin), never on its command line,
where `ps` would show it to other users of the machine. Use the same pattern for any manual check.

On 2026-11-26 `gemini-flash-latest` answered `503 high demand` or timed out, while
`gemini-flash-lite-latest` answered in about 4 s; the example config tries lite first.
Check: `jp llm ping`.

## S0 demo

```bash
jp task add selftest.fail
# within ~2 minutes: 4 attempts with growing delays, then a dead-letter alert in Telegram
jp task add selftest.sleep --payload '{"seconds": 120}'
# once /status shows it running, send /pause: the reply comes in <= 10 s; then /resume
```

## Collection (S1)

```bash
jp run collect --dry-run                  # live fetch into a copy of the database, prints per-source stats
jp run collect --dry-run --sources hn     # one source
jp run digest --dry-run                   # print what the 09:00 digest would send
jp jobs dropped                           # why jobs were filtered out
```

Timers: `jobpipe-collect.timer` (08, 11, 14, 17, 20, 23 Almaty), `jobpipe-label.timer` (20 minutes
later) and `jobpipe-digest.timer` (09:00).

## Labeling (S2)

```bash
jp run label --limit 20                   # one small batch to check the LLM and the prompt
```

Bot: `/shortlist`, `/funnel`, `/rate` (rate 30 labels: the S2 done-condition is >= 80% agreement),
`/protect <company>`, `/unprotect <company>`. Copy `config.example/protected.yaml` to
`/var/lib/jobpipe/config/` and add companies you are in a process with.

## Operations

* Logs: `journalctl -u jobpipe-bot -u jobpipe-queue -f -o cat` (JSON lines, secrets masked).
* Update: re-run `sudo bash /tmp/jp/ops/provision.sh` after `git -C /tmp/jp pull` (or a fresh
  clone). It stops the units, swaps in a fresh root-owned clone, syncs, and starts them again.
* Backups: `/var/lib/jobpipe/data/backups/`, 7 kept. Off-site upload (age + GCS) is configured with
  `backup.upload_cmd` once the bucket exists.

## Knowledge base and tailoring (S3)

The knowledge base is yours alone and lives in `/var/lib/jobpipe/kb/`. Use `config.example/kb/` as
the format reference (it describes a made-up person: never copy it as is):

```bash
sudo -u jobpipe mkdir -p /var/lib/jobpipe/kb
sudo -u jobpipe nano /var/lib/jobpipe/kb/profile.yaml   # then facts.yaml, answers.yaml
jp kb check                                            # must print no "!" lines
jp run tailor --limit 1                                # one draft, card with CV + letter
```

Tailoring sends your CV facts to the LLM, pseudonymized. With only the free Gemini key (which may
train on prompts) set `personal_on_training_providers: true` in `llm.yaml` to allow it, or add a
provider that does not train on prompts. Without `tectonic` installed the card carries the `.tex`.

## Browser worker on the laptop (S4)

1. **Tailscale** on the VM and the laptop (same account). Note the VM's Tailscale IP (`tailscale ip -4`).
2. **On the VM**: add a token to `/etc/jobpipe/secrets.env` and listen on the Tailscale address:
   ```bash
   python3 -c "import secrets; print('WORKER_TOKEN=' + secrets.token_urlsafe(32))" | sudo -u jobpipe tee -a /etc/jobpipe/secrets.env
   sudo -u jobpipe nano /var/lib/jobpipe/config/settings.yaml   # http.host: <VM tailscale IP>
   sudo systemctl restart jobpipe-bot
   ```
   Never open port 8080 in the GCP firewall: the API is for the tailnet only.
3. **On the laptop** (PowerShell, in the repository folder):
   ```powershell
   uv sync --group worker
   uv run playwright install chromium
   mkdir $env:USERPROFILE\.jobpipe-worker
   notepad $env:USERPROFILE\.jobpipe-worker\worker.env   # JOBPIPE_CORE_URL=http://<VM tailscale IP>:8080 and WORKER_TOKEN=...
   uv run jobpipe worker --login hh --login-url https://hh.kz/account/login   # log in once, press Enter
   powershell -ExecutionPolicy Bypass -File ops\worker\install-worker.ps1
   ```
4. **Dry runs first**: every platform starts with DRY_RUN on. Approve a CV card, wait for `jp run apply`
   (or run it), check the dry-run report and screenshot. After 10 correct dry runs: `/dryrun hh off`.

## Inbox (S5)

1. **Gmail app password**: Google Account → Security → 2-Step Verification → App passwords. Then on the VM:
   ```bash
   sudo -u jobpipe nano /etc/jobpipe/secrets.env      # GMAIL_APP_PASSWORD=<the 16-letter app password>
   sudo -u jobpipe nano /var/lib/jobpipe/config/settings.yaml   # inbox.gmail_user (or the email in kb/profile.yaml)
   jp run inbox                                      # first run reads the last 14 days, job mail only
   ```
   Read `/inbox` after the first runs: rules were checked on synthetic mail only.
2. **Telegram userbot** (laptop, secondary account): get API id/hash at my.telegram.org, add
   `TG_API_ID` and `TG_API_HASH` to `worker.env`, then:
   ```powershell
   uv sync --group userbot
   uv run jobpipe userbot --login
   powershell -ExecutionPolicy Bypass -File ops\worker\install-worker.ps1 -Userbot
   ```

## Self-check and restore test (S6)

* `jp selfcheck` prints every check; the hourly timer alerts once per failure and once on recovery.
* Dead-man's switch: create a free check on healthchecks.io (period 1 h, grace 2 h), put its ping URL
  in `settings.yaml: selfcheck.ping_url`. If the VM dies, that service e-mails you.
* Restore test, e.g. monthly: `jp backup --verify`.

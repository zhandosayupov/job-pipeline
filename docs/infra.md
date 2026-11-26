# Infrastructure

## Target

* GCP Compute Engine `e2-micro`, Debian 12, 30 GB standard persistent disk, region `us-central1`
  (or `us-west1` / `us-east1`: the Always Free regions).
* 2 GB swap file, journald capped at 200 MB.
* One resident unit (`jobpipe-bot`) and oneshot units on timers (`jobpipe-queue`, `jobpipe-backup`).

## Free Tier terms: verify at deploy time

Free Tier terms change. Before creating the VM, check the current
[Google Cloud Free Tier page](https://cloud.google.com/free/docs/free-cloud-features#compute) and
record what it says here, with the date:

| item | expected | verified on / result |
|---|---|---|
| e2-micro instance-hours in the free regions | 1 instance per month | _todo_ |
| standard persistent disk | 30 GB-month | _todo_ |
| outbound data transfer | 1 GB/month (excluding China and Australia) | _todo_ |
| external IPv4 address on the free VM | may be billed separately: check | _todo_ |
| Cloud Storage for backups | 5 GB-month in US regions | _todo_ |

Set a budget alert (for example 1 USD) on the billing account so any charge is noticed at once.

## Steps (owner does the account parts)

1. Create the GCP project and billing account, and set the budget alert. (Owner.)
2. Create the VM: `e2-micro`, Debian 12, 30 GB standard disk, free region, no extra services.
3. SSH in, then:
   ```bash
   sudo apt-get install -y git
   git clone https://github.com/zhandosayupov/job-pipeline.git /tmp/jp
   sudo bash /tmp/jp/ops/provision.sh
   ```
4. Fill `/etc/jobpipe/secrets.env` (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`) and set
   `telegram.owner_id` in `/var/lib/jobpipe/config/settings.yaml`. Re-run `provision.sh`: it starts
   the bot once both are set.
5. Check: `/status` in Telegram, `curl -s localhost:8080/healthz`, `systemctl list-timers 'jobpipe-*'`.

## S0 demo script

```bash
jp() { sudo -u jobpipe env JOBPIPE_HOME=/var/lib/jobpipe JOBPIPE_SECRETS=/etc/jobpipe/secrets.env \
  /opt/jobpipe/.venv/bin/jobpipe "$@"; }
jp task add selftest.fail
# within ~2 minutes: 4 attempts with growing delays, then a dead-letter alert in Telegram
jp task add selftest.sleep --payload '{"seconds": 120}'
# wait for the next minute tick, then /pause in Telegram: the run stops in <= 10 s
```

## Operations

* Logs: `journalctl -u jobpipe-bot -u jobpipe-queue -f -o cat` (JSON lines, secrets masked).
* Update: re-run `provision.sh` (pulls, syncs, reinstalls units, restarts the bot).
* Backups: `/var/lib/jobpipe/data/backups/`, 7 kept. Off-site upload (age + GCS) is configured with
  `backup.upload_cmd` once the bucket exists.

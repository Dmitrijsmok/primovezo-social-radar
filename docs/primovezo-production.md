# Primovezo Social Radar production schedule

The Primovezo runner is intended to run once per day and send at most one
**internal** ecommerce-lead digest to `HARKEN_RESEND_TO`. It never contacts a prospect automatically.

## 1. Configure the internal Resend digest

Keep the real values only in the repository's local `.env`:

```dotenv
HARKEN_RESEND_API_KEY=re_...
HARKEN_RESEND_FROM=noreply@primovezo.com
HARKEN_RESEND_TO=<your-internal-email>
```

`HARKEN_RESEND_TO` is an internal Primovezo recipient. Do not put prospect
addresses there. The runner never emails a social-network author.

The sending domain must be verified in Resend before using
`noreply@primovezo.com`.

Verify the exact digest and operational-warning delivery without scanning social networks:

```bash
uv run harken test-alert --transport resend --kind lead
uv run harken test-alert --transport resend --kind operational
```

The Resend request uses an idempotency key derived from the digest contents and
delivery target. Identical retries within Resend's idempotency window therefore
do not create a second copy of the same digest.

SMTP remains supported by general Harken alerting, but it is not required for
the Primovezo production setup.

## 2. Install the short `harken` command

Install a repo-aware wrapper once:

```bash
bash scripts/install-harken-command.sh
```

It keeps the repository as the working directory, so the local `.env` and
`harken.db` are used even when the command is called from another directory.

Useful shortcuts:

```bash
harken logs
harken logs --min-score 80
harken leads reclassify
```

`harken logs` is the short form of `harken leads report`. Reclassification
updates stored lead analysis without fetching social networks or sending a digest.

## 3. Verify a manual scan

Threads is included automatically when this is present in the local `.env`:

```dotenv
HARKEN_THREADS_ACCESS_TOKEN=<token>
```

Without that variable, Primovezo continues with Bluesky only and prints that Threads
is disabled.

Use a **long-lived** Threads token. Before every Primovezo daily/recent scan, Harken
checks its validity, `threads_keyword_search` scope, and expiry. If fewer than 14 days
remain, Harken calls the Threads refresh endpoint and atomically updates only
`HARKEN_THREADS_ACCESS_TOKEN` in the repository's local `.env`. The current token
remains in use if a refresh attempt fails while it is still valid, so the next daily
run can retry.

Check token health at any time without exposing the secret:

```bash
harken threads status
```

```bash
uv run harken leads primovezo --limit 10
harken logs
```

To inspect a bounded recent window without moving the daily cursor or sending email:

```bash
harken leads recent --days 5
harken logs
```

The scan stores qualified leads even if Resend is not configured. With Resend
configured, the full scan sends one de-duplicated internal digest only when
there are new or previously queued qualified leads.

The same internal Resend recipient also receives **one operational warning per
daily run** when the scan completes only partially, for example after a source
still fails after retries, the lead classifier fails, or a Threads token refresh
cannot be completed. Healthy runs do not send an operational email.

The scheduled shell wrapper separately sends a failure alert if the Harken
process itself exits non-zero or cannot start. The daily process is capped at
90 minutes by default so a stuck run also becomes a visible failure. This
fallback notifier reads only the local `.env` and uses Python's standard
library, so it does not depend on the Harken package or `uv` being healthy.

A local failure notifier cannot report a total server outage or a timer that
never starts at all. That case requires an external dead-man/heartbeat monitor.

## 4. Install the daily user timer

```bash
bash scripts/install-primovezo-daily-timer.sh
```

The installer uses the repository's actual path and creates a user-level
systemd service and timer. The default schedule is **09:00 Europe/Riga**, with
up to 10 minutes of randomized delay to avoid hitting public sources at exactly
the same second every day.

On a server where the user must keep timers running while logged out, enable
linger once:

```bash
sudo loginctl enable-linger "$USER"
```

Useful checks:

```bash
systemctl --user status primovezo-social-radar.timer
systemctl --user list-timers primovezo-social-radar.timer
journalctl --user -u primovezo-social-radar.service -n 100 --no-pager
```

Run the production service immediately without waiting for the timer:

```bash
systemctl --user start primovezo-social-radar.service
```

Disable scheduling without deleting configuration:

```bash
systemctl --user disable --now primovezo-social-radar.timer
```

The runner uses a local `.primovezo-social-radar.lock` file with `flock`.
If a previous scan is still running, a second scheduled invocation exits
cleanly instead of launching an overlapping scan.

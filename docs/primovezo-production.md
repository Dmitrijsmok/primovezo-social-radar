# Primovezo Social Radar production schedule

The Primovezo runner is intended to run once per day and send at most one
**internal** ecommerce-lead digest to `HARKEN_RESEND_TO`. It never contacts a
prospect automatically.

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

Verify the exact digest format without scanning social networks:

```bash
uv run harken test-alert --transport resend --kind lead
```

The Resend request uses an idempotency key derived from the digest contents and
delivery target. Identical retries within Resend's idempotency window therefore
do not create a second copy of the same digest.

SMTP remains supported by general Harken alerting, but it is not required for
the Primovezo production setup.

## 2. Verify a manual scan

```bash
uv run harken leads primovezo --limit 10
uv run harken leads report
```

The scan stores qualified leads even if Resend is not configured. With Resend
configured, the full scan sends one de-duplicated internal digest only when
there are new or previously queued qualified leads.

## 3. Install the daily user timer

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

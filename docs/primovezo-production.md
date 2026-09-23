# Primovezo Social Radar production schedule

The Primovezo runner is intended to run once per day and send at most one
**internal** ecommerce-lead digest to `HARKEN_EMAIL_TO`. It never contacts a
prospect automatically.

## 1. Configure the internal email

Keep the real values only in the repository's local `.env`:

```dotenv
HARKEN_EMAIL_TO=you@example.com
HARKEN_EMAIL_FROM=info@primovezo.lv
HARKEN_SMTP_HOST=<smtp-host>
HARKEN_SMTP_PORT=587
HARKEN_SMTP_SECURITY=starttls
HARKEN_SMTP_USERNAME=<smtp-user>
HARKEN_SMTP_PASSWORD=<smtp-password>
```

The recipient should be an internal Primovezo mailbox. Do not put prospect
addresses in `HARKEN_EMAIL_TO`.

Verify the exact digest format without scanning social networks:

```bash
uv run harken test-alert --transport email --kind lead
```

## 2. Verify a manual scan

```bash
uv run harken leads primovezo --limit 10
uv run harken leads report
```

The scan stores qualified leads even if SMTP is not configured. With SMTP
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

#!/usr/bin/env python3
"""Best-effort Primovezo operational alert that does not depend on Harken/uv."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--detail", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    env = _read_dotenv(root / ".env")
    api_key = env.get("HARKEN_RESEND_API_KEY", "").strip()
    sender = env.get("HARKEN_RESEND_FROM", "noreply@primovezo.com").strip()
    recipients = [
        address.strip()
        for address in env.get("HARKEN_RESEND_TO", "").split(",")
        if address.strip()
    ]

    if not api_key or not recipients:
        print(
            "Cannot send Primovezo failure alert: Resend is not configured.",
            file=sys.stderr,
        )
        return 2

    timestamp = datetime.now(timezone.utc).isoformat()
    host = socket.gethostname()
    detail = " ".join(args.detail.split())[:500]
    subject = "[Primovezo Social Radar] Daily job failed"
    body = "\n".join(
        [
            "Primovezo Social Radar scheduled job did not complete successfully.",
            "",
            f"Server: {host}",
            f"Time: {timestamp}",
            f"Reason: {detail}",
            "",
            "Check:",
            "journalctl --user -u primovezo-social-radar.service -n 100 --no-pager",
        ]
    )
    identity = f"{host}|{timestamp[:13]}|{detail}"
    idempotency = "primovezo-job-failure/" + hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:48]

    payload = json.dumps(
        {
            "from": sender,
            "to": recipients,
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency,
            "User-Agent": "Primovezo-Social-Radar/ops-fallback",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            if response.status >= 400:
                raise RuntimeError(f"Resend returned HTTP {response.status}")
    except HTTPError as exc:
        print(f"Failure alert delivery failed: Resend HTTP {exc.code}", file=sys.stderr)
        return 3
    except (URLError, OSError, RuntimeError) as exc:
        print(
            f"Failure alert delivery failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 3

    print("Primovezo failure alert sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

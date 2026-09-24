"""Durable outbound alerts for newly ingested negative mentions."""

from __future__ import annotations

import hashlib
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlsplit

import httpx

from harken.models import Mention
from harken.sources.base import USER_AGENT


class WebhookDeliveryError(RuntimeError):
    """A sanitized delivery error that never includes the secret webhook URL."""


class EmailDeliveryError(RuntimeError):
    """A sanitized delivery error that never includes SMTP credentials."""


class ResendDeliveryError(RuntimeError):
    """A sanitized delivery error that never includes the Resend API key."""


@dataclass(frozen=True)
class ResendSettings:
    """Validated Resend configuration for internal digest delivery."""

    api_key: str
    sender: str
    recipients: tuple[str, ...]
    timeout: float = 15.0

    def validated(self) -> ResendSettings:
        api_key = self.api_key.strip()
        sender = self.sender.strip()
        recipients = tuple(dict.fromkeys(address.strip() for address in self.recipients))
        if not api_key:
            raise ValueError("HARKEN_RESEND_API_KEY must not be empty")
        _validate_email_address(sender, "HARKEN_RESEND_FROM")
        if not recipients:
            raise ValueError("HARKEN_RESEND_TO must contain at least one address")
        for recipient in recipients:
            _validate_email_address(recipient, "HARKEN_RESEND_TO")
        if self.timeout <= 0:
            raise ValueError("Resend timeout must be greater than 0")
        return ResendSettings(
            api_key=api_key,
            sender=sender,
            recipients=recipients,
            timeout=self.timeout,
        )


@dataclass(frozen=True)
class EmailSettings:
    """Validated SMTP connection and recipient configuration."""

    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    security: str = "starttls"
    username: str | None = None
    password: str | None = None
    timeout: float = 15.0

    def validated(self) -> EmailSettings:
        host = self.host.strip()
        sender = self.sender.strip()
        recipients = tuple(dict.fromkeys(address.strip() for address in self.recipients))
        security = self.security.strip().lower()
        if not host or any(character.isspace() for character in host):
            raise ValueError("HARKEN_SMTP_HOST must be a non-empty hostname")
        if not 1 <= self.port <= 65535:
            raise ValueError("HARKEN_SMTP_PORT must be between 1 and 65535")
        if security not in {"starttls", "ssl", "none"}:
            raise ValueError("HARKEN_SMTP_SECURITY must be starttls, ssl, or none")
        if not recipients:
            raise ValueError("HARKEN_EMAIL_TO must contain at least one address")
        _validate_email_address(sender, "HARKEN_EMAIL_FROM")
        for recipient in recipients:
            _validate_email_address(recipient, "HARKEN_EMAIL_TO")
        if bool(self.username) != bool(self.password):
            raise ValueError("HARKEN_SMTP_USERNAME and HARKEN_SMTP_PASSWORD must be set together")
        if self.timeout <= 0:
            raise ValueError("SMTP timeout must be greater than 0")
        return EmailSettings(
            host=host,
            port=self.port,
            sender=sender,
            recipients=recipients,
            security=security,
            username=self.username,
            password=self.password,
            timeout=self.timeout,
        )


def webhook_target_key(url: str) -> str:
    """Stable, non-secret identifier used by the delivery outbox."""
    _validate_webhook_url(url)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def email_target_key(settings: EmailSettings) -> str:
    """Stable delivery identifier excluding the SMTP password."""
    configured = settings.validated()
    identity = json.dumps(
        {
            "host": configured.host.lower(),
            "port": configured.port,
            "security": configured.security,
            "sender": configured.sender,
            "recipients": sorted(configured.recipients, key=str.casefold),
            "username": configured.username,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "email-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def resend_target_key(settings: ResendSettings) -> str:
    """Stable delivery identifier excluding the Resend API key."""
    configured = settings.validated()
    identity = json.dumps(
        {
            "sender": configured.sender.lower(),
            "recipients": sorted(configured.recipients, key=str.casefold),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "resend-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def send_negative_alert(url: str, query: str, mentions: list[Mention]) -> None:
    """Deliver one batch to a generic webhook or Slack incoming webhook."""
    if not mentions:
        return
    text = _alert_text(query, mentions)
    _deliver_webhook(
        url,
        {
            "text": text,
            "event": "harken.negative_mentions",
            "query": query,
            "count": len(mentions),
            "mentions": [_mention_payload(mention) for mention in mentions],
        },
    )


def send_threshold_alert(url: str, text: str, payload: dict) -> None:
    """Deliver a persisted volume/sentiment event through the same transport."""
    _deliver_webhook(url, {"text": text, **payload})


def send_negative_email(settings: EmailSettings, query: str, mentions: list[Mention]) -> None:
    """Deliver one negative-mention batch as a plain-text email."""
    if not mentions:
        return
    count = len(mentions)
    subject = (
        f"[Harken] {count} new negative mention{'s' if count != 1 else ''}: {_safe_header(query)}"
    )
    _deliver_email(settings, subject, _alert_text(query, mentions))


def send_lead_alert(url: str, query: str, mentions: list[Mention]) -> None:
    """Deliver one lead-candidate batch to a generic webhook or Slack."""
    if not mentions:
        return
    text = _lead_alert_text(query, mentions)
    _deliver_webhook(
        url,
        {
            "text": text,
            "event": "harken.lead_candidates",
            "query": query,
            "count": len(mentions),
            "mentions": [_mention_payload(mention) for mention in mentions],
        },
    )


def send_lead_email(settings: EmailSettings, query: str, mentions: list[Mention]) -> None:
    """Deliver one lead-candidate batch as a plain-text email."""
    if not mentions:
        return
    count = len(mentions)
    subject = (
        f"[Social Radar] {count} new lead candidate{'s' if count != 1 else ''}: "
        f"{_safe_header(query)}"
    )
    _deliver_email(settings, subject, _lead_alert_text(query, mentions))


def send_lead_digest_email(settings: EmailSettings, mentions: list[Mention]) -> None:
    """Deliver one internal Primovezo digest of qualified ecommerce leads."""
    if not mentions:
        return
    count = len(mentions)
    subject = f"[Primovezo Social Radar] {count} new ecommerce lead{'s' if count != 1 else ''}"
    _deliver_email(settings, subject, _lead_alert_text("daily ecommerce scan", mentions))


def send_lead_digest_resend(settings: ResendSettings, mentions: list[Mention]) -> None:
    """Deliver one internal Primovezo lead digest through the Resend Email API."""
    if not mentions:
        return
    configured = settings.validated()
    count = len(mentions)
    subject = f"[Primovezo Social Radar] {count} new ecommerce lead{'s' if count != 1 else ''}"
    body = _lead_alert_text("daily ecommerce scan", mentions)
    digest_identity = "|".join(sorted(f"{mention.source}:{mention.id}" for mention in mentions))
    idempotency_key = (
        "primovezo-lead-digest/"
        + hashlib.sha256(
            (
                configured.sender
                + "|"
                + ",".join(sorted(configured.recipients, key=str.casefold))
                + "|"
                + digest_identity
            ).encode("utf-8")
        ).hexdigest()[:48]
    )
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {configured.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": USER_AGENT,
            },
            json={
                "from": configured.sender,
                "to": list(configured.recipients),
                "subject": subject,
                "text": body,
            },
            timeout=configured.timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ResendDeliveryError(f"Resend returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError as exc:
        raise ResendDeliveryError(f"Resend request failed: {type(exc).__name__}") from None


def send_live_test_resend(
    settings: ResendSettings,
    mentions: list[Mention],
    *,
    fetched: int,
    qualified: int,
    sources: list[str],
    issues: list[str] | None = None,
) -> None:
    """Send one clearly marked live-source diagnostic digest through Resend."""
    configured = settings.validated()
    run_at = datetime.now(timezone.utc)
    cleaned_issues = [issue.strip() for issue in issues or [] if issue and issue.strip()]
    source_names = list(dict.fromkeys(source.strip() for source in sources if source.strip()))
    subject = f"[Primovezo Social Radar TEST] live scan: {fetched} fetched, {qualified} qualified"
    body = _live_test_text(
        mentions,
        fetched=fetched,
        qualified=qualified,
        sources=source_names,
        issues=cleaned_issues,
        run_at=run_at,
    )
    identity = (
        configured.sender
        + "|"
        + ",".join(sorted(configured.recipients, key=str.casefold))
        + "|"
        + run_at.isoformat()
        + "|"
        + body
    )
    idempotency_key = (
        "primovezo-live-test/" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:48]
    )
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {configured.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": USER_AGENT,
            },
            json={
                "from": configured.sender,
                "to": list(configured.recipients),
                "subject": subject,
                "text": body,
            },
            timeout=configured.timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ResendDeliveryError(f"Resend returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError as exc:
        raise ResendDeliveryError(f"Resend request failed: {type(exc).__name__}") from None


def send_operational_resend(
    settings: ResendSettings,
    *,
    issues: list[str],
    run_label: str = "daily scan",
) -> None:
    """Send one internal operational warning for a Primovezo radar run."""
    cleaned = [issue.strip() for issue in issues if issue and issue.strip()]
    if not cleaned:
        return

    configured = settings.validated()
    subject = f"[Primovezo Social Radar] Operational warning: {_safe_header(run_label)}"
    lines = [
        "Primovezo Social Radar completed with an operational problem.",
        "",
        f"Run: {run_label}",
        f"Issues: {len(cleaned)}",
        "",
    ]
    lines.extend(f"• {issue}" for issue in cleaned[:20])
    if len(cleaned) > 20:
        lines.append(f"…and {len(cleaned) - 20} more issue(s)")
    lines.extend(
        [
            "",
            "The lead scan may be incomplete.",
            "Check: journalctl --user -u primovezo-social-radar.service -n 100 --no-pager",
        ]
    )
    body = "\n".join(lines)
    digest_identity = "|".join(cleaned)
    run_date = datetime.now(timezone.utc).date().isoformat()
    idempotency_key = (
        "primovezo-ops-warning/"
        + hashlib.sha256(
            (
                configured.sender
                + "|"
                + ",".join(sorted(configured.recipients, key=str.casefold))
                + "|"
                + run_date
                + "|"
                + run_label
                + "|"
                + digest_identity
            ).encode("utf-8")
        ).hexdigest()[:48]
    )

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {configured.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
                "User-Agent": USER_AGENT,
            },
            json={
                "from": configured.sender,
                "to": list(configured.recipients),
                "subject": subject,
                "text": body,
            },
            timeout=configured.timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ResendDeliveryError(f"Resend returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError as exc:
        raise ResendDeliveryError(f"Resend request failed: {type(exc).__name__}") from None


def send_threshold_email(settings: EmailSettings, text: str, payload: dict) -> None:
    """Deliver a persisted volume/sentiment threshold episode by email."""
    event = str(payload.get("event", "harken.threshold_alert")).removeprefix("harken.")
    query = _safe_header(str(payload.get("query", "tracked keyword")))
    details = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    _deliver_email(
        settings,
        f"[Harken] {event.replace('_', ' ')}: {query}",
        f"{text}\n\nEvent details:\n{details}",
    )


def _deliver_webhook(url: str, payload: dict) -> None:
    parsed = _validate_webhook_url(url)
    body = (
        {"text": payload["text"]}
        if parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
        else payload
    )
    try:
        response = httpx.post(
            url,
            json=body,
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise WebhookDeliveryError(f"webhook returned HTTP {exc.response.status_code}") from None
    except httpx.RequestError as exc:
        raise WebhookDeliveryError(f"webhook request failed: {type(exc).__name__}") from None


def _deliver_email(settings: EmailSettings, subject: str, body: str) -> None:
    configured = settings.validated()
    message = EmailMessage()
    message["From"] = configured.sender
    message["To"] = ", ".join(configured.recipients)
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    try:
        if configured.security == "ssl":
            client = smtplib.SMTP_SSL(
                configured.host,
                configured.port,
                timeout=configured.timeout,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                configured.host,
                configured.port,
                timeout=configured.timeout,
            )
        with client as smtp:
            if configured.security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if configured.username:
                smtp.login(configured.username, configured.password or "")
            smtp.send_message(
                message,
                from_addr=configured.sender,
                to_addrs=list(configured.recipients),
            )
    except smtplib.SMTPResponseException as exc:
        raise EmailDeliveryError(f"SMTP server returned {exc.smtp_code}") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailDeliveryError(f"email delivery failed: {type(exc).__name__}") from None


def _validate_webhook_url(url: str):
    try:
        parsed = urlsplit(url)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HARKEN_WEBHOOK_URL must be an absolute http(s) URL")
    return parsed


def _validate_email_address(value: str, variable: str) -> None:
    if not re.fullmatch(r"[^\s@<>,;:]+@[^\s@<>,;:]+", value):
        raise ValueError(f"{variable} contains an invalid email address")


def _safe_header(value: str) -> str:
    return " ".join(value.split())[:120] or "tracked keyword"


def _alert_text(query: str, mentions: list[Mention]) -> str:
    count = len(mentions)
    lines = [f"Harken: {count} new negative mention{'s' if count != 1 else ''} for “{query}”"]
    for mention in mentions[:10]:
        excerpt = " ".join(mention.content.split())[:180]
        source = mention.source
        if mention.author:
            source += f" · {mention.author}"
        line = f"• {source}: {excerpt}"
        if mention.url:
            line += f" — {mention.url}"
        lines.append(line)
    if count > 10:
        lines.append(f"…and {count - 10} more")
    return "\n".join(lines)


def _lead_alert_text(query: str, mentions: list[Mention]) -> str:
    count = len(mentions)
    lines = [f"Social Radar: {count} new lead candidate{'s' if count != 1 else ''} for “{query}”"]
    for mention in mentions[:10]:
        excerpt = " ".join(mention.content.split())[:500]
        source = mention.source
        if mention.author:
            source += f" · {mention.author}"
        if mention.lead_score is None:
            lines.extend(
                [
                    "",
                    f"• {source}",
                    "  AI classification unavailable; review this keyword match manually.",
                    f"  {excerpt}",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    f"• {source} · score {mention.lead_score}/100"
                    + (f" · {mention.lead_category}" if mention.lead_category else ""),
                    f"  {mention.lead_reason or 'No reason supplied.'}",
                    f"  {excerpt}",
                ]
            )
            if mention.conversation:
                lines.append("  Conversation context:")
                lines.extend(_conversation_text_lines(mention))
            else:
                lines.append(f"  {excerpt}")
            if mention.suggested_reply:
                lines.append(f"  Draft reply (not sent automatically): {mention.suggested_reply}")
        if mention.url:
            lines.append(f"  Open root/source: {mention.url}")
    if count > 10:
        lines.append(f"…and {count - 10} more")
    return "\n".join(lines)


def _live_test_text(
    mentions: list[Mention],
    *,
    fetched: int,
    qualified: int,
    sources: list[str],
    issues: list[str],
    run_at: datetime,
) -> str:
    lines = [
        "Primovezo Social Radar live delivery test",
        "",
        "TEST ONLY. These are real public-source fetch results. No social author was contacted.",
        f"Scanned at: {run_at.isoformat()}",
        f"Sources: {', '.join(sources) if sources else 'none'}",
        f"Fetched: {fetched}",
        f"Qualified at the configured production threshold: {qualified}",
        f"Reportable sample after exclusions/context normalization: {len(mentions)}",
    ]
    if issues:
        lines.extend(["", "Operational issues:"])
        lines.extend(f"- {issue}" for issue in issues[:20])

    if not mentions:
        lines.extend(
            [
                "",
                "Live posts were fetched, but none remained in the reportable sample after "
                "author exclusions/context normalization.",
                "The source-to-Resend delivery path still completed successfully.",
            ]
        )
        return "\n".join(lines)

    lines.extend(["", f"Live sample ({min(len(mentions), 10)}):"])
    for mention in mentions[:10]:
        excerpt = " ".join(mention.content.split())[:500]
        source = mention.source
        if mention.author:
            source += f" · {mention.author}"
        score = "unavailable" if mention.lead_score is None else f"{mention.lead_score}/100"
        relevant = (
            "unclassified"
            if mention.lead_relevant is None
            else ("yes" if mention.lead_relevant else "no")
        )
        lines.extend(
            [
                "",
                f"• {source} · query: {mention.query}",
                f"  AI relevant: {relevant} · score: {score}"
                + (f" · {mention.lead_category}" if mention.lead_category else ""),
            ]
        )
        if mention.lead_reason:
            lines.append(f"  Reason: {mention.lead_reason}")
        if mention.conversation:
            lines.append("  Conversation context:")
            lines.extend(_conversation_text_lines(mention))
        else:
            lines.append(f"  {excerpt}")
        if mention.url:
            lines.append(f"  Open root/source: {mention.url}")

    if len(mentions) > 10:
        lines.append(f"…and {len(mentions) - 10} more fetched sample item(s)")
    return "\n".join(lines)


def _conversation_text_lines(mention: Mention) -> list[str]:
    lines: list[str] = []
    for item in mention.conversation[:30]:
        depth = max(0, min(item.depth, 8))
        indent = "  " * depth
        marker = "ROOT" if depth == 0 else "↳"
        author = f"@{item.author}" if item.author else "unknown author"
        matched = " [keyword match]" if item.matched else ""
        excerpt = " ".join(item.text.split())[:420]
        lines.append(f"    {indent}{marker} {author}{matched}: {excerpt}")
        if item.url and (depth == 0 or item.matched):
            lines.append(f"    {indent}Open: {item.url}")
    if len(mention.conversation) > 30:
        lines.append(f"    …and {len(mention.conversation) - 30} more conversation post(s)")
    return lines


def _mention_payload(mention: Mention) -> dict:
    return {
        "id": mention.id,
        "source": mention.source,
        "author": mention.author,
        "title": mention.title,
        "text": mention.text[:500],
        "url": mention.url,
        "created_at": mention.created_at.isoformat(),
        "score": mention.score,
        "sentiment_score": mention.sentiment_score,
        "theme": mention.theme,
        "lead_relevant": mention.lead_relevant,
        "lead_score": mention.lead_score,
        "lead_category": mention.lead_category,
        "lead_reason": mention.lead_reason,
        "suggested_reply": mention.suggested_reply,
    }

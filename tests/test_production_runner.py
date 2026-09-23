"""Production runner deployment guardrails."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_daily_runner_shell_syntax():
    for relative in (
        "scripts/run-primovezo-daily.sh",
        "scripts/install-primovezo-daily-timer.sh",
        "scripts/install-harken-command.sh",
    ):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / relative)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_harken_command_installer_keeps_repo_context():
    text = (ROOT / "scripts/install-harken-command.sh").read_text()
    assert 'TARGET="$BIN_DIR/harken"' in text
    assert 'cd "$ROOT"' in text
    assert 'run harken "\\$@"' in text
    assert "Try: harken logs" in text


def test_daily_runner_is_locked_and_runs_primovezo_profile():
    text = (ROOT / "scripts/run-primovezo-daily.sh").read_text()
    assert "flock -n 9" in text
    assert 'run harken leads primovezo --limit "$LIMIT"' in text


def test_timer_installer_uses_daily_riga_schedule_and_user_service():
    text = (ROOT / "scripts/install-primovezo-daily-timer.sh").read_text()
    assert "systemctl --user enable --now primovezo-social-radar.timer" in text
    assert "OnCalendar=*-*-* 09:00:00 Europe/Riga" in text
    assert "RandomizedDelaySec=10m" in text
    assert "scripts/run-primovezo-daily.sh" in text


def test_production_docs_keep_email_internal_only():
    text = (ROOT / "docs/primovezo-production.md").read_text()
    assert "internal" in text.lower()
    assert "never contacts a prospect automatically" in text
    assert "HARKEN_RESEND_FROM=noreply@primovezo.com" in text
    assert "harken test-alert --transport resend --kind lead" in text

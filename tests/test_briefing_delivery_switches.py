from __future__ import annotations

from core.models import BriefingResult


def test_email_disabled_still_archives_without_smtp(monkeypatch):
    from core import briefing_archive
    from core import email as email_mod

    captured = {}

    def fake_archive(**kwargs):
        captured.update(kwargs)
        return "archive-id"

    def forbidden_send(*args, **kwargs):
        raise AssertionError("SMTP must not be called when stock briefing email is disabled")

    monkeypatch.setattr(email_mod, "BRIEFING_EMAIL_ENABLED", False, raising=False)
    monkeypatch.setattr(briefing_archive, "save_briefing_archive", fake_archive)
    monkeypatch.setattr(email_mod, "send_email", forbidden_send)

    result = BriefingResult(title="검수 대기 브리핑", raw_json={"advisor_oneliner": "핵심"})
    assert email_mod.send_briefing_email(result, "", "KR_OPEN") is False
    assert captured["briefing_type"] == "KR_OPEN"
    assert captured["channel"] == "hermes_review"
    assert captured["body_text"]


def test_raw_telegram_disabled_avoids_network_send(monkeypatch):
    from core import telegram as telegram_mod

    def forbidden_send(*args, **kwargs):
        raise AssertionError("raw Telegram must not be called before Hermes final review")

    monkeypatch.setattr(telegram_mod, "BRIEFING_RAW_TELEGRAM_ENABLED", False, raising=False)
    monkeypatch.setattr(telegram_mod, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(telegram_mod, "TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(telegram_mod, "_send_message", forbidden_send)

    result = BriefingResult(title="검수 대기 브리핑", raw_json={})
    assert telegram_mod.send_briefing_telegram(result, "", "KR_OPEN") is False

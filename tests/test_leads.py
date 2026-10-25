import inspect
import sqlite3

import pytest

import app.leads.service as leads
from app.leads.service import LeadError, save_lead


def _payload(**overrides):
    data = {
        "display_name": "Mina",
        "source": "instagram dm",
        "inquiry_date": "2026-10-25",
        "learning_goal": "Interview answers",
        "consent": True,
        "communication_preference": "kakao",
        "trial_lesson_status": "not_offered",
        "follow_up_on": "2026-11-01",
        "notes": "Asked for a trial.",
        "allow_duplicate": False,
    }
    data.update(overrides)
    return data


def test_follow_up_requires_consent_and_duplicate_is_blocked(db):
    saved = save_lead(db, _payload())
    db.commit()
    assert saved["consent"] is True
    assert saved["follow_up_on"] == "2026-11-01"
    assert saved["consent_recorded_at"]
    without = save_lead(db, _payload(source="friend", consent=False, follow_up_on="2026-11-02"))
    assert without["consent"] is False
    assert without["follow_up_on"] is None
    assert without["ignored_follow_up"] is True
    with pytest.raises(LeadError, match="Nothing was saved"):
        save_lead(db, _payload())
    allowed = save_lead(db, _payload(allow_duplicate=True))
    assert allowed["id"] != saved["id"]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO leads (
                source, inquiry_date, learning_goal, consent, communication_preference,
                trial_lesson_status, follow_up_on, created_at, updated_at
            ) VALUES ('x', '2026-10-25', 'goal', 0, 'none', 'not_offered', '2026-11-01', 't', 't')
            """
        )


def test_revoking_consent_clears_follow_up(db):
    saved = save_lead(db, _payload())
    updated = save_lead(db, _payload(consent=False, follow_up_on="2026-11-01"), saved["id"])
    assert updated["consent"] is False
    assert updated["follow_up_on"] is None
    assert updated["ignored_follow_up"] is True


def test_leads_module_has_no_outbound_messaging():
    source = inspect.getsource(leads)
    for token in ("smtplib", "send_message", "bulk", "scrape"):
        assert token not in source
    names = [name for name in dir(leads) if not name.startswith("_")]
    assert not any(name.startswith("send") or "scrape" in name for name in names)

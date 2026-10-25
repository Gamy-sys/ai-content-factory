import sqlite3
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.clock import utc_now_iso

PREFERENCES = ("email", "phone", "kakao", "in_person", "none")
TRIALS = ("not_offered", "offered", "scheduled", "completed", "declined")


class LeadError(ValueError):
    pass


class LeadInput(BaseModel):
    display_name: str = ""
    source: str = Field(min_length=1, max_length=120)
    inquiry_date: str
    learning_goal: str = Field(min_length=1, max_length=500)
    consent: bool
    communication_preference: str
    trial_lesson_status: str = "not_offered"
    follow_up_on: str | None = None
    notes: str = ""
    allow_duplicate: bool = False

    @field_validator("inquiry_date")
    @classmethod
    def date_ok(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%d")
        return value

    @field_validator("follow_up_on")
    @classmethod
    def follow_ok(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        datetime.strptime(value, "%Y-%m-%d")
        return value

    @field_validator("communication_preference")
    @classmethod
    def pref_ok(cls, value: str) -> str:
        if value not in PREFERENCES:
            raise ValueError("Choose a communication preference. Nothing is sent from this app.")
        return value

    @field_validator("trial_lesson_status")
    @classmethod
    def trial_ok(cls, value: str) -> str:
        if value not in TRIALS:
            raise ValueError("Unknown trial lesson status.")
        return value

    @field_validator("display_name", "notes")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()[:1000]


def _first(exc: ValidationError) -> str:
    error = exc.errors()[0]
    return str(error.get("msg") or "Check the lead form.")


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["consent"] = bool(data["consent"])
    return data


def get_lead(conn: sqlite3.Connection, lead_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return None if row is None else _row(row)


def list_leads(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM leads ORDER BY inquiry_date DESC, id DESC").fetchall()
    return [_row(row) for row in rows]


def save_lead(conn: sqlite3.Connection, payload: dict, lead_id: int | None = None) -> dict:
    try:
        parsed = LeadInput.model_validate(payload)
    except ValidationError as exc:
        raise LeadError(_first(exc)) from exc
    ignored_follow_up = bool(parsed.follow_up_on) and not parsed.consent
    follow_up = parsed.follow_up_on if parsed.consent else None
    if ignored_follow_up:
        follow_up = None
    duplicate = conn.execute(
        """
        SELECT id FROM leads
        WHERE source = ? AND inquiry_date = ? AND learning_goal = ? AND id != ?
        """,
        (parsed.source.strip(), parsed.inquiry_date, parsed.learning_goal.strip(), lead_id or -1),
    ).fetchone()
    if duplicate and not parsed.allow_duplicate:
        raise LeadError("A lead with the same source, date, and goal already exists. Nothing was saved.")
    now = utc_now_iso()
    consent_at = now if parsed.consent else None
    values = (
        parsed.display_name,
        parsed.source.strip(),
        parsed.inquiry_date,
        parsed.learning_goal.strip(),
        1 if parsed.consent else 0,
        consent_at,
        parsed.communication_preference,
        parsed.trial_lesson_status,
        follow_up,
        parsed.notes,
        now,
    )
    if lead_id is None:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                display_name, source, inquiry_date, learning_goal, consent, consent_recorded_at,
                communication_preference, trial_lesson_status, follow_up_on, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, now),
        )
        saved_id = int(cursor.lastrowid)
    else:
        current = get_lead(conn, lead_id)
        if current is None:
            raise LeadError("Lead not found.")
        if parsed.consent and current["consent_recorded_at"]:
            values = (*values[:5], current["consent_recorded_at"], *values[6:])
        conn.execute(
            """
            UPDATE leads SET
                display_name=?, source=?, inquiry_date=?, learning_goal=?, consent=?,
                consent_recorded_at=?, communication_preference=?, trial_lesson_status=?,
                follow_up_on=?, notes=?, updated_at=?
            WHERE id=?
            """,
            (*values, lead_id),
        )
        saved_id = lead_id
    saved = get_lead(conn, saved_id)
    if saved is None:
        raise LeadError("Lead not found after save.")
    saved["ignored_follow_up"] = ignored_follow_up
    return saved

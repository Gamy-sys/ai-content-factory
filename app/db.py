import sqlite3
from pathlib import Path

from app.clock import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_key TEXT NOT NULL,
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    hook TEXT NOT NULL,
    english_examples_json TEXT NOT NULL,
    korean_explanation TEXT NOT NULL,
    engagement_question TEXT NOT NULL,
    cta TEXT,
    hashtags_json TEXT NOT NULL,
    carousel_outline_json TEXT,
    content_status TEXT NOT NULL DEFAULT 'draft' CHECK (content_status IN ('draft', 'approved')),
    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0, 1)),
    review_reason TEXT,
    human_reviewed INTEGER NOT NULL DEFAULT 0 CHECK (human_reviewed IN (0, 1)),
    uncertain_linguistic_claim INTEGER NOT NULL DEFAULT 0 CHECK (uncertain_linguistic_claim IN (0, 1)),
    generation_attempts INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT,
    calendar_slot_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_history (
    topic_key TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    draft_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    category TEXT NOT NULL,
    platform TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count BETWEEN 1 AND 10),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE (weekday, category, platform)
);

CREATE TABLE IF NOT EXISTS publish_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'draft', 'approved', 'scheduled', 'publishing', 'published', 'failed', 'manual-action-required'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    scheduled_for TEXT,
    external_id TEXT,
    external_url TEXT,
    last_error TEXT,
    confirmed_success INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_success IN (0, 1)),
    retry_blocked INTEGER NOT NULL DEFAULT 0 CHECK (retry_blocked IN (0, 1)),
    prepared_text TEXT NOT NULL,
    hashtags_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (draft_id, platform),
    CHECK (status != 'published' OR (
        confirmed_success = 1 AND external_id IS NOT NULL AND external_id != ''
    ))
);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publish_item_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    detail TEXT,
    http_attempted INTEGER NOT NULL DEFAULT 0 CHECK (http_attempted IN (0, 1)),
    FOREIGN KEY (publish_item_id) REFERENCES publish_items(id)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    topic TEXT NOT NULL,
    relevance TEXT NOT NULL CHECK (relevance IN ('low', 'medium', 'high')),
    suggested_reply TEXT NOT NULL,
    reply_language TEXT NOT NULL CHECK (reply_language IN ('ko', 'en')),
    uncertain_rules INTEGER NOT NULL DEFAULT 1 CHECK (uncertain_rules IN (0, 1)),
    uncertain_reason TEXT NOT NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    imported_via TEXT NOT NULL CHECK (imported_via IN ('rss', 'manual')),
    excerpt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    inquiry_date TEXT NOT NULL,
    learning_goal TEXT NOT NULL,
    consent INTEGER NOT NULL CHECK (consent IN (0, 1)),
    consent_recorded_at TEXT,
    communication_preference TEXT NOT NULL CHECK (
        communication_preference IN ('email', 'phone', 'kakao', 'in_person', 'none')
    ),
    trial_lesson_status TEXT NOT NULL CHECK (
        trial_lesson_status IN ('not_offered', 'offered', 'scheduled', 'completed', 'declined')
    ),
    follow_up_on TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (follow_up_on IS NULL OR consent = 1)
);

CREATE TABLE IF NOT EXISTS engagement_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    source TEXT NOT NULL CHECK (source = 'official_api'),
    fetched_at TEXT NOT NULL,
    raw_payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_filter ON drafts(category, content_status);
CREATE INDEX IF NOT EXISTS idx_publish_status ON publish_items(status);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
"""

DEFAULT_CALENDAR = [
    (0, "korean_learner_mistakes", "instagram", 1, "Common mistakes Korean adults make"),
    (1, "conversational_expressions", "instagram", 1, "Natural expressions"),
    (2, "business_workplace", "facebook", 1, "Workplace English"),
    (3, "interview_prep", "naver_blog", 1, "Interview answers"),
    (4, "grammar_quizzes", "naver_cafe", 1, "Short quiz"),
    (5, "pronunciation_confidence", "instagram", 1, "Speaking confidence"),
    (6, "educational_posts", "facebook", 1, "Carousel lesson"),
]


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    set_setting_default(conn, "job_weekly_calendar", "0")
    set_setting_default(conn, "job_research_rss", "0")
    set_setting_default(conn, "job_weekly_calendar_last_run", "")
    set_setting_default(conn, "job_weekly_calendar_last_status", "never run")
    set_setting_default(conn, "job_research_rss_last_run", "")
    set_setting_default(conn, "job_research_rss_last_status", "never run")
    if get_setting(conn, "calendar_seeded") is None:
        for weekday, category, platform, count, notes in DEFAULT_CALENDAR:
            conn.execute(
                """
                INSERT INTO calendar_slots (weekday, category, platform, count, enabled, notes)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (weekday, category, platform, count, notes),
            )
        set_setting(conn, "calendar_seeded", utc_now_iso())
    conn.commit()


def set_setting_default(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (key, value),
    )

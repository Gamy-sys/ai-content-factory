import json
import sqlite3
import uuid

from app.clock import utc_hours_ago_iso, utc_now_iso
from app.config import Settings
from app.content.service import get_draft
from app.publishing.adapters import get_adapter
from app.transport import TransportError

TRANSITIONS = {
    "draft": {"approved"},
    "approved": {"scheduled", "publishing", "manual-action-required"},
    "scheduled": {"publishing", "manual-action-required", "approved"},
    "publishing": {"published", "failed", "manual-action-required"},
    "published": set(),
    "failed": {"publishing", "manual-action-required"},
    "manual-action-required": {"approved"},
}


class PublishRefused(RuntimeError):
    pass


class InvalidTransition(PublishRefused):
    pass


def assert_can_transition(current: str, new: str) -> None:
    if new == "published":
        raise PublishRefused(
            "Refusing to mark published without a confirmed platform success response."
        )
    allowed = TRANSITIONS.get(current)
    if allowed is None or new not in allowed:
        raise InvalidTransition(f"Cannot move from {current} to {new}.")


def redact(text: str, settings: Settings) -> str:
    output = text or ""
    for secret in (
        settings.facebook_page_token,
        settings.instagram_access_token,
        settings.naver_cafe_access_token,
    ):
        if secret and len(secret) >= 4:
            output = output.replace(secret, "[redacted]")
    return output[:500]


def render_post(draft: dict) -> str:
    examples = "\n".join(f"- {line}" for line in draft["english_examples"])
    lines = [draft["hook"], "", examples, "", draft["korean_explanation"], "", draft["engagement_question"]]
    if draft.get("cta"):
        lines.extend(["", draft["cta"]])
    if draft.get("carousel_outline"):
        lines.append("")
        lines.append("Carousel:")
        lines.extend(f"{index}. {slide}" for index, slide in enumerate(draft["carousel_outline"], start=1))
    lines.extend(["", " ".join(draft["hashtags"])])
    return "\n".join(lines).strip()


def _item_from_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["hashtags"] = json.loads(data.pop("hashtags_json"))
    data["confirmed_success"] = bool(data["confirmed_success"])
    data["retry_blocked"] = bool(data["retry_blocked"])
    return data


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT p.*, d.hook, d.topic_key, d.category, d.content_status
        FROM publish_items p
        JOIN drafts d ON d.id = p.draft_id
        WHERE p.id = ?
        """,
        (item_id,),
    ).fetchone()
    return None if row is None else _item_from_row(row)


def list_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.*, d.hook, d.topic_key, d.category, d.content_status
        FROM publish_items p
        JOIN drafts d ON d.id = p.draft_id
        ORDER BY p.id DESC
        """
    ).fetchall()
    return [_item_from_row(row) for row in rows]


def queue_draft(conn: sqlite3.Connection, draft_id: int) -> dict:
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise PublishRefused("Draft not found. Nothing was queued.")
    if draft["content_status"] != "approved":
        raise PublishRefused("Approve the draft first. Nothing was queued.")
    existing = conn.execute(
        "SELECT id FROM publish_items WHERE draft_id = ? AND platform = ?",
        (draft_id, draft["platform"]),
    ).fetchone()
    if existing:
        item = get_item(conn, int(existing["id"]))
        if item is None:
            raise PublishRefused("The existing queue item could not be read.")
        return item
    adapter = get_adapter(draft["platform"])
    status = "manual-action-required" if not adapter.automated else "approved"
    now = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO publish_items (
            draft_id, platform, status, idempotency_key, prepared_text, hashtags_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            draft_id,
            draft["platform"],
            status,
            str(uuid.uuid4()),
            render_post(draft),
            json.dumps(draft["hashtags"], ensure_ascii=False),
            now,
            now,
        ),
    )
    item = get_item(conn, int(cursor.lastrowid))
    if item is None:
        raise PublishRefused("The queue item could not be read.")
    return item


def _recent_successes(conn: sqlite3.Connection, platform: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM publish_items
        WHERE platform = ? AND confirmed_success = 1 AND updated_at >= ?
        """,
        (platform, utc_hours_ago_iso(24)),
    ).fetchone()
    return int(row["n"])


def _refuse(item: dict | None, detail: str) -> dict:
    return {"item": item, "published": False, "ok": False, "detail": detail}


def _save_result(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    status: str,
    confirmed: bool,
    external_id: str | None,
    external_url: str | None,
    error: str,
    retry_blocked: bool,
    prepared_text: str | None = None,
) -> None:
    if status == "published" and (not confirmed or not external_id):
        raise PublishRefused("Refusing to mark published without a confirmed platform success response.")
    conn.execute(
        """
        UPDATE publish_items SET
            status=?, confirmed_success=?, external_id=?, external_url=?, last_error=?,
            retry_blocked=?, updated_at=?,
            prepared_text=COALESCE(?, prepared_text)
        WHERE id=?
        """,
        (
            status,
            1 if confirmed else 0,
            external_id,
            external_url,
            error,
            1 if retry_blocked else 0,
            utc_now_iso(),
            prepared_text,
            item_id,
        ),
    )
    conn.commit()


def attempt_publish(conn: sqlite3.Connection, item_id: int, settings: Settings, transport) -> dict:
    item = get_item(conn, item_id)
    if item is None:
        return _refuse(None, "Publish item not found. Nothing was published.")
    if item["confirmed_success"] or item["status"] == "published":
        return {
            "item": item,
            "published": True,
            "ok": True,
            "detail": "Already published. The API was not called again.",
        }
    if item["status"] == "publishing":
        return _refuse(
            item,
            "A publish request is already in flight or was interrupted. Nothing new was sent.",
        )
    if item["retry_blocked"]:
        return _refuse(
            item,
            "The last attempt had no confirmed result. Acknowledge that it is not live before retrying. Nothing was sent.",
        )
    if item["status"] not in {"approved", "scheduled", "failed"}:
        return _refuse(item, "This item cannot be published from its current state. Nothing was published.")

    adapter = get_adapter(item["platform"])
    if not adapter.automated:
        return _refuse(item, adapter.summary + " Nothing was published.")
    if not adapter.credentials_ready(settings):
        return _refuse(item, "API credentials are not configured. Nothing was published.")
    if item["platform"] == "instagram" and not adapter.image_ready(settings):
        return _refuse(
            item,
            "Instagram's official API needs a public https image URL. No text-only post was sent.",
        )
    if item["platform"] == "instagram" and _recent_successes(conn, "instagram") >= settings.instagram_daily_cap:
        return _refuse(
            item,
            "The local Instagram cap for this 24 hour window is already reached. Nothing was published.",
        )
    if item["platform"] == "naver_cafe" and _recent_successes(conn, "naver_cafe") >= settings.naver_cafe_daily_cap:
        return _refuse(
            item,
            "Naver's Cafe write limit is 200 posts a day, and this local cap is already reached. Nothing was published.",
        )

    draft = get_draft(conn, item["draft_id"])
    if draft is None:
        return _refuse(item, "The draft is missing. Nothing was published.")
    prepared = render_post(draft)
    if not prepared.strip():
        return _refuse(item, "The post text is empty. Nothing was published.")
    if item["platform"] == "instagram" and len(prepared) > 2200:
        return _refuse(item, "The caption is longer than Instagram's 2200 character limit. Nothing was published.")

    changed = conn.execute(
        """
        UPDATE publish_items
        SET status='publishing', attempt_count=attempt_count+1, prepared_text=?, updated_at=?
        WHERE id=? AND status=? AND confirmed_success=0 AND retry_blocked=0
        """,
        (prepared, utc_now_iso(), item_id, item["status"]),
    )
    if changed.rowcount != 1:
        conn.rollback()
        return _refuse(get_item(conn, item_id), "The item changed before publishing. Nothing new was sent.")
    conn.commit()
    attempt = conn.execute(
        """
        INSERT INTO publish_attempts (publish_item_id, idempotency_key, started_at, http_attempted)
        VALUES (?, ?, ?, 0)
        """,
        (item_id, item["idempotency_key"], utc_now_iso()),
    )
    attempt_id = int(attempt.lastrowid)
    conn.commit()

    subject = draft["hook"]
    try:
        if item["platform"] == "naver_cafe":
            outcome = adapter.publish(prepared, settings, transport, subject=subject)
        else:
            outcome = adapter.publish(prepared, settings, transport)
    except TransportError as exc:
        conn.execute("UPDATE publish_attempts SET http_attempted=1 WHERE id=?", (attempt_id,))
        detail = redact(str(exc), settings)
        _save_result(
            conn,
            item_id,
            status="failed",
            confirmed=False,
            external_id=None,
            external_url=None,
            error=detail,
            retry_blocked=exc.uncertain,
        )
        _finish_attempt(conn, attempt_id, "failed", detail)
        return _refuse(get_item(conn, item_id), detail)

    conn.execute(
        "UPDATE publish_attempts SET http_attempted=? WHERE id=?",
        (1 if outcome.http_attempted else 0, attempt_id),
    )
    detail = redact(f"{outcome.detail}\n{outcome.response_text}".strip(), settings)
    if outcome.state == "published" and outcome.confirmed and outcome.external_id:
        _save_result(
            conn,
            item_id,
            status="published",
            confirmed=True,
            external_id=outcome.external_id,
            external_url=outcome.external_url,
            error="",
            retry_blocked=False,
            prepared_text=prepared,
        )
        _finish_attempt(conn, attempt_id, "published", detail)
        return {
            "item": get_item(conn, item_id),
            "published": True,
            "ok": True,
            "detail": detail or "The platform confirmed the post.",
        }

    status = "manual-action-required" if outcome.state == "manual-action-required" else "failed"
    _save_result(
        conn,
        item_id,
        status=status,
        confirmed=False,
        external_id=None,
        external_url=None,
        error=detail,
        retry_blocked=bool(outcome.uncertain),
    )
    _finish_attempt(conn, attempt_id, status, detail)
    final = get_item(conn, item_id)
    message = detail or "The platform did not confirm the post. Nothing was marked published."
    if "Nothing was" not in message and "nothing was" not in message:
        message = message + " Nothing was marked published."
    return _refuse(final, message)


def _finish_attempt(conn: sqlite3.Connection, attempt_id: int, outcome: str, detail: str) -> None:
    conn.execute(
        "UPDATE publish_attempts SET finished_at=?, outcome=?, detail=? WHERE id=?",
        (utc_now_iso(), outcome, detail[:500], attempt_id),
    )
    conn.commit()


def move_item(conn: sqlite3.Connection, item_id: int, new_status: str, *, scheduled_for: str | None = None) -> dict:
    item = get_item(conn, item_id)
    if item is None:
        raise PublishRefused("Publish item not found.")
    assert_can_transition(item["status"], new_status)
    conn.execute(
        """
        UPDATE publish_items
        SET status=?, scheduled_for=?, updated_at=?
        WHERE id=?
        """,
        (new_status, scheduled_for, utc_now_iso(), item_id),
    )
    updated = get_item(conn, item_id)
    if updated is None:
        raise PublishRefused("Publish item not found.")
    return updated


def acknowledge_uncertain(conn: sqlite3.Connection, item_id: int) -> dict:
    item = get_item(conn, item_id)
    if item is None:
        raise PublishRefused("Publish item not found.")
    if item["confirmed_success"] or item["status"] == "published":
        raise PublishRefused("This item is already confirmed published. It was not changed.")
    if item["status"] not in {"publishing", "failed"}:
        raise PublishRefused("There is no uncertain attempt to acknowledge. Nothing was published.")
    if item["status"] == "failed" and not item["retry_blocked"]:
        raise PublishRefused("Retry is already allowed. Nothing was published.")
    conn.execute(
        """
        UPDATE publish_items
        SET status='failed', retry_blocked=0,
            last_error='User confirmed the previous attempt is not live. One retry is allowed.',
            updated_at=?
        WHERE id=?
        """,
        (utc_now_iso(), item_id),
    )
    updated = get_item(conn, item_id)
    if updated is None:
        raise PublishRefused("Publish item not found.")
    return updated


def export_text(conn: sqlite3.Connection, item_id: int) -> str:
    item = get_item(conn, item_id)
    if item is None:
        raise PublishRefused("Publish item not found.")
    draft = get_draft(conn, item["draft_id"])
    body = render_post(draft) if draft else item["prepared_text"]
    return (
        f"Platform: {item['platform']}\n"
        f"Status: {item['status']}\n"
        f"Idempotency key: {item['idempotency_key']}\n\n"
        f"{body}\n"
    )

import json
import sqlite3

from app.clock import utc_now_iso
from app.config import Settings
from app.content.schema import CATEGORIES
from app.transport import TransportError


NOT_AVAILABLE = "not available"


def counts(conn: sqlite3.Connection) -> dict:
    generated = conn.execute("SELECT COUNT(*) AS n FROM drafts").fetchone()["n"]
    approved = conn.execute(
        "SELECT COUNT(*) AS n FROM drafts WHERE content_status = 'approved'"
    ).fetchone()["n"]
    published = conn.execute(
        "SELECT COUNT(*) AS n FROM publish_items WHERE status = 'published' AND confirmed_success = 1"
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM publish_items WHERE status = 'failed'"
    ).fetchone()["n"]
    inquiries = [
        {"source": row["source"], "count": row["n"]}
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM leads GROUP BY source ORDER BY n DESC, source"
        )
    ]
    return {
        "generated": int(generated),
        "approved": int(approved),
        "published": int(published),
        "failed": int(failed),
        "inquiries_by_source": inquiries,
    }


def suggest_topics(conn: sqlite3.Connection) -> list[str]:
    totals = {key: 0 for key, _label in CATEGORIES}
    flagged = {key: 0 for key, _label in CATEGORIES}
    published = {key: 0 for key, _label in CATEGORIES}
    for row in conn.execute(
        "SELECT category, COUNT(*) AS n, SUM(review_required) AS flags FROM drafts GROUP BY category"
    ):
        totals[row["category"]] = int(row["n"])
        flagged[row["category"]] = int(row["flags"] or 0)
    for row in conn.execute(
        """
        SELECT d.category AS category, COUNT(*) AS n
        FROM publish_items p
        JOIN drafts d ON d.id = p.draft_id
        WHERE p.confirmed_success = 1
        GROUP BY d.category
        """
    ):
        published[row["category"]] = int(row["n"])
    suggestions = []
    for key, label in CATEGORIES:
        if totals[key] == 0:
            suggestions.append(f"No drafts yet for {label}.")
        elif totals[key] >= 2 and flagged[key] / totals[key] >= 0.5:
            suggestions.append(f"{label} often needs a language review before approval.")
        elif published[key] == 0 and totals[key] >= 2:
            suggestions.append(
                f"{label} has drafts but nothing confirmed published. Review the queue before generating more."
            )
        elif published[key] > 0:
            suggestions.append(
                f"{label} has {published[key]} confirmed published post(s). A follow-up in this category matches that outcome."
            )
    return suggestions


def list_engagement(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT platform, external_id, metric_name, metric_value, source, fetched_at
        FROM engagement_snapshots
        ORDER BY id DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def engagement_view(conn: sqlite3.Connection) -> dict:
    metrics = list_engagement(conn)
    if not metrics:
        return {"status": NOT_AVAILABLE, "metrics": [], "detail": "No official API response has been stored."}
    return {"status": "official_api", "metrics": metrics, "detail": "Figures below came from a stored official API response."}


def record_official_metric(
    conn: sqlite3.Connection,
    *,
    platform: str,
    external_id: str,
    metric_name: str,
    metric_value: float,
    raw_payload: str,
) -> None:
    conn.execute(
        """
        INSERT INTO engagement_snapshots (
            platform, external_id, metric_name, metric_value, source, fetched_at, raw_payload
        ) VALUES (?, ?, ?, ?, 'official_api', ?, ?)
        """,
        (platform, external_id, metric_name, metric_value, utc_now_iso(), raw_payload[:4000]),
    )


def parse_facebook_engagement(payload: dict) -> list[tuple[str, float]]:
    metrics: list[tuple[str, float]] = []
    reactions = payload.get("reactions")
    if isinstance(reactions, dict):
        summary = reactions.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("total_count"), (int, float)) and not isinstance(summary.get("total_count"), bool):
            metrics.append(("reactions", float(summary["total_count"])))
    comments = payload.get("comments")
    if isinstance(comments, dict):
        summary = comments.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("total_count"), (int, float)) and not isinstance(summary.get("total_count"), bool):
            metrics.append(("comments", float(summary["total_count"])))
    return metrics


def parse_instagram_engagement(payload: dict) -> list[tuple[str, float]]:
    metrics: list[tuple[str, float]] = []
    for key in ("like_count", "comments_count"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics.append((key, float(value)))
    return metrics


def refresh_engagement(conn: sqlite3.Connection, settings: Settings, transport) -> dict:
    facebook_ready = bool(settings.facebook_page_id and settings.facebook_page_token)
    instagram_ready = bool(settings.instagram_user_id and settings.instagram_access_token)
    if not facebook_ready and not instagram_ready:
        return {
            "status": NOT_AVAILABLE,
            "stored": 0,
            "detail": "No official API credentials are configured. No numbers were fetched or invented.",
        }
    rows = conn.execute(
        """
        SELECT platform, external_id FROM publish_items
        WHERE confirmed_success = 1 AND platform IN ('facebook', 'instagram')
        ORDER BY id DESC LIMIT 10
        """
    ).fetchall()
    if not rows:
        return {
            "status": NOT_AVAILABLE,
            "stored": 0,
            "detail": "No confirmed published Facebook or Instagram posts to query. Naver metrics are not available.",
        }
    stored = 0
    version = settings.meta_graph_version if settings.meta_graph_version.startswith("v") else "v26.0"
    for row in rows:
        platform = row["platform"]
        external_id = row["external_id"]
        if platform == "facebook" and facebook_ready:
            url = f"https://graph.facebook.com/{version}/{external_id}"
            params_data = {
                "fields": "reactions.summary(true),comments.summary(true)",
                "access_token": settings.facebook_page_token,
            }
            parser = parse_facebook_engagement
        elif platform == "instagram" and instagram_ready:
            url = f"https://graph.facebook.com/{version}/{external_id}"
            params_data = {
                "fields": "like_count,comments_count",
                "access_token": settings.instagram_access_token,
            }
            parser = parse_instagram_engagement
        else:
            continue
        try:
            result = transport.request("GET", url, params=params_data, timeout=20)
        except TransportError:
            continue
        if result.status_code != 200:
            continue
        try:
            payload = result.json()
        except (json.JSONDecodeError, ValueError):
            continue
        metrics = parser(payload)
        if not metrics:
            continue
        raw = result.text[:4000]
        for name, value in metrics:
            record_official_metric(
                conn,
                platform=platform,
                external_id=external_id,
                metric_name=name,
                metric_value=value,
                raw_payload=raw,
            )
            stored += 1
    if stored == 0:
        return {
            "status": NOT_AVAILABLE,
            "stored": 0,
            "detail": "The API response did not include engagement fields. Nothing was invented.",
        }
    return {
        "status": "official_api",
        "stored": stored,
        "detail": f"Stored {stored} metric(s) from an official API response.",
    }

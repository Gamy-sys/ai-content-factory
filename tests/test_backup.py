from app.backup import backup_database, create_backup, resolve_backup_name, restore_database, restore_into
from app.db import connect, init_db
from app.leads.service import save_lead


def _lead(conn, source):
    save_lead(
        conn,
        {
            "source": source,
            "inquiry_date": "2026-10-25",
            "learning_goal": "Speaking confidence",
            "consent": True,
            "communication_preference": "email",
            "trial_lesson_status": "offered",
            "follow_up_on": None,
        },
    )


def test_sqlite_backup_round_trip(tmp_path):
    live = tmp_path / "app.db"
    conn = connect(str(live))
    init_db(conn)
    _lead(conn, "friend")
    conn.commit()
    dest = tmp_path / "copy.db"
    backup_database(str(live), dest)
    check = connect(str(dest)).execute("PRAGMA integrity_check").fetchone()[0]
    assert check == "ok"
    _lead(conn, "website")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"] == 2
    restore_database(str(dest), str(live))
    conn.close()
    restored = connect(str(live))
    sources = [row["source"] for row in restored.execute("SELECT source FROM leads")]
    assert sources == ["friend"]
    restored.close()


def test_restore_into_open_connection_and_name_guard(tmp_path, db, settings):
    _lead(db, "friend")
    db.commit()
    path = create_backup(settings.database_path, settings.backup_dir)
    _lead(db, "website")
    db.commit()
    restore_into(db, str(path))
    db.commit()
    sources = [row["source"] for row in db.execute("SELECT source FROM leads ORDER BY id")]
    assert sources == ["friend"]
    import pytest
    from app.backup import BackupError

    with pytest.raises(BackupError):
        resolve_backup_name(settings.backup_dir, "../app.db")
    with pytest.raises(BackupError):
        resolve_backup_name(settings.backup_dir, "missing.db")

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from app.db import connect, init_db


class BackupError(RuntimeError):
    pass


def backup_database(src: str, dest: Path) -> None:
    if not Path(src).is_file():
        raise BackupError("Database not found. Nothing was backed up.")
    source = sqlite3.connect(src)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
        check = target.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise BackupError("Backup failed integrity check.")
    finally:
        target.close()
        source.close()


def create_backup(database_path: str, backup_dir: str) -> Path:
    folder = Path(backup_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = folder / f"studio-{stamp}.db"
    suffix = 1
    while dest.exists():
        dest = folder / f"studio-{stamp}-{suffix}.db"
        suffix += 1
    backup_database(database_path, dest)
    return dest


def resolve_backup_name(backup_dir: str, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise BackupError("Invalid backup name. Nothing was restored.")
    if not name.endswith(".db"):
        raise BackupError("Backup files must end in .db. Nothing was restored.")
    root = Path(backup_dir).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        raise BackupError("Backup file was not found. Nothing was restored.")
    return path


def _checked_source(backup_path: str) -> sqlite3.Connection:
    if not Path(backup_path).is_file():
        raise BackupError("Backup file was not found. Nothing was restored.")
    source = sqlite3.connect(backup_path)
    check = source.execute("PRAGMA integrity_check").fetchone()
    if check is None or check[0] != "ok":
        source.close()
        raise BackupError("Backup failed integrity check. The live database was not changed.")
    return source


def restore_into(conn: sqlite3.Connection, backup_path: str) -> None:
    source = _checked_source(backup_path)
    try:
        source.backup(conn)
    finally:
        source.close()


def restore_database(backup_path: str, dest_path: str) -> None:
    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    source = _checked_source(backup_path)
    try:
        target = sqlite3.connect(dest_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def list_backups(backup_dir: str) -> list[str]:
    folder = Path(backup_dir)
    if not folder.is_dir():
        return []
    return sorted((path.name for path in folder.glob("*.db") if path.is_file()), reverse=True)


def main(argv: list[str]) -> int:
    settings = get_settings()
    if len(argv) < 2 or argv[1] not in {"backup", "restore"}:
        print("Usage: python -m app.backup backup|restore [file]", file=sys.stderr)
        return 2
    conn = connect(settings.database_path)
    try:
        init_db(conn)
    finally:
        conn.close()
    if argv[1] == "backup":
        print(create_backup(settings.database_path, settings.backup_dir))
        return 0
    if len(argv) < 3:
        print("Usage: python -m app.backup restore FILE", file=sys.stderr)
        return 2
    restore_database(argv[2], settings.database_path)
    print("restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

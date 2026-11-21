"""Import the data files from a downloaded official V2G Liberty app backup."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

OFFICIAL_APP = "9a1c9f7e_v2g-liberty"
SUPPORTED_VERSION = "0.9.0"
MAX_ARCHIVE = 512 * 1024 * 1024
SETTINGS = "v2g_liberty_settings.json"
DATABASE = "v2g_liberty_data.db"
FILES = {SETTINGS: 1024 * 1024, DATABASE: MAX_ARCHIVE, DATABASE + "-wal": MAX_ARCHIVE}


class ImportError(Exception):
    """An import cannot be completed without user action."""


def _members(archive, names, *, scan_limit):
    """Only accept exact, regular-file members and reject duplicates."""
    found = {}
    for index, member in enumerate(archive):
        name = member.name.removeprefix("./")
        if index >= 10_000 or member.offset_data + member.size > scan_limit:
            raise ImportError("The backup contains too much data.")
        if name in names:
            if name in found or not member.isreg() or getattr(member, "sparse", None):
                raise ImportError("The backup contains an invalid or duplicate file.")
            found[name] = member
    return found


def _copy(archive, member, dest, limit):
    if not 0 < member.size <= limit:
        raise ImportError("A required backup file is empty or too large.")
    with archive.extractfile(member) as source, dest.open("xb") as output:
        os.chmod(dest, 0o600)
        shutil.copyfileobj(source, output)


def _read_app_backup(backup, staging):
    names = {OFFICIAL_APP + ".tar", OFFICIAL_APP + ".tar.gz"}
    with tarfile.open(backup, "r:") as outer:
        members = _members(outer, names | {"backup.json"}, scan_limit=MAX_ARCHIVE)
        metadata = members.get("backup.json")
        if metadata is None or not 0 < metadata.size <= 1024 * 1024:
            raise ImportError("This file is missing Home Assistant backup metadata.")
        with outer.extractfile(metadata) as raw:
            description = json.load(raw)
        if not isinstance(description, dict) or description.get("type") != "partial":
            raise ImportError("Choose a partial backup containing only official V2G Liberty.")
        apps = description.get("addons")
        if (
            not isinstance(apps, list) or len(apps) != 1
            or not isinstance(apps[0], dict) or apps[0].get("slug") != OFFICIAL_APP
            or description.get("folders") or description.get("homeassistant")
            or any(name not in names | {"backup.json", "supervisor.tar", "supervisor.tar.gz"}
                   for name in (member.name.removeprefix("./") for member in outer.getmembers()))
        ):
            raise ImportError("Choose a partial backup containing only official V2G Liberty.")
        if apps[0].get("version") != SUPPORTED_VERSION:
            raise ImportError("This fork imports settings and history from V2G Liberty 0.9.0.")
        app_members = names.intersection(members)
        if len(app_members) != 1:
            raise ImportError("The backup does not contain one official V2G Liberty app archive.")
        packed = staging / "app-backup"
        _copy(outer, members[app_members.pop()], packed, MAX_ARCHIVE)

    with tarfile.open(packed, "r:*") as inner:
        members = _members(inner, {"data/" + name for name in FILES}, scan_limit=MAX_ARCHIVE * 2)
        for name, limit in FILES.items():
            member = members.get("data/" + name)
            if member is None:
                if name == DATABASE + "-wal":
                    continue
                raise ImportError("The backup must contain both settings and V2G Liberty history.")
            if name == DATABASE + "-wal" and not member.size:
                continue
            _copy(inner, member, staging / name, limit)


def _checked_database(source, destination):
    # Resolve committed WAL records into a standalone database. Do not copy
    # the shared-memory index from the backup; SQLite can rebuild it locally.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ImportError("The V2G Liberty history database failed its integrity check.")
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"schema_version", "interval_log"} <= tables:
            raise ImportError("The backup does not contain a V2G Liberty history database.")
        with closing(sqlite3.connect(destination)) as clean:
            os.chmod(destination, 0o600)
            db.backup(clean)
            clean.execute("PRAGMA journal_mode=DELETE")


def import_backup(backup: Path, data: Path):
    """Validate and install an official backup into an unused fork."""
    targets = [data / name for name in (DATABASE, SETTINGS)]
    if any(os.path.lexists(target) for target in [*targets, data / (DATABASE + "-wal"), data / (DATABASE + "-shm")]):
        raise ImportError("Import is available only before the fork has settings or history.")
    if not 0 < backup.stat().st_size <= MAX_ARCHIVE:
        raise ImportError("Choose a backup file smaller than 512 MiB.")

    try:
        with tempfile.TemporaryDirectory(dir=data, prefix=".backup-import-") as tmp:
            staging = Path(tmp)
            _read_app_backup(backup, staging)
            settings = staging / SETTINGS
            contents = json.loads(settings.read_bytes())
            if not isinstance(contents, dict) or not contents:
                raise ImportError("The V2G Liberty settings file is empty or invalid.")
            checked = staging / "checked.db"
            _checked_database(staging / DATABASE, checked)

            installed = []
            try:
                for source, target in zip((checked, settings), targets):
                    target.touch(mode=0o600, exist_ok=False)
                    installed.append(target)
                    os.replace(source, target)
            except OSError:
                for target in installed:
                    target.unlink()
                raise
    except (tarfile.TarError, EOFError, ValueError, sqlite3.Error) as error:
        raise ImportError("The backup is invalid or encrypted. Download the decrypted copy from Home Assistant Backups.") from error

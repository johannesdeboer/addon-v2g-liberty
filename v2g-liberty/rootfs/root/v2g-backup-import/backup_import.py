"""Import the data files from an official V2G Liberty app backup."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tarfile
import tempfile

from securetar import InvalidPasswordError, SecureTarError, SecureTarFile

OFFICIAL_APP = "9a1c9f7e_v2g-liberty"
SUPPORTED_VERSION = "0.9.0"
MAX_ARCHIVE = 512 * 1024 * 1024
SETTINGS = "v2g_liberty_settings.json"
DATABASE = "v2g_liberty_data.db"
FILES = {SETTINGS: 1024 * 1024, DATABASE: MAX_ARCHIVE, DATABASE + "-wal": MAX_ARCHIVE}


class ImportError(Exception):
    """An import cannot be completed without user action."""


APP_ARCHIVES = {OFFICIAL_APP + ".tar", OFFICIAL_APP + ".tar.gz"}


def _scan(archive, names, *, scan_limit):
    """Yield selected regular files once and bound archive expansion."""
    seen = set()
    for index, member in enumerate(archive):
        name = member.name.removeprefix("./")
        if index >= 10_000 or member.offset_data + member.size > scan_limit:
            raise ImportError("The backup contains too much data.")
        if name in names:
            if name in seen or not member.isreg() or getattr(member, "sparse", None):
                raise ImportError("The backup contains an invalid or duplicate file.")
            seen.add(name)
            yield name, member


def _copy(archive, member, dest, limit):
    if not 0 < member.size <= limit:
        raise ImportError("A required backup file is empty or too large.")
    with archive.extractfile(member) as source, dest.open("xb") as output:
        os.chmod(dest, 0o600)
        shutil.copyfileobj(source, output)


@contextmanager
def _open_backup(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ARCHIVE:
            raise ImportError("Choose a regular backup file up to 512 MiB.")
        with tarfile.open(fileobj=file, mode="r:") as archive:
            yield archive


def _describe(outer):
    members = dict(_scan(outer, APP_ARCHIVES | {"backup.json"}, scan_limit=MAX_ARCHIVE))
    metadata = members.get("backup.json")
    if metadata is None or not 0 < metadata.size <= 1024 * 1024:
        raise ImportError("This file is missing Home Assistant backup metadata.")
    with outer.extractfile(metadata) as raw:
        description = json.load(raw)
    if not isinstance(description, dict) or description.get("type") != "partial":
        raise ImportError("Choose a partial backup containing only official V2G Liberty.")
    apps = description.get("addons")
    allowed = APP_ARCHIVES | {"backup.json", "supervisor.tar", "supervisor.tar.gz"}
    if (
        not isinstance(apps, list) or len(apps) != 1
        or not isinstance(apps[0], dict) or apps[0].get("slug") != OFFICIAL_APP
        or description.get("folders") or description.get("homeassistant")
        or any(member.name.removeprefix("./") not in allowed for member in outer.getmembers())
    ):
        raise ImportError("Choose a partial backup containing only official V2G Liberty.")
    if apps[0].get("version") != SUPPORTED_VERSION:
        raise ImportError("This fork imports settings and history from V2G Liberty 0.9.0.")
    app_members = APP_ARCHIVES.intersection(members)
    if len(app_members) != 1:
        raise ImportError("The backup does not contain one official V2G Liberty app archive.")
    return description, members[app_members.pop()]


def list_local_backups(folder: Path):
    """Find compatible local backups using only their outer metadata."""
    backups = []
    for path in folder.glob("*.tar"):
        try:
            with _open_backup(path) as outer:
                description, _ = _describe(outer)
        except (ImportError, OSError, tarfile.TarError, EOFError, ValueError):
            continue
        name = description.get("name")
        date = description.get("date")
        backups.append({
            "file": path.name,
            "name": name if isinstance(name, str) else path.name,
            "date": date if isinstance(date, str) else "",
            "encrypted": description.get("protected") is True,
        })
    return sorted(backups, key=lambda item: (item["date"], item["file"]), reverse=True)


def local_backup_path(folder: Path, filename: str) -> Path:
    """Resolve a selected basename within the read-only backup directory."""
    if not filename.endswith(".tar") or any(c in filename for c in ("/", "\\", "\0")):
        raise ImportError("Select a backup from the local backup list.")
    return folder / filename


def _read_app_backup(backup, staging, password):
    with _open_backup(backup) as outer:
        description, member = _describe(outer)
        encrypted = description.get("protected") is True
        if encrypted and not password:
            raise ImportError("Enter the encryption key for this Home Assistant backup.")
        gzip = member.name.endswith(".gz")
        with outer.extractfile(member) as packed:
            if encrypted:
                reader = SecureTarFile(fileobj=packed, gzip=gzip, password=password)
            else:
                reader = tarfile.open(fileobj=packed, mode="r|gz" if gzip else "r|")
            with reader as inner:
                names = {"data/" + name for name in FILES}
                for path, entry in _scan(inner, names, scan_limit=MAX_ARCHIVE * 2):
                    name = path.removeprefix("data/")
                    if name == DATABASE + "-wal" and not entry.size:
                        continue
                    _copy(inner, entry, staging / name, FILES[name])
    if not all((staging / name).is_file() for name in (SETTINGS, DATABASE)):
        raise ImportError("The backup must contain both settings and V2G Liberty history.")


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


def import_backup(backup: Path, data: Path, *, password: str | None = None):
    """Validate and install an official backup into an unused fork."""
    targets = [data / name for name in (DATABASE, SETTINGS)]
    if any(os.path.lexists(target) for target in [*targets, data / (DATABASE + "-wal"), data / (DATABASE + "-shm")]):
        raise ImportError("Import is available only before the fork has settings or history.")

    try:
        with tempfile.TemporaryDirectory(dir=data, prefix=".backup-import-") as tmp:
            staging = Path(tmp)
            _read_app_backup(backup, staging, password)
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
    except InvalidPasswordError as error:
        raise ImportError("The backup encryption key is incorrect.") from error
    except (SecureTarError, tarfile.TarError, EOFError, ValueError, sqlite3.Error) as error:
        raise ImportError("Unable to read the backup. Check its encryption key and choose a valid Home Assistant backup.") from error

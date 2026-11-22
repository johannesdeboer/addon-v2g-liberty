"""Exercise migration with the Supervisor app-backup archive layout."""

from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tarfile
from tempfile import TemporaryDirectory
from unittest import TestCase

from securetar import SecureTarArchive

from backup_import import (
    DATABASE, ImportError, OFFICIAL_APP, SETTINGS, import_backup,
    list_local_backups, local_backup_path,
)


def tar_entry(archive, name, data):
    header = tarfile.TarInfo(name)
    header.size = len(data)
    archive.addfile(header, BytesIO(data))


class BackupImportTest(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        self.source = self.base / DATABASE
        self.backup = self.base / "backup.tar"
        self.local = self.base / "local"
        self.local.mkdir()
        self.db = sqlite3.connect(self.source)
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE schema_version (version);"
            "CREATE TABLE interval_log (energy);"
            "INSERT INTO interval_log VALUES (1);"
        )

    def make_backup(self, *, slug=OFFICIAL_APP, database=True, symlink=False, wal=False, version="0.9.0"):
        packed = BytesIO()
        with tarfile.open(fileobj=packed, mode="w:gz") as inner:
            if symlink:
                entry = tarfile.TarInfo("data/" + SETTINGS)
                entry.type = tarfile.SYMTYPE
                entry.linkname = "/data/settings"
                inner.addfile(entry)
            else:
                tar_entry(inner, "data/" + SETTINGS, b'{"input_text.charger_type":"example"}')
            if database:
                tar_entry(inner, "data/" + DATABASE, self.source.read_bytes())
            if wal:
                tar_entry(inner, "data/" + DATABASE + "-wal", Path(str(self.source) + "-wal").read_bytes())
            tar_entry(inner, "data/options.json", b'{"other": "do not import"}')
        with tarfile.open(self.backup, "w") as outer:
            tar_entry(outer, "./" + slug + ".tar.gz", packed.getvalue())
            tar_entry(outer, "./backup.json", json.dumps({
                "type": "partial", "addons": [{"slug": slug, "version": version}],
                "folders": [], "homeassistant": None,
            }).encode())

    def test_imports_settings_and_committed_wal_history(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("INSERT INTO interval_log VALUES (2)")
        self.db.commit()
        self.make_backup(wal=True)
        import_backup(self.backup, self.target)
        self.assertEqual({SETTINGS, DATABASE}, {path.name for path in self.target.iterdir()})
        with closing(sqlite3.connect(self.target / DATABASE)) as db:
            self.assertEqual([(1,), (2,)], db.execute("SELECT energy FROM interval_log").fetchall())

    def test_rejects_another_app(self):
        self.make_backup(slug="abcd_example")
        with self.assertRaisesRegex(ImportError, "official V2G Liberty"):
            import_backup(self.backup, self.target)
        self.assertEqual([], list(self.target.iterdir()))

    def test_refuses_an_incompatible_version(self):
        self.make_backup(version="1.0.0")
        with self.assertRaisesRegex(ImportError, "0.9.0"):
            import_backup(self.backup, self.target)

    def test_rejects_missing_history_and_links(self):
        for options in ({"database": False}, {"symlink": True}):
            with self.subTest(options=options):
                self.make_backup(**options)
                with self.assertRaises(ImportError):
                    import_backup(self.backup, self.target)
                self.assertEqual([], list(self.target.iterdir()))

    def test_refuses_to_overwrite_existing_data(self):
        self.make_backup()
        existing = self.target / SETTINGS
        existing.write_text('{"existing":true}')
        with self.assertRaisesRegex(ImportError, "only before"):
            import_backup(self.backup, self.target)
        self.assertEqual('{"existing":true}', existing.read_text())
        self.assertFalse((self.target / DATABASE).exists())

    def test_local_picker_filters_and_imports_a_large_backup(self):
        self.make_backup(slug="other")
        self.backup.rename(self.local / "other.tar")
        self.make_backup(version="1.0.0")
        self.backup.rename(self.local / "newer.tar")
        self.make_backup()
        selected = self.local / "selected.tar"
        self.backup.rename(selected)
        # The tar reader ignores padding after the end of the archive.
        with selected.open("r+b") as file:
            file.truncate(113 * 1024 * 1024)
        (self.local / "alias.tar").symlink_to(selected)
        (self.local / "broken.tar").write_bytes(b"invalid")
        self.assertEqual(
            [{"file": "selected.tar", "name": "selected.tar", "date": "", "encrypted": False}],
            list_local_backups(self.local),
        )
        with self.assertRaises(ImportError):
            local_backup_path(self.local, "../selected.tar")
        import_backup(local_backup_path(self.local, "selected.tar"), self.target)
        self.assertEqual({SETTINGS, DATABASE}, {path.name for path in self.target.iterdir()})

    def test_encrypted_local_backup_requires_its_key(self):
        password = "synthetic backup key"
        selected = self.local / "encrypted.tar"
        with SecureTarArchive(selected, mode="w", password=password) as outer:
            with outer.create_tar("./" + OFFICIAL_APP + ".tar.gz") as inner:
                tar_entry(inner, "data/" + SETTINGS, b'{"input_text.charger_type":"example"}')
                tar_entry(inner, "data/" + DATABASE, self.source.read_bytes())
            tar_entry(outer.tar, "./backup.json", json.dumps({
                "name": "Encrypted backup", "type": "partial", "protected": True,
                "addons": [{"slug": OFFICIAL_APP, "version": "0.9.0"}],
            }).encode())
        self.assertTrue(list_local_backups(self.local)[0]["encrypted"])
        for key in (None, "wrong key"):
            with self.subTest(key=key), self.assertRaisesRegex(ImportError, "encryption key"):
                import_backup(selected, self.target, password=key)
            self.assertFalse(any(self.target.iterdir()))
        import_backup(selected, self.target, password=password)
        with closing(sqlite3.connect(self.target / DATABASE)) as db:
            self.assertEqual([(1,)], db.execute("SELECT energy FROM interval_log").fetchall())

"""Exercise backup selection through the importer HTTP endpoints."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import web_ui


class BackupPickerWebTest(AioHTTPTestCase):
    async def get_application(self):
        # The test server connects on loopback. The proxy authorization policy is
        # checked independently from request parsing and archive dispatch.
        @web.middleware
        async def test_ingress(request, handler):
            if request.headers.get("X-Remote-User-Groups") != "system-admin":
                raise web.HTTPForbidden()
            return await handler(request)

        with patch.object(web_ui, "ingress_admin", test_ingress):
            return web_ui.create_app()

    async def test_local_selection_only_sends_the_filename_and_key(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "selected.tar"
            with path.open("wb") as file:
                file.truncate(113 * 1024 * 1024)
            with (
                patch.object(web_ui, "BACKUPS", path.parent),
                patch.object(web_ui, "list_local_backups", return_value=[
                    {"file": path.name, "name": "Selected", "encrypted": True}
                ]),
                patch.object(web_ui, "import_backup") as importer,
            ):
                headers = {"X-Remote-User-Groups": "system-admin"}
                response = await self.client.get("/local-backups", headers=headers)
                self.assertEqual(path.name, (await response.json())[0]["file"])
                data = {"filename": path.name, "password": "synthetic key"}
                response = await self.client.post("/import-local", headers=headers, json=data)
                self.assertEqual(403, response.status)
                headers["X-V2G-Import"] = web_ui.TOKEN
                response = await self.client.post("/import-local", headers=headers, json=data)
                self.assertEqual(200, response.status)
                importer.assert_called_once_with(path, web_ui.DATA, password="synthetic key")

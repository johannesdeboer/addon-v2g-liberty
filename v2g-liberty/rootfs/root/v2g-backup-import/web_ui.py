"""Home Assistant Ingress UI for one-time V2G Liberty backup import."""

import asyncio
from pathlib import Path
import secrets
import tempfile

from aiohttp import web

from backup_import import ImportError, MAX_ARCHIVE, import_backup

DATA = Path("/data")
TOKEN = secrets.token_urlsafe(32)
HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import V2G Liberty backup</title>
<h1>Import from official V2G Liberty</h1>
<p>Keep official V2G Liberty stopped. Upload a decrypted Home Assistant partial
backup containing only V2G Liberty 0.9.0.</p>
<form id="backup">
  <label>Home Assistant backup <input type="file" id="file" accept=".tar" required></label>
  <button id="submit">Import settings and history</button>
</form>
<p role="status" aria-live="polite" id="status"></p>
<script nonce="__TOKEN__">
const form = document.getElementById("backup");
const status = document.getElementById("status");
const button = document.getElementById("submit");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  status.textContent = "Importing backup…";
  try {
    const response = await fetch("./import", {
      method: "POST",
      headers: {"Content-Type": "application/octet-stream", "X-V2G-Import": "__TOKEN__"},
      body: document.getElementById("file").files[0]
    });
    status.textContent = await response.text();
    if (!response.ok) button.disabled = false;
  } catch {
    status.textContent = "Upload failed. Check the connection and try again.";
    button.disabled = false;
  }
});
</script>
</html>
"""


@web.middleware
async def ingress_admin(request, handler):
    peer = request.transport.get_extra_info("peername") if request.transport else None
    groups = request.headers.get("X-Remote-User-Groups", "").split(",")
    if (
        not peer or peer[0] != "172.30.32.2"
        or not request.headers.get("X-Remote-User-Id") or "system-admin" not in groups
    ):
        raise web.HTTPForbidden(text="Open this page from Home Assistant as an administrator.")
    return await handler(request)


async def index(_request):
    return web.Response(
        text=HTML.replace("__TOKEN__", TOKEN),
        content_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{TOKEN}'; connect-src 'self'; base-uri 'none'; form-action 'none'",
        },
    )


async def upload(request):
    if not secrets.compare_digest(request.headers.get("X-V2G-Import", ""), TOKEN):
        raise web.HTTPForbidden(text="Reopen the import page and try again.")
    if request.content_type != "application/octet-stream":
        raise web.HTTPUnsupportedMediaType(text="Choose a backup file to upload.")
    if request.content_length and request.content_length > MAX_ARCHIVE:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_ARCHIVE, actual_size=request.content_length)

    try:
        with tempfile.TemporaryDirectory(dir=DATA, prefix=".backup-upload-") as temp:
            backup = Path(temp) / "backup.tar"
            size = 0
            with backup.open("xb") as output:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARCHIVE:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_ARCHIVE, actual_size=size)
                    output.write(chunk)
            await asyncio.to_thread(import_backup, backup, DATA)
    except ImportError as error:
        return web.Response(status=400, text=str(error))
    except OSError:
        return web.Response(status=500, text="Unable to save the backup. Check available disk space and the app log.")
    return web.Response(
        text="Imported settings and history. Turn off Backup import mode in the app Configuration, then restart the fork."
    )


def main():
    app = web.Application(middlewares=[ingress_admin], client_max_size=MAX_ARCHIVE)
    app.add_routes([web.get("/", index), web.post("/import", upload)])
    web.run_app(app, host="0.0.0.0", port=5051, access_log=None, print=None)


if __name__ == "__main__":
    main()

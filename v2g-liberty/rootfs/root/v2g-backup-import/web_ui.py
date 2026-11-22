"""Home Assistant Ingress UI for one-time V2G Liberty backup import."""

import asyncio
from pathlib import Path
import secrets
import tempfile

from aiohttp import web

from backup_import import ImportError, MAX_ARCHIVE, import_backup, list_local_backups, local_backup_path

DATA = Path("/data")
BACKUPS = Path("/backup")
IMPORT_LOCK = web.AppKey("import_lock", asyncio.Lock)
TOKEN = secrets.token_urlsafe(32)
HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Import V2G Liberty backup</title>
<h1>Import from official V2G Liberty</h1>
<p>Keep official V2G Liberty stopped. Choose a Home Assistant partial backup
containing only V2G Liberty 0.9.0.</p>
<form id="local">
  <label>Local Home Assistant backup <select id="local-file" required>
    <option value="">Loading local backups…</option>
  </select></label>
  <label id="key-label" hidden>Backup encryption key
    <input id="key" type="password" autocomplete="off">
  </label>
  <button id="local-submit" disabled>Import selected backup</button>
</form>
<p id="local-help"></p>
<h2>Upload a backup file</h2>
<p>Use this option if the backup is stored elsewhere.</p>
<form id="backup">
  <label>Home Assistant backup <input type="file" id="file" accept=".tar" required></label>
  <button id="submit">Import settings and history</button>
</form>
<p role="status" aria-live="polite" id="status"></p>
<script nonce="__TOKEN__">
const status = document.getElementById("status");
const picker = document.getElementById("local-file");
const key = document.getElementById("key");
const keyLabel = document.getElementById("key-label");
const localButton = document.getElementById("local-submit");
const uploadButton = document.getElementById("submit");
const localHelp = document.getElementById("local-help");

function updatePicker() {
  const encrypted = picker.selectedOptions[0]?.dataset.encrypted === "true";
  keyLabel.hidden = !encrypted;
  key.required = encrypted;
  if (!encrypted) key.value = "";
}

async function loadBackups() {
  try {
    const response = await fetch("./local-backups");
    if (!response.ok) throw new Error();
    const backups = await response.json();
    picker.replaceChildren(new Option("Select a backup", ""));
    for (const backup of backups) {
      const label = [backup.name, backup.date, backup.encrypted ? "Encrypted" : ""]
        .filter(Boolean).join(" · ");
      const option = new Option(label, backup.file);
      option.dataset.encrypted = backup.encrypted;
      picker.add(option);
    }
    localButton.disabled = !backups.length;
    if (!backups.length) {
      localHelp.textContent = "No compatible local backups found. Save a V2G Liberty 0.9.0 app-only backup to this Home Assistant system, then reload this page.";
    }
  } catch {
    localHelp.textContent = "Unable to list local backups. Check the app log and reload this page.";
  }
}

async function runImport(url, type, body) {
  localButton.disabled = uploadButton.disabled = true;
  status.textContent = "Importing backup…";
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": type, "X-V2G-Import": "__TOKEN__"},
      body
    });
    if (response.status === 413) {
      status.textContent = "The upload was too large for this connection. Choose a local Home Assistant backup above.";
    } else if (response.headers.get("Content-Type")?.startsWith("text/plain")) {
      status.textContent = await response.text();
    } else {
      status.textContent = "The importer did not return a result. Check the app log before retrying.";
    }
    if (response.ok) return;
  } catch {
    status.textContent = "The connection was interrupted. Check the app log before retrying.";
  }
  localButton.disabled = picker.options.length < 2;
  uploadButton.disabled = false;
}

picker.addEventListener("change", updatePicker);
document.getElementById("local").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = JSON.stringify({filename: picker.value, password: key.value});
  key.value = "";
  await runImport("./import-local", "application/json", body);
});
document.getElementById("backup").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runImport("./import", "application/octet-stream", document.getElementById("file").files[0]);
});
loadBackups();
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


def check_import_request(request):
    if not secrets.compare_digest(request.headers.get("X-V2G-Import", ""), TOKEN):
        raise web.HTTPForbidden(text="Reopen the import page and try again.")


async def local_backups(_request):
    backups = await asyncio.to_thread(list_local_backups, BACKUPS)
    return web.json_response(backups, headers={"Cache-Control": "no-store"})


async def run_import(request, backup, password=None):
    lock = request.app[IMPORT_LOCK]
    if lock.locked():
        raise web.HTTPConflict(text="A backup import is already in progress.")
    async with lock:
        try:
            await asyncio.to_thread(import_backup, backup, DATA, password=password)
        except ImportError as error:
            return web.Response(status=400, text=str(error))
        except FileNotFoundError:
            return web.Response(status=404, text="The selected backup is no longer available. Reload the list.")
        except OSError:
            return web.Response(status=500, text="Unable to read or save the backup. Check available disk space.")
    return web.Response(
        text="Imported settings and history. Turn off Backup import mode in the app Configuration, then restart the fork."
    )


async def import_local(request):
    check_import_request(request)
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Choose a local backup from the list.")
    try:
        selection = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="Choose a local backup from the list.") from None
    if not isinstance(selection, dict):
        raise web.HTTPBadRequest(text="Choose a local backup from the list.")
    filename, password = selection.get("filename"), selection.get("password")
    if not isinstance(filename, str) or not isinstance(password, str) or len(password) > 1024:
        raise web.HTTPBadRequest(text="Choose a local backup and enter its encryption key if needed.")
    try:
        backup = local_backup_path(BACKUPS, filename)
    except ImportError as error:
        raise web.HTTPBadRequest(text=str(error)) from error
    return await run_import(request, backup, password or None)


async def upload(request):
    check_import_request(request)
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
            return await run_import(request, backup)
    except OSError:
        return web.Response(status=500, text="Unable to save the backup. Check available disk space.")


def create_app():
    app = web.Application(middlewares=[ingress_admin], client_max_size=8192)
    app[IMPORT_LOCK] = asyncio.Lock()
    app.add_routes([
        web.get("/", index),
        web.get("/local-backups", local_backups),
        web.post("/import-local", import_local),
        web.post("/import", upload),
    ])
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=5051, access_log=None, print=None)

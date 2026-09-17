#!/usr/bin/env python3
"""OpenFile web page.

A small standard-library HTTP server for the browsers of phones, tablets and
computers that cannot, or would rather not, mount a network share. It shows
what is on the volume and accepts an ability as a .py file or a .zip folder.

Uploads are staged on the volume and go through the same syntax check and
sync as any other edit. Nothing is written straight into the live abilities.
"""
import io
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import sys
import tempfile
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import devkit_functions as df  # noqa: E402

PORT = int(os.environ.get("OPENFILE_WEB_PORT") or 8088)
TOKEN = os.environ.get("OPENFILE_WEB_TOKEN") or ""
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
ABILITY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class UploadError(ValueError):
    pass


def ability_name_from(filename: str) -> str:
    stem = pathlib.PurePosixPath(filename.replace("\\", "/")).name.rsplit(".", 1)[0]
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    if name in ("devkit_functions", "main", ""):
        name = "custom_ability"
    if not ABILITY_NAME.match(name):
        raise UploadError("That file name cannot be used as an ability name.")
    return name


def check_python(source: bytes, label: str) -> None:
    import ast
    try:
        ast.parse(source.decode("utf-8"), filename=label)
    except (SyntaxError, UnicodeDecodeError) as e:
        raise UploadError(f"{label} is not valid Python: {e}")


def extract_zip(body: bytes, dest: pathlib.Path) -> None:
    """Extract an archive into dest, refusing anything that could land outside it."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        raise UploadError("That is not a zip file.")
    members = archive.infolist()
    if len(members) > MAX_ZIP_MEMBERS:
        raise UploadError("The archive has too many files.")
    if sum(m.file_size for m in members) > MAX_EXTRACTED_BYTES:
        raise UploadError("The archive is too large once extracted.")

    root = dest.resolve()
    for member in members:
        name = member.filename.replace("\\", "/")
        if stat.S_ISLNK(member.external_attr >> 16):
            raise UploadError("Archives with symbolic links are not accepted.")
        if name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
            raise UploadError("The archive contains a path outside its own folder.")
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise UploadError("The archive contains a path outside its own folder.")
        if name.startswith("__MACOSX/") or pathlib.PurePosixPath(name).name.startswith("."):
            continue
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def find_ability_dir(staging: pathlib.Path) -> pathlib.Path:
    """The folder inside an extracted archive that holds devkit_functions.py."""
    if (staging / "devkit_functions.py").is_file():
        return staging
    found = sorted(p.parent for p in staging.rglob("devkit_functions.py"))
    if len(found) != 1:
        raise UploadError("The archive must contain one ability, and only one, with a devkit_functions.py.")
    return found[0]


def stage_upload(filename: str, body: bytes, drive: pathlib.Path) -> str:
    """Validate an upload and place it under <drive>/abilities. Returns the ability name."""
    if len(body) > MAX_UPLOAD_BYTES:
        raise UploadError("The file is too large.")
    abilities = drive / "abilities"
    abilities.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".upload-", dir=str(abilities)) as tmp:
        staging = pathlib.Path(tmp) / "content"
        staging.mkdir()
        if filename.lower().endswith(".zip"):
            extract_zip(body, staging)
            source = find_ability_dir(staging)
            name = ability_name_from(filename if source == staging else source.name)
        elif filename.lower().endswith(".py"):
            source, name = staging, ability_name_from(filename)
            (staging / "devkit_functions.py").write_bytes(body)
        else:
            raise UploadError("Upload a .py file or a .zip of an ability folder.")

        for py in sorted(source.rglob("*.py")):
            check_python(py.read_bytes(), str(py.relative_to(source)))

        target = abilities / name
        target.mkdir(exist_ok=True)
        for item in sorted(source.rglob("*")):
            if item.is_file():
                out = target / item.relative_to(source)
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, out)
    return name


def host_allowed(host_header: str) -> bool:
    """Accept requests addressed to this device by name or by IP address.

    A page on another site can point its own domain at a private address. The
    browser then treats it as same-origin, but it still sends that domain in
    Host, which is how it gets turned away here.
    """
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        host = host[1:host.index("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    own = socket.gethostname().lower()
    return host in {"localhost", own, f"{own}.local", own.split(".")[0], f"{own.split('.')[0]}.local"}


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenFile</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --text:#e6edf3; --dim:#8b949e; --ok:#3fb950; --off:#6e7681; --accent:#2f81f7; --bad:#f85149; }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f8fa; --card:#fff; --line:#d0d7de; --text:#1f2328; --dim:#656d76; } }
  * { box-sizing: border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:900px; margin:0 auto; display:grid; gap:16px; }
  h1 { font-size:20px; margin:0; } h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em; color:var(--dim); margin:0 0 12px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .pill { border:1px solid var(--line); border-radius:999px; padding:3px 10px; font-size:13px; }
  .pill::before { content:""; display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--off); margin-right:6px; }
  .pill.on::before { background:var(--ok); }
  button { font:inherit; color:var(--text); background:transparent; border:1px solid var(--line); border-radius:6px; padding:7px 12px; cursor:pointer; }
  button:hover { border-color:var(--accent); } button:disabled { opacity:.45; cursor:default; } button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  #drop { border:2px dashed var(--line); border-radius:10px; padding:28px 16px; text-align:center; color:var(--dim); cursor:pointer; }
  #drop.over { border-color:var(--accent); color:var(--text); }
  ul { list-style:none; margin:0; padding:0; display:grid; gap:6px; } li { display:flex; justify-content:space-between; gap:12px; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px; }
  li span:last-child { color:var(--dim); }
  pre { margin:0; max-height:240px; overflow:auto; font:12px/1.6 ui-monospace,Menlo,Consolas,monospace; white-space:pre-wrap; color:var(--dim); }
  #msg { min-height:1.5em; font-size:14px; } #msg.bad { color:var(--bad); } #msg.good { color:var(--ok); }
</style>
</head>
<body>
<main>
  <div class="row" style="justify-content:space-between">
    <h1>OpenFile <span id="host" style="color:var(--dim);font-weight:400"></span></h1>
    <div class="row" id="pills"></div>
  </div>

  <section>
    <h2>Add an ability</h2>
    <div id="drop" tabindex="0">Drop a .py file or a .zip of an ability folder, or tap to choose one.
      <input type="file" id="file" accept=".py,.zip" hidden></div>
    <p id="msg"></p>
    <div class="row">
      <button class="primary" data-act="sync">Refresh</button>
      <button data-act="undo" id="undo">Undo last change</button>
      <button data-act="reseed">Reseed from device</button>
      <button data-act="restart">Restart services</button>
      <button data-act="eject_usb">Eject flash drive</button>
    </div>
  </section>

  <section id="attention" hidden><h2 style="color:var(--bad)">Needs attention</h2><ul id="notices"></ul></section>

  <section><h2>On the volume <span id="space" style="text-transform:none;letter-spacing:0"></span></h2><ul id="abilities"></ul></section>
  <section><h2>Ports</h2><ul id="ports"></ul></section>
  <section><h2>Sync log</h2><pre id="log"></pre></section>
</main>
<script>
  const token = new URLSearchParams(location.search).get('token') || '';
  const headers = { 'X-OpenFile': '1' };
  if (token) headers['X-OpenFile-Token'] = token;
  const $ = id => document.getElementById(id);
  const say = (text, cls) => { $('msg').textContent = text; $('msg').className = cls || ''; };
  const item = (left, right) => { const li = document.createElement('li'), a = document.createElement('span'), b = document.createElement('span'); a.textContent = left; b.textContent = right; li.append(a, b); return li; };
  const get = path => fetch(path, { headers }).then(r => r.ok ? r : Promise.reject(r.status));

  async function refresh() {
    try {
      const status = await (await get('/api/status')).json();
      $('host').textContent = status.hostname;
      $('space').textContent = status.mounted ? `· ${status.free_mb} MB free of ${status.total_mb}` : '· drive is off';
      const names = { network_share: 'Network share', web_portal: 'Web page', flash_drive: 'Flash drive' };
      $('pills').replaceChildren(...Object.entries(names).map(([k, label]) => { const s = document.createElement('span'); s.className = 'pill' + (status.transports[k] ? ' on' : ''); s.textContent = label; return s; }));
      $('abilities').replaceChildren(...(status.abilities.length ? status.abilities.map(a => item(a.name, `${a.files} files`)) : [item('Nothing here yet', '')]));

      const ports = await (await get('/api/ports')).json();
      const rows = [item('USB-C', 'power input')];
      ports.usb_a.storage.forEach(d => rows.push(item('USB-A storage', `${d.model} · ${d.size_mb} MB`)));
      ports.usb_a.devices.forEach(d => rows.push(item(`USB ${d.port}`, d.product)));
      Object.entries(ports.network).forEach(([n, i]) => rows.push(item(n, i.ip ? `${i.ip} · ${i.state}` : i.state)));
      rows.push(item('Bluetooth', ports.bluetooth.address ? 'present' : 'not present'));
      Object.entries(ports.video).forEach(([n, s]) => rows.push(item(n, s)));
      ports.audio.forEach(c => rows.push(item('Audio', c)));
      $('ports').replaceChildren(...rows);

      const safety = await (await get('/api/safety')).json();
      lastChange = safety.history.find(h => !h.undone) || null;
      $('undo').disabled = !lastChange;
      $('undo').title = lastChange ? 'Reverses: ' + describe(lastChange) : 'Nothing to undo';
      $('attention').hidden = safety.attention.length === 0;
      $('notices').replaceChildren(...safety.attention.map(n => item(n.text, n.when)));
      installed = status.abilities.map(a => a.name);

      const log = await (await get('/api/logs')).text();
      $('log').textContent = log || 'Nothing logged yet.';
      $('log').scrollTop = $('log').scrollHeight;
    } catch (e) { say(e === 401 ? 'This page needs its access token in the address.' : 'The device did not answer.', 'bad'); }
  }

  async function post(path, body, extra) {
    const r = await fetch(path, { method: 'POST', headers: Object.assign({}, headers, extra), body });
    const data = await r.json().catch(() => ({}));
    say(data.message || data.error || (r.ok ? 'Done.' : 'That did not work.'), r.ok && data.success !== false ? 'good' : 'bad');
    refresh();
  }

  let lastChange = null, installed = [];
  const describe = h => [...Object.entries(h.abilities).map(([n, c]) => `${n} (${c} file${c === 1 ? '' : 's'})`), ...(h.settings.length ? ['settings: ' + h.settings.join(', ')] : [])].join(', ');
  const questions = {
    reseed: () => 'Replace what is on the volume with what is on the device? The current copies are kept in backup/.',
    restart: () => 'Restart the OpenFile services? This page and the network share drop for a few seconds.',
    undo: () => 'Put back the previous version of: ' + (lastChange ? describe(lastChange) : 'nothing') + '? What you undo is kept in backup/.',
  };
  document.querySelectorAll('button[data-act]').forEach(b => b.onclick = () => {
    const ask = questions[b.dataset.act];
    if (ask && !confirm(ask())) return;
    say('Working…'); post('/api/' + b.dataset.act);
  });
  const upload = f => { if (!f) return;
    const name = f.name.replace(/[.][^.]+$/, '');
    if (installed.includes(name) && !confirm(`"${name}" is already installed. Replace it? The version running now is saved first, and Undo brings it back.`)) return;
    say(`Checking ${f.name}…`); post('/api/upload', f, { 'X-Filename': encodeURIComponent(f.name), 'Content-Type': 'application/octet-stream' }); };
  const drop = $('drop');
  drop.onclick = () => $('file').click();
  drop.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') $('file').click(); };
  $('file').onchange = e => { upload(e.target.files[0]); e.target.value = ''; };
  ['dragenter', 'dragover'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(n => drop.addEventListener(n, e => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', e => upload(e.dataTransfer.files[0]));
  refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DualStackHTTPServer(ThreadedHTTPServer):
    """Listens on IPv6 and IPv4 through one socket.

    A cable run straight from a computer to the DevKit, with no router to hand
    out addresses, can leave the two with IPv6 link-local addresses only.
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(port: int):
    try:
        return DualStackHTTPServer(("::", port), OpenFileWebHandler)
    except OSError:
        # No IPv6 on this system. IPv4 alone is still a working web page.
        return ThreadedHTTPServer(("0.0.0.0", port), OpenFileWebHandler)


class OpenFileWebHandler(BaseHTTPRequestHandler):
    server_version = "OpenFile"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def send_body(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data).encode("utf-8"), "application/json", status)

    def authorised(self, write: bool) -> bool:
        """Turn away other sites' pages, and anyone without the token when one is set."""
        if not host_allowed(self.headers.get("Host", "")):
            self.send_json({"success": False, "error": "Unknown host."}, 421)
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
            self.send_json({"success": False, "error": "Cross-site request refused."}, 403)
            return False
        # A custom header cannot be sent cross-site without a preflight, and
        # this server answers no preflight.
        if write and self.headers.get("X-OpenFile") != "1":
            self.send_json({"success": False, "error": "Missing request header."}, 403)
            return False
        if TOKEN:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if TOKEN not in (self.headers.get("X-OpenFile-Token"), (query.get("token") or [""])[0]):
                self.send_json({"success": False, "error": "Access token required."}, 401)
                return False
        return True

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.authorised(write=False):
            return

        if path in ("/", "/index.html"):
            self.send_body(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            data = df.get_status_dict()
            root = pathlib.Path(df.DRIVE_MOUNT) / "abilities"
            data["abilities"] = [{"name": n, "files": sum(1 for f in (root / n).rglob("*") if f.is_file())}
                                 for n in data["abilities"]]
            data["hostname"] = socket.gethostname()
            self.send_json(data)
        elif path == "/api/ports":
            self.send_json(df.get_hardware_ports())
        elif path == "/api/safety":
            log_path = pathlib.Path(df.DRIVE_MOUNT) / "logs" / "sync_history.log"
            notices = [line for line in df._read_text(log_path).splitlines()[-400:] if "] ATTENTION: " in line][-5:]
            self.send_json({
                "history": df.DriveSyncEngine().list_snapshots()[:10],
                "attention": [{"when": n[1:20], "text": n.split("] ATTENTION: ", 1)[1]} for n in reversed(notices)],
            })
        elif path == "/api/logs":
            log_path = pathlib.Path(df.DRIVE_MOUNT) / "logs" / "sync_history.log"
            lines = df._read_text(log_path).splitlines()[-100:]
            self.send_body("\n".join(lines).encode("utf-8"), "text/plain; charset=utf-8")
        else:
            self.send_json({"success": False, "error": "Not found."}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self.authorised(write=True):
            return
        engine = df.DriveSyncEngine()

        if path == "/api/eject_usb":
            result = engine.eject_external_usb()
            self.send_json({"success": result.get("success", False),
                            "message": result.get("message") or result.get("error")})
            return
        if path == "/api/restart":
            import subprocess
            subprocess.Popen(["systemd-run", "--quiet", "--collect", "--on-active=2",
                              "systemctl", "restart", *df.installed_services()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.send_json({"success": True, "message": "Restarting. The page will come back in a few seconds."})
            return

        if not df.is_drive_mounted():
            self.send_json({"success": False, "error": "The drive is off. Turn it on first."}, 409)
            return

        if path == "/api/undo":
            result = engine.undo_last()
            what = ", ".join(result.get("abilities", []) + (["settings"] if result.get("settings") else []))
            self.send_json({"success": result["success"],
                            "message": f"Undone: {what}." if result["success"] else result["error"]},
                           200 if result["success"] else 409)
            return

        if path in ("/api/sync", "/api/reseed"):
            result = engine.run_full_sync(reseed=path == "/api/reseed")
            rejected = len(result["errors"])
            held = len(result.get("warnings") or [])
            message = ("A sync is already running." if result.get("skipped")
                       else f"{result['abilities_synced']} updated, {rejected} rejected"
                            + (f", {held} held back. See Needs attention." if held else "."))
            self.send_json({"success": result["success"], "message": message, "details": result})
            return

        if path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_UPLOAD_BYTES:
                self.send_json({"success": False, "error": "The file is empty or too large."}, 413)
                return
            filename = urllib.parse.unquote(self.headers.get("X-Filename") or "")
            try:
                name = stage_upload(filename, self.rfile.read(length), pathlib.Path(df.DRIVE_MOUNT))
            except UploadError as e:
                engine.log(f"Web upload refused: {e}")
                self.send_json({"success": False, "error": str(e)}, 400)
                return
            result = engine.run_full_sync()
            engine.log(f"Web upload installed ability '{name}'")
            self.send_json({"success": result["success"], "ability": name,
                            "message": f"{name} installed." if result["success"] else f"{name} was rejected. See the log."})
            return

        self.send_json({"success": False, "error": "Not found."}, 404)


def run_web_portal(port=PORT):
    server = make_server(port)
    print(f"OpenFile web page listening on port {port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_web_portal(int(sys.argv[1]) if len(sys.argv) > 1 else PORT)

#!/usr/bin/env python3
"""OpenFile stress test. Run on the DevKit: sudo python3 tests/device_stress_test.py

The ability code runs for real, on the device, against a sandbox: a copy of
the device's abilities, sounds and .env in a temporary folder, with its own
watcher and web page. Chimes are off. Nothing here writes to the live
abilities, the live .env or the speaker. The last phase looks at the live
install and only reads.
"""
import concurrent.futures
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
DEVKIT_FN = str(PACKAGE_DIR / "devkit_functions.py")
LIVE_HOME = pathlib.Path(os.environ.get("OPENHOME_DEVICE_HOME") or "/home/openhome")
LIVE_MOUNT = pathlib.Path("/mnt/openfile")
ALLOWED_SHARES = {"OpenFile", "Abilities-Live", "DevKit-Home"}
SECRET_KEYS = ("API_KEY", "MQTT_PASSWORD", "MQTT_USERNAME")
WAIT = 12.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, seconds=WAIT):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


class StressTest:
    def __init__(self):
        self.results = []
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_stress_"))
        self.drive = self.tmp / "volume"
        self.home = self.tmp / "home"
        self.caps = self.home / "openhome_devkit" / "local_capabilities"
        self.port = free_port()
        self.env = dict(os.environ, OPENFILE_MOUNT=str(self.drive), OPENHOME_DEVICE_HOME=str(self.home),
                        LOCAL_CAPABILITIES_DIR=str(self.caps), OPENFILE_STATE=str(self.tmp / "state.json"),
                        OPENFILE_SNAPSHOTS=str(self.tmp / "snapshots"),
                        OPENFILE_LOCK=str(self.tmp / "lock"), OPENFILE_PID=str(self.tmp / "pid"),
                        OPENFILE_PLAIN_DIR="1", OPENFILE_CHIMES="0", OPENFILE_PIP="0", OPENFILE_USB_SCAN="0",
                        OPENFILE_WEB_PORT=str(self.port), OPENFILE_WEB_TOKEN="")
        self.procs = []
        self.secrets = []

    # ── plumbing ──────────────────────────────────────────────────────────────
    def call(self, *args):
        res = subprocess.run([sys.executable, DEVKIT_FN, *args], env=self.env, capture_output=True, text=True)
        return res.returncode, json.loads(res.stdout.strip().splitlines()[-1])

    def call_live(self, *args):
        """Ask the installed OpenFile, not the sandbox. Read-only functions only."""
        res = subprocess.run([sys.executable, DEVKIT_FN, *args], capture_output=True, text=True)
        try:
            return res.returncode, json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return res.returncode, {}

    def record(self, name, passed, details):
        print(f"  [{'PASS' if passed else 'FAIL'}] {len(self.results) + 1:02d} {name}\n         {details}")
        self.results.append({"name": name, "passed": bool(passed), "details": details})

    def phase(self, name, fn):
        try:
            passed, details = fn()
        except Exception as e:  # a crash is a failure, with its reason on the record
            passed, details = False, f"{type(e).__name__}: {e}"
        self.record(name, passed, details)

    def build_sandbox(self):
        """Copy the device's real abilities, sounds and .env so the code meets real content."""
        live_caps = LIVE_HOME / "openhome_devkit" / "local_capabilities"
        if live_caps.is_dir():
            shutil.copytree(live_caps, self.caps, ignore=shutil.ignore_patterns("__pycache__", ".git"), symlinks=True)
        self.caps.mkdir(parents=True, exist_ok=True)
        audio = LIVE_HOME / "openhome_devkit" / "audio_files"
        if audio.is_dir():
            shutil.copytree(audio, self.home / "openhome_devkit" / "audio_files")
        live_env = LIVE_HOME / ".env"
        text = live_env.read_text() if live_env.is_file() else "API_KEY=stand-in-secret\nSPEAKER_VOLUME=50\nMIC_SENSITIVITY=160\n"
        (self.home / ".env").write_text("# kept comment\n" + text)
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() in SECRET_KEYS and len(value.strip()) >= 6:
                self.secrets.append(value.strip())
        self.drive.mkdir()

    def start(self, *args):
        proc = subprocess.Popen([sys.executable, *args], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return proc

    def http(self, method, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def write_ability(self, name, body):
        folder = self.drive / "abilities" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "devkit_functions.py").write_text(body)

    # ── phases ────────────────────────────────────────────────────────────────
    def seed(self):
        code, reply = self.call("enable_drive")
        expected = sorted(d.name for d in self.caps.iterdir() if d.is_dir() and ".retired" not in d.name
                          and not d.name.endswith((".bak", ".old", ".disabled")) and not d.name.startswith("."))
        seeded = sorted(d.name for d in (self.drive / "abilities").iterdir() if d.is_dir())
        sounds = len(list((self.drive / "sounds").iterdir()))
        files = [f for f in self.drive.rglob("*") if f.is_file()]
        leaked = [str(f.relative_to(self.drive)) for f in files
                  if any(s.encode() in f.read_bytes() for s in self.secrets)]
        agents = sorted(f.name for f in (self.drive / "agents").rglob("*") if f.is_file())
        ok = (code == 0 and reply["success"] and seeded == expected and len(files) > 3
              and not leaked and agents == ["README.md"] and not (self.drive / "config" / ".env").exists())
        return ok, (f"{len(seeded)} abilities match the device, {sounds} sounds, {len(files)} files scanned for "
                    f"{len(self.secrets)} live secret(s): {len(leaked)} leaked, agents/ holds {agents}")

    def burst(self):
        names = [f"burst_{i}" for i in range(10)]
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: self.write_ability(n, f"NAME = '{n}'\n"), names))
        ok = wait_for(lambda: all((self.caps / n / "devkit_functions.py").is_file() for n in names))
        staged = sum((self.caps / n / "devkit_functions.py").is_file() for n in names)
        return ok, f"{staged}/10 abilities written in parallel reached the device in {time.time() - start:.1f}s"

    def execute(self):
        self.write_ability("adder", "import json, sys\nprint(json.dumps({'sum': sum(int(a) for a in sys.argv[1:])}))\n")
        target = self.caps / "adder" / "devkit_functions.py"
        if not wait_for(target.is_file):
            return False, "the ability never reached the device"
        out = subprocess.run([sys.executable, str(target), "15", "25", "60"], capture_output=True, text=True)
        return json.loads(out.stdout)["sum"] == 100, f"synced ability ran on the device and returned {out.stdout.strip()}"

    def chaos(self):
        good, bad = [f"good_{i}" for i in range(5)], [f"bad_{i}" for i in range(10)]
        for n in bad:
            self.write_ability(n, "def (: nope @@\n")
        for n in good:
            self.write_ability(n, "OK = True\n")
        ok = wait_for(lambda: all((self.caps / n).is_dir() for n in good))
        time.sleep(2.5)  # give a wrongly accepted file time to show up
        leaked = [n for n in bad if (self.caps / n).exists()]
        log = (self.drive / "logs" / "sync_history.log").read_text()
        explained = sum(f"Rejected '{n}'" in log for n in bad)
        return ok and not leaked and explained == 10, (
            f"5/5 valid installed, {len(leaked)}/10 broken leaked through, {explained}/10 rejections explained in the log")

    def concurrent_calls(self):
        def one(i):
            code, reply = self.call(("get_status", "health", "get_ports")[i % 3])
            return code == 0 and reply["success"]
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            good = sum(pool.map(one, range(45)))
        return good == 45, f"{good}/45 concurrent status, health and ports calls answered in {time.time() - start:.1f}s"

    def settings(self):
        env_file = self.home / ".env"
        before = env_file.read_text()
        settings = self.drive / "config" / "settings.env"
        for volume in (45, 60, 75, 88, 92):
            settings.write_text(f"SPEAKER_VOLUME={volume}\nMIC_SENSITIVITY=170\nAPI_KEY=hijacked\nSERVER_URL_API=http://evil\n")
            self.call("sync_drive")
        after = env_file.read_text()
        untouched = [l for l in before.splitlines() if not l.startswith(("SPEAKER_VOLUME=", "MIC_SENSITIVITY="))]
        kept = all(l in after.splitlines() for l in untouched)
        ok = ("SPEAKER_VOLUME=92" in after and "MIC_SENSITIVITY=170" in after and kept
              and "hijacked" not in after and "evil" not in after and after.startswith("# kept comment"))
        return ok, (f"5 rapid edits applied, {len(untouched)} other lines of .env intact: {kept}, "
                    f"planted API_KEY and server URL refused: {'hijacked' not in after and 'evil' not in after}")

    def two_way(self):
        self.write_ability("deployed", "VERSION = 1\n")
        device = self.caps / "deployed" / "devkit_functions.py"
        if not wait_for(device.is_file):
            return False, "setup never reached the device"
        device.write_text("VERSION = 2\n")  # what an openhome deploy does
        self.call("sync_drive")
        drive = (self.drive / "abilities" / "deployed" / "devkit_functions.py").read_text()
        return device.read_text() == drive == "VERSION = 2\n", (
            f"device update kept ({device.read_text().strip()}) and copied to the drive ({drive.strip()})")

    def undo(self):
        self.write_ability("fragile", "WORKS = True\n")
        device = self.caps / "fragile" / "devkit_functions.py"
        if not wait_for(device.is_file):
            return False, "setup never reached the device"
        self.write_ability("fragile", "WORKS = 'it parses, and it is wrong'\n")
        if not wait_for(lambda: "wrong" in device.read_text()):
            return False, "the bad edit never reached the device"
        if not wait_for(lambda: self.http("GET", "/api/status")[0] == 200):
            return False, "the web page never came up"
        status, body = self.http("POST", "/api/undo", b"", {"X-OpenFile": "1"})
        back = device.read_text() == "WORKS = True\n"
        shown = (self.drive / "abilities" / "fragile" / "devkit_functions.py").read_text() == "WORKS = True\n"
        time.sleep(3.5)  # a watcher pass must not put the bad edit back
        stays = device.read_text() == "WORKS = True\n"

        settings = self.drive / "config" / "settings.env"
        settings.write_text("SPEAKER_VOLUME=5000\n")
        self.call("sync_drive")
        refused = "SPEAKER_VOLUME=5000" not in (self.home / ".env").read_text()
        notices = json.loads(self.http("GET", "/api/safety")[1])["attention"]
        told = any("SPEAKER_VOLUME=5000" in n["text"] for n in notices)
        return status == 200 and back and shown and stays and refused and told, (
            f"bad edit reversed by the web page's Undo: {back}, volume shows the running version: {shown}, "
            f"stayed reversed after a watcher pass: {stays}; out-of-range volume refused: {refused}, "
            f"and explained under Needs attention: {told}")

    def lock(self):
        for i in range(6):
            self.write_ability(f"race_{i}", f"R = {i}\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda _: self.call("sync_drive"), range(6)))
        clean = all(code == 0 and reply["success"] for code, reply in replies)
        skipped = sum(bool(reply["data"].get("skipped")) for _, reply in replies)
        ran = sum(1 for _, reply in replies if "abilities_synced" in reply["data"] and not reply["data"].get("skipped"))
        manifest = json.loads((self.tmp / "state.json").read_text())
        landed = all((self.caps / f"race_{i}").is_dir() for i in range(6))
        return clean and landed and "race_5" in manifest and skipped == 0, (
            f"6 simultaneous syncs: all clean, {ran} ran in turn, {skipped} dropped, "
            f"manifest intact with {len(manifest)} abilities")

    def web(self):
        if not wait_for(lambda: self.http("GET", "/api/status")[0] == 200):
            return False, "the web page never came up"
        write = {"X-OpenFile": "1", "X-Filename": "beamed.py"}
        checks = {
            "page loads": self.http("GET", "/")[0] == 200,
            "post from another site refused": self.http("POST", "/api/sync", b"", {"X-OpenFile": "1", "Origin": "http://evil.example"})[0] == 403,
            "post without the header refused": self.http("POST", "/api/upload", b"x = 1\n", {"X-Filename": "sneak.py"})[0] == 403,
            "foreign host name refused": self.http("GET", "/api/status", headers={"Host": "evil.example"})[0] == 421,
            "broken python refused": self.http("POST", "/api/upload", b"def (:\n", dict(write, **{"X-Filename": "broken.py"}))[0] == 400,
            "shell script refused": self.http("POST", "/api/upload", b"echo hi\n", dict(write, **{"X-Filename": "run.sh"}))[0] == 400,
            "good upload accepted": self.http("POST", "/api/upload", b"BEAMED = True\n", write)[0] == 200,
        }
        checks["good upload reached the device"] = wait_for((self.caps / "beamed" / "devkit_functions.py").is_file)
        checks["refused uploads left nothing"] = not any((self.caps / n).exists() for n in ("sneak", "broken", "run"))
        failed = [name for name, ok in checks.items() if not ok]
        return not failed, f"{len(checks) - len(failed)}/{len(checks)} web checks held" + (f". Failed: {failed}" if failed else "")

    def memory(self, watcher):
        rss_kb = int(subprocess.run(["ps", "-o", "rss=", "-p", str(watcher.pid)], capture_output=True, text=True).stdout.strip() or 0)
        alive = watcher.poll() is None
        return alive and 0 < rss_kb < 60 * 1024, f"watcher still running: {alive}, resident memory {rss_kb / 1024:.1f} MB after every phase above"

    def live_install(self):
        """Read-only look at the live install. Skipped, and says so, when OpenFile is not installed."""
        installed = (os.path.ismount(LIVE_MOUNT) or pathlib.Path("/etc/default/openfile").exists()
                     or any(pathlib.Path("/etc/systemd/system").glob("openfile-*.service")))
        if not installed:
            return True, "SKIPPED: no OpenFile volume, settings or services on this device, so there was nothing live to inspect"
        findings = []
        units = ["openfile-gadget", "openfile-sync"]
        if pathlib.Path("/etc/systemd/system/openfile-web.service").exists():
            units.append("openfile-web")  # the web page is optional
        for unit in units:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode != 0:
                findings.append(f"{unit} is not running")
        live_env = LIVE_HOME / ".env"
        secrets = [v.strip() for k, _, v in (l.partition("=") for l in live_env.read_text().splitlines())
                   if k.strip() in SECRET_KEYS and len(v.strip()) >= 6] if live_env.is_file() else []
        scanned = 0
        for f in LIVE_MOUNT.rglob("*"):
            if f.is_file() and f.stat().st_size < 2_000_000 and f.suffix.lower() not in (".mp3", ".wav", ".ogg"):
                scanned += 1
                data = f.read_bytes()
                if any(s.encode() in data for s in secrets):
                    findings.append(f"a live secret is on the volume in {f.relative_to(LIVE_MOUNT)}")
        if not scanned:
            findings.append("the live volume had no files to scan")
        code, reply = self.call_live("health")
        discovery = reply.get("data", {}).get("discovery", {})
        if not discovery.get("windows_discovery"):
            findings.append("the device did not answer a Windows-style discovery probe")
        if not discovery.get("mdns"):
            findings.append("the share is not announced over mDNS for Macs and Linux")
        shares = {}
        current = None
        for line in subprocess.run(["testparm", "-s"], capture_output=True, text=True).stdout.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                shares[current] = {}
            elif current and "=" in line:
                key, _, value = line.partition("=")
                shares[current][key.strip()] = value.strip()
        ours = {n: o for n, o in shares.items() if n not in ("global", "homes", "printers", "print$")}
        for name, options in ours.items():
            if name not in ALLOWED_SHARES:
                findings.append(f"unexpected share [{name}] -> {options.get('path')}")
            if options.get("path") == "/":
                findings.append(f"share [{name}] exposes the whole filesystem")
            if name != "OpenFile" and options.get("guest ok", "No").lower() == "yes":
                findings.append(f"share [{name}] is open without a login")
        return not findings, (f"{scanned} live files scanned for {len(secrets)} secret(s), {len(ours)} share(s) checked, "
                              f"discovery Windows={discovery.get('windows_discovery')} mDNS={discovery.get('mdns')}. "
                              + ("; ".join(findings) if findings else "Nothing found."))

    # ── run ───────────────────────────────────────────────────────────────────
    def run(self):
        print(f"\nOpenFile stress test on {socket.gethostname()} ({os.uname().machine}, kernel {os.uname().release})")
        print(f"Sandbox: {self.tmp}\n")
        try:
            self.build_sandbox()
            self.phase("Seed the volume from real device content, leaking nothing", self.seed)
            watcher = self.start(DEVKIT_FN, "watch_drive", "--debounce", "1.0")
            self.start(str(PACKAGE_DIR / "device" / "web_portal.py"))
            self.phase("Ten abilities written at once", self.burst)
            self.phase("A synced ability runs on the device", self.execute)
            self.phase("Ten broken abilities among five good ones", self.chaos)
            self.phase("Forty-five concurrent calls", self.concurrent_calls)
            self.phase("Rapid settings edits with planted secrets", self.settings)
            self.phase("An update made on the device survives a sync", self.two_way)
            self.phase("A bad edit is undone, a bad setting is refused", self.undo)
            self.phase("Six syncs at the same instant", self.lock)
            self.phase("Web page under hostile requests", self.web)
            self.phase("Watcher health after all of it", lambda: self.memory(watcher))
            self.phase("Live install, read only", self.live_install)
        finally:
            for proc in self.procs:
                proc.terminate()
            for proc in self.procs:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            mounted = [l.split()[1] for l in open("/proc/mounts") if l.split()[1].startswith(str(self.tmp) + "/")] \
                if os.path.exists("/proc/mounts") else []
            if mounted:
                print(f"NOT removing {self.tmp}: mounted inside it: {mounted}")
            else:
                shutil.rmtree(self.tmp, ignore_errors=True)

        failed = [r for r in self.results if not r["passed"]]
        print(f"\n{len(self.results) - len(failed)} of {len(self.results)} phases passed.")
        for r in failed:
            print(f"  FAILED: {r['name']}")
        receipt = {"host": socket.gethostname(), "kernel": os.uname().release, "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "passed": len(self.results) - len(failed), "total": len(self.results), "results": self.results}
        print("\nRECEIPT " + json.dumps(receipt))
        return 1 if failed or not self.results else 0


if __name__ == "__main__":
    sys.exit(StressTest().run())

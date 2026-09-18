#!/usr/bin/env python3
"""OpenHome DevKit hardware function dispatcher and sync engine for OpenFile.

Executed on the DevKit by OpenHome's node server with root privileges:
    sudo python3 devkit_functions.py <function_name> [args...]

All output is printed as a single JSON object to stdout.
"""
import ast
import contextlib
import datetime
import fcntl
import fnmatch
import glob
import hashlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# install.sh records the owner's choices here. The OpenHome node server starts
# this file through sudo with a clean environment, so read them directly.
DEFAULTS_FILE = "/etc/default/openfile"
try:
    with open(DEFAULTS_FILE, "r", encoding="utf-8") as _defaults:
        for _line in _defaults:
            _key, _sep, _value = _line.strip().partition("=")
            if _sep and _key and not _key.startswith("#"):
                os.environ.setdefault(_key, _value)
except OSError:
    pass

# Drive and system paths
DEVICE_HOME = os.environ.get("OPENHOME_DEVICE_HOME") or "/home/openhome"
DRIVE_MOUNT = os.environ.get("OPENFILE_MOUNT") or "/mnt/openfile"
DRIVE_IMG = os.environ.get("OPENFILE_IMG") or "/var/lib/openhome/openfile.img"
USB_EXTERNAL_MOUNT = os.environ.get("OPENFILE_USB_MOUNT") or "/mnt/openfile_usb"
CAPS_DIR = (os.environ.get("LOCAL_CAPABILITIES_DIR") or
            os.path.join(DEVICE_HOME, "openhome_devkit/local_capabilities"))
CHIME_SUCCESS = (os.environ.get("OPENFILE_CHIME_SUCCESS") or
                 os.path.join(DEVICE_HOME, "openhome_devkit/audio_files/audio_complete.mp3"))
CHIME_DECLINE = (os.environ.get("OPENFILE_CHIME_DECLINE") or
                 os.path.join(DEVICE_HOME, "openhome_devkit/audio_files/general_error.mp3"))
LOCK_FILE = os.environ.get("OPENFILE_LOCK") or "/run/openfile.lock"
PID_FILE = os.environ.get("OPENFILE_PID") or "/run/openfile-sync.pid"
STATE_FILE = os.environ.get("OPENFILE_STATE") or "/var/lib/openhome/openfile-state.json"
IMG_SIZE_MB = int(os.environ.get("OPENFILE_IMG_SIZE_MB") or 1024)

# Use an ordinary folder as the volume instead of a FAT image. The gadget needs
# the image; every other transport works the same either way.
PLAIN_DIR = os.environ.get("OPENFILE_PLAIN_DIR", "0") == "1"
CHIMES_ENABLED = os.environ.get("OPENFILE_CHIMES", "1") != "0"
# Installing an ability's requirements reaches the network and changes the system
# interpreter. Tests turn it off.
PIP_ENABLED = os.environ.get("OPENFILE_PIP", "1") != "0"
# Looking for flash drives means touching real hardware, and acting on one
# means mounting it. Tests turn this off so they can never reach a real drive.
USB_SCAN_ENABLED = os.environ.get("OPENFILE_USB_SCAN", "1") != "0"
# A sync that finds another one running waits this long for its turn.
LOCK_WAIT_SECONDS = float(os.environ.get("OPENFILE_LOCK_WAIT") or 30)
# The watcher sees edits on the volume. An update made on the device, such as
# an `openhome deploy`, is picked up by this periodic pass.
RESYNC_SECONDS = float(os.environ.get("OPENFILE_RESYNC_SECONDS") or 60)

# The device .env holds the account API key and broker password. Only these
# tuning keys ever leave the device or are accepted back from the drive.
SETTINGS_KEYS = (
    "SPEAKER_VOLUME",
    "MIC_SENSITIVITY",
    "INTERRUPTION_SENSITIVITY",
    "AUTO_INTERRUPT",
    "INTERACTIVE_INTERRUPT",
    "AUTOMATIC_LEDS_ON",
)

# Ability folders matching these patterns are parked copies, not live abilities.
SKIP_ABILITY_PATTERNS = tuple(
    p.strip() for p in (os.environ.get("OPENFILE_SKIP_PATTERNS") or
                        "*.retired*,*.disabled,*.bak,*.old").split(",") if p.strip()
)
VERSION = "0.3.0"
# Where the rest of OpenFile comes from when only this file arrived. OpenHome
# installs an ability's files onto the DevKit itself, but not the folders the
# installer needs, so the first "turn on drive" fetches this release.
RELEASE_ARCHIVE = os.environ.get("OPENFILE_RELEASE_ARCHIVE") or \
    f"https://github.com/Jmesmykil/OpenFile/archive/refs/tags/v{VERSION}.tar.gz"
SERVICES = ("openfile-sync", "openfile-web")
DISCOVERY_SERVICES = ("avahi-daemon", "wsdd2")

# Safety nets. Before a file on the device is replaced, the version that was
# running is kept here, off the volume, so that one command brings it back.
SNAPSHOT_DIR = os.environ.get("OPENFILE_SNAPSHOTS") or "/var/lib/openhome/openfile-snapshots"
SNAPSHOT_KEEP = int(os.environ.get("OPENFILE_SNAPSHOT_KEEP") or 30)
BACKUP_KEEP = int(os.environ.get("OPENFILE_BACKUP_KEEP") or 30)
LOW_SPACE_MB = 50

# OpenFile is the tool a user recovers with. Edits to its own folder are held
# back unless the owner has said otherwise in /etc/default/openfile.
SELF_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
ALLOW_SELF_EDIT = os.environ.get("OPENFILE_ALLOW_SELF_EDIT", "0") == "1"

# What each tuning key may hold. A value outside this is not applied.
ON_OFF = ("true", "false")
SETTINGS_RULES = {
    "SPEAKER_VOLUME": (0, 100),
    "MIC_SENSITIVITY": (0, 200),
    "INTERRUPTION_SENSITIVITY": (0, 100),
    "AUTO_INTERRUPT": ON_OFF,
    "INTERACTIVE_INTERRUPT": ON_OFF,
    "AUTOMATIC_LEDS_ON": ON_OFF,
}


def keep_newest(folder: pathlib.Path, keep: int) -> int:
    """Remove the oldest timestamped subfolders beyond `keep`. Returns how many went."""
    if not folder.is_dir():
        return 0
    stamped = sorted(d for d in folder.iterdir() if d.is_dir() and not d.is_symlink() and d.name[:8].isdigit())
    doomed = stamped[:-keep] if keep > 0 else []
    for d in doomed:
        if not os.path.ismount(d):
            shutil.rmtree(d, ignore_errors=True)
    return len(doomed)


def check_setting(key: str, value: str) -> str:
    """Return '' when the value is acceptable, otherwise what it should have been."""
    rule = SETTINGS_RULES.get(key)
    if rule is ON_OFF:
        return "" if value.lower() in ON_OFF else "true or false"
    low, high = rule
    try:
        return "" if low <= int(value) <= high else f"a whole number from {low} to {high}"
    except ValueError:
        return f"a whole number from {low} to {high}"



# ── Drive Scaffold ────────────────────────────────────────────────────────────

DRIVE_README = """# OpenFile

This volume is the modding surface of your OpenHome DevKit. Everything here
was copied from the device, and what you change here is copied back.

## Folders

- `abilities/`  One folder per ability. Edit a file, or drop in a new folder
                that contains `devkit_functions.py`. Python files are syntax
                checked before anything reaches the device. A file that does
                not parse is skipped and the reason lands in `logs/`.
- `sounds/`     The chimes and tones the device plays.
- `config/`     `settings.env` holds speaker, microphone and interrupt levels.
                `wifi.txt` moves the device to another network.
                `device_info.json` is a read-only report, rewritten each sync.
- `agents/`     Notes and prompt drafts. See the README inside.
- `logs/`       `sync_history.log` records every sync and every rejection.
- `backup/`     Copies of anything a reseed replaced.

## Voice commands

Start with "open file", then:

- "refresh"   pick up your edits now instead of waiting for the watcher
- "undo"      reverse your last change to the device
- "reseed"    copy the device's current abilities, sounds and settings back
              onto this volume (what it replaces goes to `backup/`)
- "restart"   restart the OpenFile services
- "status"    free space and ability count
- "ports"     what is plugged into the device
- "eject"     flush and unmount the volume

## Secrets

Your account key and broker password stay on the device. They are never
written to this volume, and `settings.env` ignores any key it does not list.
"""

AGENTS_README = """# agents/

Agent personalities are stored in your OpenHome account, not on the DevKit.
The device only keeps the id of the agent it starts with, which you can see
in `config/device_info.json`.

Use this folder for prompt drafts and notes you want to keep with the device.
Nothing in here is applied automatically. Edit the live personality in the
OpenHome dashboard.
"""

WIFI_HEADER = """# Move the DevKit to another Wi-Fi network.
# Fill in both lines and save. The password is erased from this file as soon
# as the device has joined the network.
#
# CAREFUL: once it joins, the DevKit leaves the network you are reaching it
# on, and this volume disconnects. If the join fails, it goes back to the
# network it was on. Leave PASSWORD empty to change nothing.
"""

ABILITIES_GUIDE = """BEFORE YOU EDIT AN ABILITY

You cannot break your DevKit from this folder in a way that one command does
not fix. Every time an edit of yours replaces a file on the device, the
version that was running is saved first.

  Made it worse?   Say "open file, undo", run `openfile undo` on the device,
                   or press Undo on the web page. The last change is reversed.
  Want a fresh copy of everything?   Say "open file, reseed".

What is checked for you:
  - A Python file that does not parse is never sent. The device keeps running
    what it had, and logs/sync_history.log says which line is wrong.
  - A config.json that is not valid JSON is never sent.

What is not checked: whether your code does what you meant. A file can parse
and still misbehave. That is what undo is for.

Files worth knowing:
  devkit_functions.py   runs on the device, as root. Edit freely, test often.
  requirements.txt      packages are installed into the device's system
                        Python. A bad pin can affect other abilities. The
                        package list from before each install is saved in
                        backup/, in pip-freeze.txt.
  main.py, config.json  run on OpenHome's side. Changing them here has no
                        effect until you run `openhome deploy`.
  openfile/             OpenFile itself. Edits here are held back, so that a
                        mistake cannot take away undo. See the README to
                        lift that.
"""

CONFIG_GUIDE = """BEFORE YOU EDIT A SETTING

settings.env    Six keys, listed in the file. A value outside its range is
                not applied, and the file is set back to what the device
                has. Each change can be reversed with "open file, undo".
wifi.txt        Moves the DevKit to another network. Read the note at the
                top of the file first: a successful join disconnects you.
device_info.json   A report. Editing it changes nothing.

Your account key and broker password are not on this volume, by design.
"""


def _read_text(path, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip().rstrip("\x00")
    except OSError:
        return default


def _run(cmd: list, timeout: float = 3) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_env_file(path) -> dict:
    """Parse KEY=VALUE lines, ignoring comments and blanks."""
    values = {}
    for line in _read_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_atomic(path, content: str) -> None:
    """Replace a file in one step, keeping its owner and mode."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if previous is not None:
            os.chmod(tmp, previous.st_mode & 0o7777)
            with contextlib.suppress(OSError):
                os.chown(tmp, previous.st_uid, previous.st_gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def active_wifi_ssid() -> str:
    for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1].strip()
    return ""


def network_interfaces() -> dict:
    """Return {name: {state, ip, mac}} for every non-loopback interface."""
    found = {}
    for line in _run(["ip", "-br", "addr"]).splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] == "lo":
            continue
        name = parts[0].split("@")[0]
        addresses = [p.split("/")[0] for p in parts[2:] if "." in p]
        found[name] = {
            "state": parts[1],
            "ip": addresses[0] if addresses else "",
            "mac": _read_text(f"/sys/class/net/{name}/address"),
        }
    return found


def bluetooth_address() -> str:
    for hci in sorted(glob.glob("/sys/class/bluetooth/hci*")):
        address = _read_text(os.path.join(hci, "address"))
        if address:
            return address
    for line in _run(["hciconfig"]).splitlines():
        if "BD Address:" in line:
            return line.split("BD Address:", 1)[1].split()[0]
    return ""


def ignore_for_copy(folder, names):
    """copytree filter. Symlinks are never followed: a link inside an ability
    folder could otherwise carry any file on the device onto the volume."""
    clutter = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".DS_Store", "*.bak",
                                     ".requirements.hash", ".env")(folder, names)
    links = {n for n in names if os.path.islink(os.path.join(folder, n))}
    return set(clutter) | links


def is_skipped_ability(name: str) -> bool:
    lowered = name.lower()
    return name.startswith(".") or any(fnmatch.fnmatch(lowered, p) for p in SKIP_ABILITY_PATTERNS)


def probe_device_info(root_path, device_home: str = DEVICE_HOME) -> dict:
    """Read the facts reported in config/device_info.json from the running system."""
    env = read_env_file(pathlib.Path(device_home) / ".env")
    uname = os.uname()
    size_mb = 0
    with contextlib.suppress(OSError):
        stat = os.statvfs(root_path)
        size_mb = round((stat.f_blocks * stat.f_frsize) / (1024 * 1024))
    return {
        "hostname": socket.gethostname(),
        "hardware": _read_text("/proc/device-tree/model") or uname.machine,
        "architecture": uname.machine,
        "kernel": uname.release,
        "firmware": env.get("FIRMWARE_VERSION", ""),
        "default_agent_id": env.get("DEFAULT_AGENT", ""),
        "wifi_ssid": active_wifi_ssid(),
        "interfaces": network_interfaces(),
        "storage_mount": str(root_path),
        "storage_size_mb": size_mb,
        "last_updated": datetime.datetime.now().isoformat(timespec="seconds"),
    }


def export_settings(config_dest: pathlib.Path, device_home: str, overwrite: bool = False) -> bool:
    """Write the tunable keys, and only those, from the device .env to settings.env."""
    # An earlier release copied the whole .env here. Remove that copy on sight.
    with contextlib.suppress(OSError):
        (config_dest / ".env").unlink()

    settings_file = config_dest / "settings.env"
    on_drive = read_env_file(settings_file)
    leaked = [k for k in on_drive if k not in SETTINGS_KEYS]
    if settings_file.exists() and not overwrite and not leaked:
        return False

    device = read_env_file(pathlib.Path(device_home) / ".env")
    lines = ["# DevKit tuning. Save the file and the device applies it.",
             "# Keys not listed here are ignored."]
    for key in SETTINGS_KEYS:
        # Keep an edit the user already made unless this is a reseed.
        value = device.get(key) if overwrite or key not in on_drive else on_drive[key]
        if value is not None:
            lines.append(f"{key}={value}")
    if len(lines) == 2:
        return False
    write_atomic(settings_file, "\n".join(lines) + "\n")
    return True


def _backup(existing: pathlib.Path, root_path: pathlib.Path, stamp: str) -> None:
    target = root_path / "backup" / stamp / existing.relative_to(root_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(existing), str(target))


def autopopulate_from_device(root_path: pathlib.Path, caps_dir: str = CAPS_DIR,
                             device_home: str = DEVICE_HOME, overwrite: bool = False,
                             already_seeded=()) -> dict:
    """Copy the device's abilities, sounds and tuning onto the drive.

    Existing files are left alone so a user's edits survive. With overwrite,
    the device wins and whatever it replaces is moved under backup/ first.
    """
    stats = {"abilities_copied": 0, "sounds_copied": 0, "settings_synced": False, "backed_up": 0}
    root_path = pathlib.Path(root_path)
    caps_path = pathlib.Path(caps_dir)
    home_path = pathlib.Path(device_home)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dirs = {name: root_path / name for name in ("abilities", "agents", "sounds", "config", "logs", "inbox")}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    ignore = ignore_for_copy
    if caps_path.is_dir():
        for item in sorted(caps_path.iterdir()):
            if not item.is_dir() or item.is_symlink() or is_skipped_ability(item.name):
                continue
            dest_dir = dirs["abilities"] / item.name
            try:
                if not overwrite and not dest_dir.exists() and item.name in already_seeded:
                    continue  # deleted on the volume on purpose. A reseed restores it.
                if dest_dir.exists():
                    if not overwrite:
                        continue
                    _backup(dest_dir, root_path, stamp)
                    stats["backed_up"] += 1
                shutil.copytree(item, dest_dir, ignore=ignore)
                stats["abilities_copied"] += 1
            except (OSError, shutil.Error) as e:
                print(f"Could not copy ability {item.name}: {e}", file=sys.stderr)

    audio_src = home_path / "openhome_devkit" / "audio_files"
    if audio_src.is_dir():
        for audio_file in sorted(audio_src.iterdir()):
            if not audio_file.is_file() or audio_file.suffix.lower() not in (".wav", ".mp3", ".ogg"):
                continue
            dest_file = dirs["sounds"] / audio_file.name
            if dest_file.exists() and not overwrite:
                continue
            try:
                shutil.copy2(audio_file, dest_file)
                stats["sounds_copied"] += 1
            except OSError as e:
                print(f"Could not copy sound {audio_file.name}: {e}", file=sys.stderr)

    try:
        stats["settings_synced"] = export_settings(dirs["config"], device_home, overwrite=overwrite)
    except OSError as e:
        print(f"Could not export settings: {e}", file=sys.stderr)

    wifi_file = dirs["config"] / "wifi.txt"
    if not wifi_file.exists():
        with contextlib.suppress(OSError):
            wifi_file.write_text(f"{WIFI_HEADER}SSID={active_wifi_ssid()}\nPASSWORD=\n", encoding="utf-8")

    with contextlib.suppress(OSError):
        write_atomic(dirs["config"] / "device_info.json",
                     json.dumps(probe_device_info(root_path, device_home), indent=2) + "\n")

    for path, text in ((root_path / "README.md", DRIVE_README), (dirs["agents"] / "README.md", AGENTS_README),
                       (dirs["abilities"] / "READ ME FIRST.txt", ABILITIES_GUIDE),
                       (dirs["config"] / "READ ME FIRST.txt", CONFIG_GUIDE)):
        if overwrite or not path.exists():
            with contextlib.suppress(OSError):
                path.write_text(text, encoding="utf-8")

    return stats


def scaffold_drive_tree(root_path: pathlib.Path, caps_dir: str = CAPS_DIR, device_home: str = DEVICE_HOME) -> None:
    """Create the drive folders and fill them from the device."""
    root_path = pathlib.Path(root_path)
    root_path.mkdir(parents=True, exist_ok=True)
    autopopulate_from_device(root_path, caps_dir=caps_dir, device_home=device_home)


@contextlib.contextmanager
def sync_lock(path: str = None, wait_seconds: float = 0):
    """Hold the cross-process sync lock. Yields False when someone else has it."""
    path = path or LOCK_FILE
    try:
        handle = open(path, "w")
    except OSError:
        # No writable lock location (a test sandbox, for one). Run unlocked.
        yield True
        return
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# ── Synchronization Engine ───────────────────────────────────────────────────

class DriveSyncEngine:
    def __init__(self, drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR, device_home=None):
        self.device_home = device_home or os.environ.get("OPENHOME_DEVICE_HOME") or DEVICE_HOME
        self.drive_path = pathlib.Path(drive_path)
        self.caps_dir = pathlib.Path(caps_dir)
        self.log_file = self.drive_path / "logs" / "sync_history.log"

    def log(self, message: str):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        print(entry, file=sys.stderr)
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass

    def play_sound(self, success=True):
        """Play a short chime through the desktop audio server, if one is reachable."""
        if not CHIMES_ENABLED:
            return
        try:
            name = "audio_complete.mp3" if success else "general_error.mp3"
            candidates = [
                os.path.join(str(self.drive_path), "sounds", name),
                CHIME_SUCCESS if success else CHIME_DECLINE,
            ]
            sound_file = next((c for c in candidates if os.path.exists(c)), None)
            player = shutil.which("pw-play") or shutil.which("paplay")
            if not sound_file or not player:
                return

            # The audio server belongs to the device account, not to root.
            owner = pathlib.Path(self.device_home).stat()
            runtime_dir = f"/run/user/{owner.st_uid}"
            cmd = [player, sound_file]
            if os.getuid() == 0 and owner.st_uid != 0 and os.path.isdir(runtime_dir):
                cmd = ["sudo", "-n", "-u", f"#{owner.st_uid}", f"XDG_RUNTIME_DIR={runtime_dir}"] + cmd
            subprocess.run(cmd, timeout=3, capture_output=True)
        except Exception:
            pass

    def validate_python_file(self, file_path: pathlib.Path) -> tuple[bool, str]:
        """Verify that a python file has valid syntax before copying."""
        try:
            content = file_path.read_text(encoding="utf-8")
            ast.parse(content, filename=file_path.name)
            return True, "Syntax valid"
        except SyntaxError as e:
            return False, f"SyntaxError at line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Failed to read/parse: {e}"

    def sync_abilities(self) -> dict:
        """Two-way sync of abilities between the drive and the device.

        A manifest remembers each file as it was after the last sync, which is
        what tells an edit made on the drive from an update made on the device
        (an `openhome deploy`, say). Whichever side changed is copied to the
        other. When both changed, the drive wins and the device's version is
        kept under backup/. With no history at all, the device wins and the
        drive's version is kept. Deleting a file on the drive never deletes
        it from the device. Removing one on the device retires the drive's
        copy to backup/, unless that copy was edited, in which case it is
        treated as new and sent.
        """
        abilities_src = self.drive_path / "abilities"
        if not abilities_src.exists():
            return {"synced": 0, "errors": ["abilities directory does not exist"]}

        self.caps_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.load_manifest()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        # Snapshots are named to the microsecond: two changes in one second must not share a folder.
        snap_stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        synced_count, errors = 0, []
        self.warnings, snapshot = [], {}

        names = {d.name for d in abilities_src.iterdir() if d.is_dir() and not d.is_symlink()}
        names |= {d.name for d in self.caps_dir.iterdir() if d.is_dir() and not d.is_symlink()}
        for name in sorted(n for n in names if not is_skipped_ability(n)):
            on_drive, on_device = abilities_src / name, self.caps_dir / name
            try:
                if on_drive.resolve() == on_device.resolve():
                    continue
            except OSError:
                pass
            drive_files, device_files = self.ability_files(on_drive), self.ability_files(on_device)
            known = manifest.setdefault(name, {})

            push, pull, conflicts, first_run, removed = [], [], [], [], []
            for rel in sorted(set(drive_files) | set(device_files)):
                drive_hash, device_hash, last = drive_files.get(rel), device_files.get(rel), known.get(rel)
                if drive_hash == device_hash:
                    known[rel] = drive_hash
                elif drive_hash is None:
                    if device_hash != last:  # new or updated on the device
                        pull.append(rel)
                elif device_hash is None and last is not None and drive_hash == last:
                    removed.append(rel)  # uninstalled on the device, untouched on the drive
                elif device_hash is None or device_hash == last:
                    push.append(rel)
                elif drive_hash == last:
                    pull.append(rel)
                elif last is None:
                    # No history for this file, as on a first run or an upgrade.
                    # The device is what is running, so it is the reference.
                    # The drive copy is kept, not lost.
                    first_run.append(rel)
                else:
                    conflicts.append(rel)

            # OpenFile's own folder: hold the edit back, keep it, show the running copy again.
            if name == SELF_NAME and not ALLOW_SELF_EDIT and (push or conflicts):
                for rel in push + conflicts:
                    kept = self.drive_path / "backup" / stamp / "held-back" / name / rel
                    kept.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(on_drive / rel), str(kept))
                    if rel in device_files:
                        pull.append(rel)
                self.warn(f"OpenFile's own files are protected, so {len(push) + len(conflicts)} edit(s) to "
                          f"'{name}' were not applied. Your versions are in backup/{stamp}/held-back/.")
                push, conflicts = [], []
                self.prune_empty(on_drive)

            # Nothing reaches the device unless every Python file parses and every JSON file loads.
            rejected = None
            for rel in push + conflicts:
                if rel.endswith(".py"):
                    valid, reason = self.validate_python_file(on_drive / rel)
                elif rel.endswith(".json"):
                    try:
                        json.loads((on_drive / rel).read_text(encoding="utf-8"))
                        valid, reason = True, ""
                    except (ValueError, UnicodeDecodeError) as e:
                        valid, reason = False, f"not valid JSON: {e}"
                else:
                    continue
                if not valid:
                    rejected = f"Rejected '{name}': {rel}: {reason}"
                    break
            if rejected:
                self.log(rejected)
                errors.append(rejected)
                push, conflicts = [], []

            for rel in conflicts:
                kept = self.drive_path / "backup" / stamp / "device-version" / name / rel
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(on_device / rel, kept)
                self.log(f"'{name}/{rel}' changed on both sides. Drive copy applied, device copy kept in backup/.")
            for rel in push + conflicts:
                if not self.stays_inside(on_device, on_device / rel):
                    self.log(f"Skipped '{name}/{rel}': that path leaves the ability folder through a link.")
                    continue
                self.keep_running_version(snapshot, snap_stamp, name, rel, on_device / rel)
                self.copy_to_device(on_drive / rel, on_device / rel)
                known[rel] = drive_files[rel]
            for rel in removed:
                kept = self.drive_path / "backup" / stamp / "removed-on-device" / name / rel
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(on_drive / rel), str(kept))
                known.pop(rel, None)
            if removed:
                self.log(f"'{name}': {len(removed)} file(s) were removed on the device. Drive copies moved to backup/.")
                self.prune_empty(on_drive)
            for rel in first_run:
                kept = self.drive_path / "backup" / stamp / "drive-version" / name / rel
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(on_drive / rel, kept)
                self.log(f"'{name}/{rel}' differed with no sync history. Device copy used, drive copy kept in backup/.")
            for rel in pull + first_run:
                target = on_drive / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(on_device / rel, target)
                known[rel] = device_files[rel]

            if push or conflicts:
                err_msg = self.install_requirements(on_device / "requirements.txt", name)
                if err_msg:
                    errors.append(err_msg)
            if not known:
                manifest.pop(name, None)
            if push or pull or conflicts or first_run or removed:
                synced_count += 1
                self.log(f"Synced ability: {name} ({len(push) + len(conflicts)} to device, "
                         f"{len(pull) + len(first_run)} to drive)")

        self.save_manifest(manifest)
        if snapshot:
            self.write_snapshot(snap_stamp, {"abilities": snapshot})
        return {"synced": synced_count, "errors": errors, "warnings": list(self.warnings)}

    # ── safety nets ───────────────────────────────────────────────────────────
    warnings = ()

    def warn(self, message: str) -> None:
        """Something the user should hear about: logged where the web page looks for it."""
        self.warnings = list(self.warnings) + [message]
        self.log(f"ATTENTION: {message}")

    def keep_running_version(self, snapshot: dict, stamp: str, name: str, rel: str, target: pathlib.Path) -> None:
        """Save the file that is about to be replaced on the device, or note that it is new."""
        record = snapshot.setdefault(name, {"changed": [], "created": []})
        if target.is_file():
            kept = pathlib.Path(SNAPSHOT_DIR) / stamp / "abilities" / name / rel
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, kept)
            record["changed"].append(rel)
        else:
            record["created"].append(rel)

    def write_snapshot(self, stamp: str, content: dict) -> None:
        folder = pathlib.Path(SNAPSHOT_DIR) / stamp
        try:
            folder.mkdir(parents=True, exist_ok=True)
            record_file = folder / "snapshot.json"
            record = json.loads(_read_text(record_file) or "{}")
            record.update(content)
            record.update({"stamp": stamp, "undone": False})
            write_atomic(record_file, json.dumps(record, indent=1, sort_keys=True))
            self.log(f"The versions that were running are saved as snapshot {stamp}. Undo reverses this change.")
            keep_newest(pathlib.Path(SNAPSHOT_DIR), SNAPSHOT_KEEP)
        except OSError as e:
            self.warn(f"Could not save a snapshot, so this change cannot be undone: {e}")

    def list_snapshots(self) -> list:
        """Newest first. Each entry says what an undo of it would reverse."""
        found = []
        root = pathlib.Path(SNAPSHOT_DIR)
        for folder in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []:
            try:
                record = json.loads(_read_text(folder / "snapshot.json"))
            except ValueError:
                continue
            abilities = record.get("abilities", {})
            found.append({
                "stamp": record.get("stamp", folder.name),
                "undone": bool(record.get("undone")),
                "abilities": {n: len(r["changed"]) + len(r["created"]) for n, r in abilities.items()},
                "settings": sorted(record.get("settings", {})),
            })
        return found

    def undo_last(self) -> dict:
        """Reverse the newest change that has not been reversed yet."""
        with sync_lock(wait_seconds=LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                return {"success": False, "error": "A sync is still running. Try again in a moment."}
            target = next((s for s in self.list_snapshots() if not s["undone"]), None)
            if target is None:
                return {"success": False, "error": "There is nothing to undo."}

            stamp = target["stamp"]
            folder = pathlib.Path(SNAPSHOT_DIR) / stamp
            record = json.loads(_read_text(folder / "snapshot.json"))
            manifest = self.load_manifest()
            now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            restored = removed = 0

            for name, changes in record.get("abilities", {}).items():
                on_device, on_drive = self.caps_dir / name, self.drive_path / "abilities" / name
                known = manifest.setdefault(name, {})
                for rel in changes["changed"]:
                    kept = folder / "abilities" / name / rel
                    if not kept.is_file() or not self.stays_inside(on_device, on_device / rel):
                        continue
                    self.copy_to_device(kept, on_device / rel)
                    (on_drive / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(kept, on_drive / rel)
                    known[rel] = hashlib.sha256(kept.read_bytes()).hexdigest()
                    restored += 1
                for rel in changes["created"]:
                    # The change added this file. Undo takes it off the device and keeps it in backup/.
                    for side in (on_drive / rel, on_device / rel):
                        if side.is_file() and not side.is_symlink():
                            aside = self.drive_path / "backup" / now / "undone" / name / rel
                            aside.parent.mkdir(parents=True, exist_ok=True)
                            if aside.exists():
                                side.unlink()
                            else:
                                shutil.move(str(side), str(aside))
                    known.pop(rel, None)
                    removed += 1
                self.prune_empty(on_drive)
                self.prune_empty(on_device)
                if not known:
                    manifest.pop(name, None)

            settings = record.get("settings", {})
            if settings:
                self.apply_settings(settings)
                export_settings(self.drive_path / "config", str(self.device_home), overwrite=True)

            self.save_manifest(manifest)
            record["undone"] = True
            write_atomic(folder / "snapshot.json", json.dumps(record, indent=1, sort_keys=True))
            self.log(f"Undid snapshot {stamp}: {restored} file(s) restored, {removed} added file(s) set aside, "
                     f"{len(settings)} setting(s) restored.")
            return {"success": True, "stamp": stamp, "restored": restored, "removed": removed,
                    "settings": sorted(settings), "abilities": sorted(record.get("abilities", {}))}

    @staticmethod
    def ability_files(folder: pathlib.Path) -> dict:
        """Relative path to sha256 for every file of an ability that is worth syncing."""
        found = {}
        if not folder.is_dir():
            return found
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
            for filename in sorted(files):
                if filename.startswith(".") or filename.endswith((".pyc", ".bak", ".tmp", ".swp")):
                    continue
                path = pathlib.Path(root) / filename
                if path.is_symlink():
                    continue
                with contextlib.suppress(OSError):
                    found[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found

    @staticmethod
    def prune_empty(folder: pathlib.Path) -> None:
        """Remove folders left empty once their files have been moved to backup/."""
        if not folder.is_dir():
            return
        for root, _, _ in os.walk(folder, topdown=False):
            # rmdir only succeeds on a folder that is empty right now, which is
            # the question. The lists from os.walk were taken before its
            # children were removed.
            with contextlib.suppress(OSError):
                os.rmdir(root)

    @staticmethod
    def stays_inside(folder: pathlib.Path, target: pathlib.Path) -> bool:
        """True when writing target cannot land outside folder, links resolved."""
        root = folder.resolve()
        parent = target.parent.resolve()
        return (parent == root or root in parent.parents) and not target.is_symlink()

    def load_manifest(self) -> dict:
        try:
            data = json.loads(_read_text(STATE_FILE) or "{}")
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def save_manifest(self, manifest: dict) -> None:
        try:
            write_atomic(STATE_FILE, json.dumps(manifest, indent=1, sort_keys=True))
        except OSError as e:
            self.log(f"Could not save the sync manifest: {e}")

    def copy_to_device(self, src: pathlib.Path, dest: pathlib.Path) -> None:
        """Copy a file into the live abilities, owned by whoever owns that folder."""
        owner = self.caps_dir.stat()
        missing = [p for p in [dest.parent, *dest.parent.parents] if not p.exists()]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        for path in [dest, *missing]:
            with contextlib.suppress(OSError):
                os.chown(path, owner.st_uid, owner.st_gid)

    def install_requirements(self, req_file: pathlib.Path, ability: str) -> str:
        """Install an ability's pip requirements when they change. Returns an error or ''."""
        req_file = pathlib.Path(req_file)
        stamp_file = req_file.parent / ".requirements.hash"
        try:
            wanted = [l for l in _read_text(req_file).splitlines() if l.strip() and not l.strip().startswith("#")]
            if not wanted:
                return ""
            curr_hash = hashlib.sha256(req_file.read_bytes()).hexdigest()
            if curr_hash == _read_text(stamp_file):
                return ""
            if not PIP_ENABLED:
                self.log(f"Requirements for '{ability}' changed. Package installs are turned off, so nothing was installed.")
                return ""
            frozen = _run([sys.executable, "-m", "pip", "freeze"], timeout=60)
            if frozen:
                stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                kept = self.drive_path / "backup" / stamp / "pip-freeze.txt"
                kept.parent.mkdir(parents=True, exist_ok=True)
                kept.write_text(f"# Packages on the device before '{ability}' had its requirements installed.\n{frozen}")
            self.log(f"Installing requirements for '{ability}'...")
            # Abilities run under the system interpreter, so that is where packages go.
            res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--break-system-packages"],
                capture_output=True, text=True, timeout=300
            )
            if res.returncode != 0:
                err_msg = f"Dependency error for '{ability}': {res.stderr.strip()[-400:]}"
                self.log(err_msg)
                return err_msg
            # Stamp only a successful install, so a failure is retried next sync.
            stamp_file.write_text(curr_hash)
        except Exception as e:
            err_msg = f"Requirements installation error for '{ability}': {e}"
            self.log(err_msg)
            return err_msg
        return ""

    def sync_settings(self, settings_src=None) -> bool:
        """Apply the tunable keys from settings.env to the device .env, in place."""
        settings_src = pathlib.Path(settings_src) if settings_src else self.drive_path / "config" / "settings.env"
        env_dest = pathlib.Path(self.device_home) / ".env"
        if not settings_src.exists():
            return False

        try:
            offered = read_env_file(settings_src)
            ignored = sorted(k for k in offered if k not in SETTINGS_KEYS)
            if ignored:
                self.log(f"Ignored keys that are not device tuning: {', '.join(ignored)}")
            existing = read_env_file(env_dest)
            updates, refused = {}, []
            for key, value in offered.items():
                if key not in SETTINGS_KEYS or existing.get(key) == value:
                    continue
                expected = check_setting(key, value)
                if expected:
                    refused.append(f"{key}={value} (it must be {expected})")
                else:
                    updates[key] = value
            if refused:
                self.warn(f"Not applied: {'; '.join(refused)}. settings.env was set back to what the device has.")
                export_settings(settings_src.parent, str(self.device_home), overwrite=True)
            if not updates:
                return False

            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            self.write_snapshot(stamp, {"settings": {k: existing[k] for k in updates if k in existing}})
            self.apply_settings(updates)
            self.log(f"Updated device settings: {', '.join(sorted(updates))}")
            return True
        except Exception as e:
            self.log(f"Error updating settings: {e}")
        return False

    def housekeeping(self) -> list:
        """Trim old backups and say so when the volume is filling up."""
        notes = []
        self.sweep_desktop_scraps()
        keep_newest(self.drive_path / "backup", BACKUP_KEEP)
        with contextlib.suppress(OSError):
            stat = os.statvfs(self.drive_path)
            free_mb = (stat.f_bavail * stat.f_frsize) // (1024 * 1024)
            if free_mb < LOW_SPACE_MB:
                keep_newest(self.drive_path / "backup", max(3, BACKUP_KEEP // 5))
                notes.append(f"The volume has {free_mb} MB free. Older backups were removed to make room.")
                self.log(f"ATTENTION: {notes[-1]}")
        return notes

    def sweep_desktop_scraps(self, older_than: float = 120) -> int:
        """Remove the scrap files desktops leave beside real ones: ._name, .DS_Store, Thumbs.db.

        They hold icon positions and download flags, nothing OpenFile or the
        device uses. Fresh ones are left alone in case a copy is still going.
        """
        removed, now = 0, time.time()
        for root, dirs, files in os.walk(self.drive_path):
            dirs[:] = [d for d in dirs if d not in (".streams", "backup") and not os.path.islink(os.path.join(root, d))]
            for name in files:
                if not (name.startswith("._") or name in (".DS_Store", "Thumbs.db", "desktop.ini")):
                    continue
                path = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    if not os.path.islink(path) and now - os.path.getmtime(path) >= older_than:
                        os.remove(path)
                        removed += 1
        return removed

    def apply_settings(self, updates: dict) -> None:
        """Rewrite only the lines that change. Comments, order and every other key stay as they were."""
        env_dest = pathlib.Path(self.device_home) / ".env"
        lines = _read_text(env_dest).splitlines() if env_dest.exists() else []
        pending = {k: v for k, v in updates.items() if k in SETTINGS_KEYS}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                lines[i] = f"{key}={pending.pop(key)}"
        lines.extend(f"{k}={v}" for k, v in pending.items())
        write_atomic(env_dest, "\n".join(lines) + "\n")

    def sync_wifi(self, wifi_file=None) -> tuple[bool, str]:
        """Join the network named in wifi.txt, then erase the password from the file."""
        wifi_file = pathlib.Path(wifi_file) if wifi_file else self.drive_path / "config" / "wifi.txt"
        if not wifi_file.exists():
            return False, "No wifi.txt found"

        creds = read_env_file(wifi_file)
        ssid, password = creds.get("SSID", ""), creds.get("PASSWORD", "")
        if not ssid:
            return False, "No network name given"
        if active_wifi_ssid() == ssid and not password:
            return True, "Already on the requested network"
        if not password:
            return False, "Password needed to change network"
        if not shutil.which("nmcli"):
            return False, "nmcli utility not installed"

        previous = next((line.split(":", 1)[0] for line in
                         _run(["nmcli", "-t", "-f", "NAME,TYPE", "con", "show", "--active"]).splitlines()
                         if line.endswith("wireless")), "")
        self.log("Joining the Wi-Fi network named in wifi.txt...")
        res = subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, "password", password],
                             capture_output=True, text=True, timeout=45)
        # The volume is readable by anyone who can reach it. Never leave a
        # password sitting on it, whether or not the join worked.
        with contextlib.suppress(OSError):
            write_atomic(wifi_file, f"{WIFI_HEADER}SSID={ssid}\nPASSWORD=\n")
        if res.returncode == 0:
            self.log("Wi-Fi network joined.")
            return True, "Joined the requested network"
        self.warn(f"Could not join the Wi-Fi network in wifi.txt: {res.stderr.strip()[-160:]}")
        if previous:
            back = subprocess.run(["nmcli", "con", "up", "id", previous], capture_output=True, text=True, timeout=45)
            self.log("Back on the previous Wi-Fi network." if back.returncode == 0
                     else f"Could not return to the previous network: {back.stderr.strip()[-160:]}")
        return False, "Could not join the requested network"

    def run_full_sync(self, reseed: bool = False, wait_seconds: float = None) -> dict:
        """One pass: device to drive, then drive to device. Serialised across processes.

        A pass that finds another one running waits for its turn, so a
        request made in that moment is carried out, not dropped.
        """
        wait_seconds = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
        with sync_lock(wait_seconds=wait_seconds) as acquired:
            if not acquired:
                self.log(f"Another sync was still running after {wait_seconds:.0f}s. Skipped.")
                return {"success": True, "skipped": True, "abilities_synced": 0, "errors": [],
                        "settings_updated": False, "wifi_status": "", "reseeded": False}

            self.log("Reseeding drive from device..." if reseed else "Beginning drive synchronization pass...")
            seeded = autopopulate_from_device(self.drive_path, caps_dir=str(self.caps_dir),
                                              device_home=str(self.device_home), overwrite=reseed,
                                              already_seeded=set(self.load_manifest()))
            ability_res = self.sync_abilities()
            warnings = list(ability_res.get("warnings", []))
            self.warnings = []
            # After a reseed the drive already matches the device, so there is nothing to apply.
            settings_changed = False if reseed else self.sync_settings()
            wifi_connected, wifi_msg = self.sync_wifi()

            success = len(ability_res["errors"]) == 0
            if ability_res["synced"] or ability_res["errors"] or reseed:
                self.play_sound(success=success)

            summary = {
                "success": success,
                "skipped": False,
                "abilities_synced": ability_res["synced"],
                "errors": ability_res["errors"],
                "settings_updated": settings_changed,
                "wifi_status": wifi_msg,
                "reseeded": reseed,
                "seeded": seeded,
                "warnings": warnings + list(self.warnings) + self.housekeeping(),
            }
            self.log(f"Sync complete: {summary}")
            return summary

    def detect_external_usb_devices(self) -> list[dict]:
        """Detect external USB storage block devices on physical USB-A ports."""
        found = []
        if not USB_SCAN_ENABLED:
            return found
        for dev_path in sorted(glob.glob("/sys/block/sd*")):
            dev_name = os.path.basename(dev_path)
            model = ""
            try:
                with open(f"{dev_path}/device/model", "r", encoding="utf-8", errors="ignore") as f:
                    model = f.read().strip()
            except Exception:
                pass
            size_mb = 0
            try:
                with open(f"{dev_path}/size", "r") as f:
                    size_mb = (int(f.read().strip()) * 512) // (1024 * 1024)
            except Exception:
                pass

            # Detect partitions
            partitions = []
            for part in sorted(glob.glob(f"/sys/block/{dev_name}/{dev_name}*")):
                part_name = os.path.basename(part)
                if part_name != dev_name:
                    partitions.append(f"/dev/{part_name}")
            if not partitions:
                partitions = [f"/dev/{dev_name}"]

            found.append({
                "device": f"/dev/{dev_name}",
                "model": model or "External USB Drive",
                "size_mb": size_mb,
                "partitions": partitions
            })
        return found

    def sync_external_usb(self, device_path=None, target_mount=USB_EXTERNAL_MOUNT) -> dict:
        """Mount a flash drive plugged into a USB-A port and exchange files with it."""
        if not USB_SCAN_ENABLED:
            return {"success": False, "error": "Flash drive handling is turned off"}
        drives = self.detect_external_usb_devices()
        if not drives:
            return {"success": False, "error": "No USB storage device is plugged in"}

        selected = next((d for d in drives
                         if device_path and (d["device"] == device_path or device_path in d["partitions"])),
                        drives[0])
        part_to_mount = selected["partitions"][0]
        mount_dir = pathlib.Path(target_mount)
        mount_dir.mkdir(parents=True, exist_ok=True)

        if not any(line.split()[1:2] == [str(mount_dir)] for line in _read_text("/proc/mounts").splitlines()):
            res = subprocess.run(["mount", part_to_mount, str(mount_dir)], capture_output=True, text=True)
            if res.returncode != 0:
                self.log(f"Failed to mount {part_to_mount}: {res.stderr.strip()}")
                return {"success": False, "error": f"Mount failed: {res.stderr.strip()}"}

        with sync_lock(wait_seconds=30) as acquired:
            if not acquired:
                return {"success": False, "error": "Another sync is still running"}
            self.log(f"Flash drive {part_to_mount} mounted at {mount_dir}.")
            result = self.ingest_usb_tree(mount_dir)
            self.write_usb_backup(mount_dir)

        with contextlib.suppress(OSError):
            os.sync()
        self.play_sound(success=not result["errors"])
        return {"success": True, "device": part_to_mount, "mountpoint": str(mount_dir), **result}

    def ingest_usb_tree(self, mount_dir: pathlib.Path) -> dict:
        """Stage abilities found on a flash drive onto the OpenFile volume, then sync once.

        Everything goes through the same validated path as an edit made on the
        volume itself, so a flash drive gets no shortcut past the syntax check.
        """
        mount_dir = pathlib.Path(mount_dir)
        candidates = []
        for holder in (mount_dir / "abilities", mount_dir):
            if holder.is_dir():
                candidates.extend(d for d in sorted(holder.iterdir())
                                  if d.is_dir() and not is_skipped_ability(d.name)
                                  and d.name != "OPENFILE_BACKUP"
                                  and (d / "devkit_functions.py").exists())

        staged, errors = [], []
        for src_dir in candidates:
            valid, reason = self.validate_python_file(src_dir / "devkit_functions.py")
            if not valid:
                errors.append(f"Rejected '{src_dir.name}' from flash drive: {reason}")
                self.log(errors[-1])
                continue
            target = self.drive_path / "abilities" / src_dir.name
            target.mkdir(parents=True, exist_ok=True)
            for f in sorted(src_dir.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    shutil.copyfile(f, target / f.name)
            staged.append(src_dir.name)

        ability_res = self.sync_abilities() if staged else {"synced": 0, "errors": []}
        errors.extend(ability_res["errors"])

        for name, apply in (("settings.env", self.sync_settings), ("wifi.txt", self.sync_wifi)):
            if (mount_dir / name).exists():
                self.log(f"Flash drive {name}: {apply(mount_dir / name)}")

        return {"abilities_synced": staged, "errors": errors}

    def write_usb_backup(self, mount_dir: pathlib.Path) -> None:
        """Leave a copy of the device's abilities on the flash drive."""
        backup_dir = pathlib.Path(mount_dir) / "OPENFILE_BACKUP"
        if not os.access(mount_dir, os.W_OK):
            self.log("The flash drive is read-only, so no backup was written to it.")
            return
        try:
            ignore = ignore_for_copy
            if self.caps_dir.exists():
                for d in sorted(self.caps_dir.iterdir()):
                    if d.is_dir() and not d.is_symlink() and not is_skipped_ability(d.name):
                        shutil.copytree(d, backup_dir / "abilities" / d.name, ignore=ignore, dirs_exist_ok=True)
            (backup_dir / "README.txt").write_text(
                "Abilities copied from an OpenHome DevKit by OpenFile.\n\n"
                "To install or change an ability, put its folder in abilities/ on this\n"
                "drive, with a devkit_functions.py inside, and plug the drive back into\n"
                "any USB-A port on the DevKit.\n", encoding="utf-8")
            if self.log_file.exists():
                shutil.copyfile(self.log_file, backup_dir / "sync_history.log")
        except (OSError, shutil.Error) as e:
            self.log(f"Could not write the flash drive backup: {e}")

    def eject_external_usb(self, target_mount=USB_EXTERNAL_MOUNT) -> dict:
        """Flush writes and unmount external USB-A drive."""
        try:
            os.sync()
        except Exception:
            pass

        mount_dir = pathlib.Path(target_mount)
        if not mount_dir.exists():
            return {"success": True, "message": "No USB mount directory found"}

        res = subprocess.run(["umount", str(mount_dir)], capture_output=True, text=True)
        if res.returncode == 0:
            self.log(f"Flash drive at {mount_dir} unmounted.")
            return {"success": True, "message": f"USB drive at {mount_dir} safely ejected."}
        else:
            if "not mounted" in res.stderr.lower():
                return {"success": True, "message": "Drive was not mounted."}
            self.log(f"Failed to unmount {mount_dir}: {res.stderr}")
            return {"success": False, "error": f"Failed to unmount: {res.stderr}"}



# ── Hardware Actions & JSON Output ───────────────────────────────────────────

def _emit_success(spoken: str, data: dict = None):
    print(json.dumps({
        "success": True,
        "spoken_response": spoken,
        "data": data or {},
        "error": None
    }))


def _emit_error(code: str, message: str, spoken: str = ""):
    print(json.dumps({
        "success": False,
        "spoken_response": spoken or f"Error: {message}",
        "data": {},
        "error": {"code": code, "message": message}
    }))


def is_drive_mounted() -> bool:
    if PLAIN_DIR:
        return os.path.isdir(DRIVE_MOUNT)
    try:
        res = subprocess.run(["mountpoint", "-q", DRIVE_MOUNT], capture_output=True)
        return res.returncode == 0
    except Exception:
        return os.path.exists(os.path.join(DRIVE_MOUNT, "abilities"))


def device_account() -> tuple[int, int]:
    """uid and gid of the account that owns the DevKit home directory."""
    try:
        st = os.stat(DEVICE_HOME)
        return st.st_uid, st.st_gid
    except OSError:
        return 0, 0


def mount_volume() -> None:
    """Create the backing image if needed and mount it for the device account."""
    os.makedirs(DRIVE_MOUNT, exist_ok=True)
    if PLAIN_DIR:
        return
    os.makedirs(os.path.dirname(DRIVE_IMG), exist_ok=True)
    if not os.path.exists(DRIVE_IMG):
        subprocess.run(["truncate", "-s", f"{IMG_SIZE_MB}M", DRIVE_IMG], check=True, capture_output=True)
        subprocess.run(["mkfs.vfat", "-F", "32", "-n", "OPENFILE", DRIVE_IMG], check=True, capture_output=True)
    if not is_drive_mounted():
        uid, gid = device_account()
        subprocess.run(["mount", "-o", f"loop,rw,uid={uid},gid={gid},umask=0002", DRIVE_IMG, DRIVE_MOUNT],
                       check=True, capture_output=True)


def is_installed() -> bool:
    return os.path.exists(DEFAULTS_FILE)


def ability_dir() -> pathlib.Path:
    return pathlib.Path(os.path.dirname(os.path.abspath(__file__)))


def safe_extract(archive: pathlib.Path, into: pathlib.Path) -> int:
    """Extract a release archive into the ability folder, minus its top-level folder.

    Refuses links and any member whose path would leave the folder.
    """
    import tarfile
    count = 0
    root = into.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            parts = pathlib.PurePosixPath(member.name).parts
            if len(parts) < 2 or ".." in parts:
                continue
            rel = pathlib.PurePosixPath(*parts[1:])
            target = (root / rel).resolve()
            if target != root and root not in target.parents:
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                if member.mode & 0o111:
                    os.chmod(target, 0o755)
                count += 1
    return count


def fetch_release(into: pathlib.Path) -> int:
    """Download this version's release and extract it beside this file."""
    import urllib.request
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        with urllib.request.urlopen(RELEASE_ARCHIVE, timeout=60) as response:
            shutil.copyfileobj(response, tmp)
        path = tmp.name
    try:
        return safe_extract(pathlib.Path(path), into)
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


def first_time_setup() -> str:
    """Run once on a DevKit that got OpenFile through OpenHome rather than make install.

    Returns a spoken sentence, or '' when there was nothing to do.
    """
    if is_installed():
        return ""
    folder = ability_dir()
    installer = folder / "install.sh"
    if not installer.exists():
        try:
            fetched = fetch_release(folder)
        except Exception as e:
            return f"Open file could not fetch its files. Check the device's internet connection. {e}"
        if not installer.exists():
            return f"Open file fetched {fetched} files but the installer was not among them."
    os.chmod(installer, 0o755)
    subprocess.Popen(["/bin/bash", str(installer)], cwd=str(folder), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "Setting open file up for the first time. Give it a minute, then say open file, status."


def enable_drive(*_):
    """Mount the volume and fill it from the device."""
    setup = first_time_setup()
    if setup:
        _emit_success(setup, {"first_time_setup": True})
        return
    try:
        mount_volume()
        scaffold_drive_tree(pathlib.Path(DRIVE_MOUNT), caps_dir=CAPS_DIR, device_home=DEVICE_HOME)

        _emit_success(
            "Open file is on. Open it from your computer, the web page, or a flash drive.",
            {"mounted": True, "path": DRIVE_MOUNT, "image": DRIVE_IMG}
        )
    except Exception as e:
        _emit_error("enable_failed", str(e), "Open file could not turn the drive on.")


def disable_drive(*_):
    """Apply pending edits, then unmount."""
    try:
        if is_drive_mounted():
            DriveSyncEngine(drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR).run_full_sync()
        subprocess.run(["sync"], check=False)

        if is_drive_mounted() and not PLAIN_DIR:
            res = subprocess.run(["umount", DRIVE_MOUNT], capture_output=True, text=True)
            if res.returncode != 0:
                _emit_error("busy", res.stderr.strip(),
                            "The drive is still in use. Close any open files and try again.")
                return

        _emit_success("The drive is unmounted. It is safe to disconnect.", {"mounted": False})
    except Exception as e:
        _emit_error("disable_failed", str(e), "Open file could not unmount the drive.")


CALL_BUDGET_SECONDS = float(os.environ.get("OPENFILE_CALL_BUDGET") or 10)


def _sync_and_report(reseed: bool):
    """Run a sync in a detached child and report it, or report that it is still going.

    OpenHome ends a capability call at 15 seconds and kills the process. A
    sync that has to install packages or join a network can take longer, so
    the work runs in its own session, where that kill cannot reach it.
    """
    if not is_drive_mounted():
        _emit_error("not_mounted", "Drive is not mounted", "The drive is off. Say open file, turn on drive.")
        return
    try:
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_sync_worker", "reseed" if reseed else "sync"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, _ = child.communicate(timeout=CALL_BUDGET_SECONDS)
        except subprocess.TimeoutExpired:
            _emit_success("Still working on it. The sync will finish on its own, and the log will have the result.",
                          {"running": True, "pid": child.pid})
            return
        result = json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        _emit_error("sync_failed", str(e), "The sync did not finish.")
        return
    _report_sync(result, reseed)


def _sync_worker(mode: str = "sync", *_):
    """Child of _sync_and_report. Prints the raw sync summary as one JSON line."""
    result = DriveSyncEngine(drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR).run_full_sync(reseed=mode == "reseed")
    print(json.dumps(result))


def _report_sync(result: dict, reseed: bool):
    try:
        if result.get("skipped"):
            _emit_success("A sync is already running.", result)
            return

        count, errors = result["abilities_synced"], result["errors"]
        if reseed:
            seeded = result["seeded"]
            parts = [f"Reseeded the drive with {seeded['abilities_copied']} abilities "
                     f"and {seeded['sounds_copied']} sounds from the device."]
            if seeded["backed_up"]:
                parts.append("Your previous copies are in the backup folder.")
        else:
            parts = [f"{count} {'ability' if count == 1 else 'abilities'} updated." if count
                     else "Everything is already up to date."]
            if result["settings_updated"]:
                parts.append("Device settings applied.")
        if errors:
            parts.append(f"{len(errors)} {'file was' if len(errors) == 1 else 'files were'} rejected. "
                         "The reason is in the logs folder.")
        held = result.get("warnings") or []
        if held:
            parts.append(f"{len(held)} {'change was' if len(held) == 1 else 'changes were'} held back. "
                         "The log says why.")
        _emit_success(" ".join(parts), result)
    except Exception as e:
        _emit_error("sync_failed", str(e), "The sync did not finish.")


def sync_drive(*_):
    """Apply edits made on the volume to the device."""
    _sync_and_report(reseed=False)


def reseed_drive(*_):
    """Rebuild the volume from the device. Replaced files move to backup/."""
    _sync_and_report(reseed=True)


def undo_last_change(*_):
    """Reverse the most recent change OpenFile made to the device."""
    try:
        result = DriveSyncEngine(drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR).undo_last()
        if not result["success"]:
            _emit_error("nothing_to_undo", result["error"], result["error"])
            return
        what = result["abilities"] + (["settings"] if result["settings"] else [])
        _emit_success(f"Undone. {' and '.join(what)} {'is' if len(what) == 1 else 'are'} back to the previous version.",
                      result)
    except Exception as e:
        _emit_error("undo_failed", str(e), "Open file could not undo that.")


def show_history(*_):
    """List the changes that can be reversed, newest first."""
    try:
        history = DriveSyncEngine(drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR).list_snapshots()
        pending = [h for h in history if not h["undone"]]
        spoken = (f"{len(pending)} {'change' if len(pending) == 1 else 'changes'} can be undone."
                  if pending else "There is nothing to undo.")
        _emit_success(spoken, {"history": history[:15]})
    except Exception as e:
        _emit_error("history_failed", str(e))


def installed_services() -> list:
    """The OpenFile services this device has, plus the discovery daemons. The web page is optional."""
    ours = [name for name in SERVICES if os.path.exists(f"/etc/systemd/system/{name}.service")]
    return ours + [name for name in DISCOVERY_SERVICES if _service_active(name)]


def restart_services(*_):
    """Restart the OpenFile daemons. Detached, so the reply is sent before they cycle."""
    if not shutil.which("systemctl"):
        _emit_error("no_systemd", "systemctl not found", "This device cannot restart services.")
        return
    try:
        installed = installed_services()
        subprocess.Popen(["systemd-run", "--quiet", "--collect", "--on-active=2",
                          "systemctl", "restart", *installed],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _emit_success("Restarting open file. Give it a few seconds.", {"services": installed})
    except Exception as e:
        _emit_error("restart_failed", str(e), "Open file could not restart its services.")


def get_status_dict() -> dict:
    """Return dictionary of current OpenFile subsystem status."""
    free_mb = total_mb = 0
    if is_drive_mounted():
        with contextlib.suppress(OSError):
            stat = os.statvfs(DRIVE_MOUNT)
            free_mb = round((stat.f_bavail * stat.f_frsize) / (1024 * 1024))
            total_mb = round((stat.f_blocks * stat.f_frsize) / (1024 * 1024))

    ability_dir = pathlib.Path(DRIVE_MOUNT) / "abilities"
    abilities = sorted(d.name for d in ability_dir.iterdir()
                       if d.is_dir() and not d.name.startswith(".")) if ability_dir.is_dir() else []

    return {
        "mounted": is_drive_mounted(),
        "watcher_active": is_watcher_running(),
        "free_mb": free_mb,
        "total_mb": total_mb,
        "abilities": abilities,
        "transports": transport_status(),
    }


def get_status(*_):
    """Report storage space, connection state, and loaded abilities."""
    try:
        data = get_status_dict()
        count = len(data["abilities"])
        spoken = (f"The drive has {data['free_mb']} megabytes free of {data['total_mb']}, "
                  f"with {count} {'ability' if count == 1 else 'abilities'} on it.")
        if not data["mounted"]:
            spoken = "The drive is off."
        _emit_success(spoken, data)
    except Exception as e:
        _emit_error("status_failed", str(e))


def _service_active(name: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", name],
                              capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def transport_status() -> dict:
    """Which ways into the volume are up right now."""
    return {
        "network_share": _service_active("smbd"),
        "web_portal": _service_active("openfile-web"),
        "flash_drive": bool(DriveSyncEngine().detect_external_usb_devices()),
    }


def get_hardware_ports() -> dict:
    """Report every port on the DevKit from what the kernel exposes right now."""
    usb_devices = []
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        product = _read_text(os.path.join(dev, "product"))
        if product and not os.path.basename(dev).startswith("usb"):
            usb_devices.append({
                "port": os.path.basename(dev),
                "product": product,
                "speed_mbps": _read_text(os.path.join(dev, "speed")),
            })

    displays = {os.path.basename(d): _read_text(os.path.join(d, "status"), "unknown")
                for d in sorted(glob.glob("/sys/class/drm/card*-*"))}

    sound_cards = [line.split("]:", 1)[1].strip()
                   for line in _read_text("/proc/asound/cards").splitlines() if "]:" in line]

    mounted_usb = any(line.split()[1:2] == [USB_EXTERNAL_MOUNT]
                      for line in _read_text("/proc/mounts").splitlines())

    return {
        "board": _read_text("/proc/device-tree/model") or os.uname().machine,
        "usb_c": {"role": "power input"},
        "usb_a": {
            "storage": DriveSyncEngine().detect_external_usb_devices(),
            "devices": usb_devices,
            "mountpoint": USB_EXTERNAL_MOUNT if mounted_usb else None,
        },
        "network": network_interfaces(),
        "bluetooth": {"address": bluetooth_address()},
        "video": displays,
        "audio": sound_cards,
        "transports": transport_status(),
    }


def get_ports(*_):
    """Query and output all hardware ports on the DevKit."""
    try:
        ports = get_hardware_ports()
        drives = len(ports["usb_a"]["storage"])
        online = [name for name, info in ports["network"].items() if info["ip"]]
        spoken = (f"{drives} flash {'drive is' if drives == 1 else 'drives are'} plugged in. "
                  f"Network is up on {' and '.join(online) if online else 'nothing'}. "
                  "The USB C port is power.")
        _emit_success(spoken, ports)
    except Exception as e:
        _emit_error("ports_query_failed", str(e))


def sync_usb(*_):
    """Exchange files with a flash drive in a USB-A port."""
    try:
        result = DriveSyncEngine().sync_external_usb()
        if not result.get("success"):
            _emit_error("usb_sync_failed", result.get("error", "Failed"), "No flash drive could be read.")
            return
        count = len(result.get("abilities_synced", []))
        _emit_success(f"Flash drive read. {count} {'ability' if count == 1 else 'abilities'} installed.", result)
    except Exception as e:
        _emit_error("usb_sync_failed", str(e))


def eject_usb(*_):
    """Unmount the flash drive."""
    try:
        result = DriveSyncEngine().eject_external_usb()
        if result.get("success"):
            _emit_success("The flash drive is safe to remove.", result)
        else:
            _emit_error("usb_eject_failed", result.get("error", "Failed"), "The flash drive is still busy.")
    except Exception as e:
        _emit_error("usb_eject_failed", str(e))


def load_web_portal():
    """Import device/web_portal.py, which sits beside this file."""
    device_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device")
    if device_dir not in sys.path:
        sys.path.insert(0, device_dir)
    import web_portal
    return web_portal


def web_portal(*args):
    """Run the OpenFile web page."""
    wp = load_web_portal()
    wp.run_web_portal(int(args[0]) if args else wp.PORT)


def is_watcher_running() -> bool:
    """Check if the background auto-sync watcher daemon is active."""
    try:
        os.kill(int(_read_text(PID_FILE)), 0)
        return True
    except (OSError, ValueError):
        return _service_active("openfile-sync")


class DriveWatcher:
    """Monitors OPENFILE drive for changes and automatically executes debounced sync."""

    def __init__(self, drive_path=DRIVE_MOUNT, caps_dir=CAPS_DIR, device_home=DEVICE_HOME, debounce_seconds=2.0, poll_interval=1.0):
        self.drive_path = pathlib.Path(drive_path)
        self.caps_dir = pathlib.Path(caps_dir)
        self.device_home = device_home
        self.debounce_seconds = float(debounce_seconds)
        self.poll_interval = float(poll_interval)
        self.sync_engine = DriveSyncEngine(drive_path=drive_path, caps_dir=caps_dir, device_home=device_home)
        self.running = False
        self._last_snapshot = {}
        self._dirty_since = None
        self._known_usb_devices = set()
        self._inbox_sizes = {}
        self._started = False
        self._last_full_sync = time.monotonic()

    def _take_snapshot(self) -> dict:
        """Collect mtimes and sizes of monitored directories (abilities, config, sounds)."""
        snapshot = {}
        if not self.drive_path.exists():
            return snapshot

        monitored_subdirs = ["abilities", "config", "sounds"]
        for sub in monitored_subdirs:
            p = self.drive_path / sub
            if not p.exists():
                continue
            try:
                for root, dirs, files in os.walk(p):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "logs"]
                    for f in files:
                        if f.startswith(".") or f.endswith((".tmp", ".swp")) or f == "device_info.json":
                            continue
                        fpath = os.path.join(root, f)
                        try:
                            st = os.stat(fpath)
                            snapshot[fpath] = (st.st_mtime, st.st_size)
                        except OSError:
                            pass
            except OSError:
                pass
        return snapshot

    def check_once(self) -> bool:
        """Check for changes and execute sync if changes have settled. Returns True if sync occurred."""
        usb_synced = False
        try:
            detected_devs = self.sync_engine.detect_external_usb_devices()
            current_usb = {d["device"] for d in detected_devs}
            if current_usb != self._known_usb_devices:
                new_devs = current_usb - self._known_usb_devices
                removed_devs = self._known_usb_devices - current_usb
                self._known_usb_devices = current_usb
                if new_devs:
                    for dev in new_devs:
                        self.sync_engine.log(f"Auto-detected physical USB-A storage device: {dev}")
                        self.sync_engine.sync_external_usb(device_path=dev)
                        usb_synced = True
                if removed_devs:
                    for dev in removed_devs:
                        self.sync_engine.log(f"Physical USB-A storage device disconnected: {dev}")
                        self.sync_engine.eject_external_usb()
        except Exception as e:
            self.sync_engine.log(f"USB-A check error: {e}")

        if not is_drive_mounted():
            self._dirty_since = None
            return usb_synced

        self.process_inbox()

        current_snapshot = self._take_snapshot()
        now = time.monotonic()

        # Edits made while the watcher was down (a restart, a reboot) would
        # otherwise become the baseline and wait for some later change.
        if not self._started:
            self._started = True
            # Keep the snapshot from BEFORE the sync as the baseline. Anything
            # written while the sync runs then still differs from it and is
            # picked up on the next pass.
            self._last_snapshot = current_snapshot
            self.sync_engine.run_full_sync()
            return True

        if current_snapshot != self._last_snapshot:
            if self._dirty_since is None:
                self.sync_engine.log("Detected filesystem changes on drive. Waiting for writes to settle...")
            self._dirty_since = now
            self._last_snapshot = current_snapshot
            return False

        if self._dirty_since is not None:
            if (now - self._dirty_since) >= self.debounce_seconds:
                self.sync_engine.log(f"Writes settled ({self.debounce_seconds}s quiet period). Running auto-sync...")
                self._dirty_since = None
                self._last_snapshot = current_snapshot
                self._last_full_sync = now
                try:
                    os.sync()
                except Exception:
                    pass
                self.sync_engine.run_full_sync()
                return True

        if self._dirty_since is None and (now - self._last_full_sync) >= RESYNC_SECONDS:
            self._last_full_sync = now
            self._last_snapshot = current_snapshot
            self.sync_engine.run_full_sync()
            return True

        return False

    def process_inbox(self) -> int:
        """Install .py and .zip files left in inbox/, once they have stopped growing."""
        inbox = self.drive_path / "inbox"
        if not inbox.is_dir():
            return 0
        web_portal = load_web_portal()
        handled = 0
        for item in sorted(inbox.iterdir()):
            if not item.is_file() or item.name.startswith(".") or item.suffix.lower() not in (".py", ".zip"):
                continue  # dot-files are what desktops leave beside real files, never uploads
            size = item.stat().st_size
            if self._inbox_sizes.get(item.name) != size:
                self._inbox_sizes[item.name] = size  # still arriving, look again next pass
                continue
            self._inbox_sizes.pop(item.name, None)
            try:
                name = web_portal.stage_upload(item.name, item.read_bytes(), self.drive_path)
                self.sync_engine.log(f"Inbox file {item.name} staged as ability '{name}'")
                verdict = "installed"
            except web_portal.UploadError as e:
                self.sync_engine.log(f"Inbox file {item.name} refused: {e}")
                verdict = "refused"
            done = inbox / verdict
            done.mkdir(exist_ok=True)
            shutil.move(str(item), str(done / item.name))
            handled += 1
        return handled

    def run(self, max_iterations=None):
        """Run watcher loop until interrupted."""
        self.running = True
        self.sync_engine.log(f"Starting OpenFile Watcher daemon (debounce={self.debounce_seconds}s)...")
        iterations = 0
        while self.running:
            try:
                self.check_once()
                iterations += 1
                if max_iterations and iterations >= max_iterations:
                    break
                time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                self.sync_engine.log("Watcher received interrupt, shutting down.")
                break
            except Exception as e:
                self.sync_engine.log(f"Watcher error: {e}")
                time.sleep(self.poll_interval)
        self.running = False


def watch_drive(*args):
    """Start watcher daemon to automatically sync drive on changes."""
    debounce = 2.0
    single_pass = False
    iterations = None

    for i, a in enumerate(args):
        if a in ["--debounce", "-d"] and i + 1 < len(args):
            try:
                debounce = float(args[i + 1])
            except ValueError:
                pass
        elif a in ["--single", "--single-pass", "-1"]:
            single_pass = True
        elif a in ["--iterations", "-n"] and i + 1 < len(args):
            try:
                iterations = int(args[i + 1])
            except ValueError:
                pass

    watcher = DriveWatcher(debounce_seconds=debounce)
    if single_pass:
        synced = watcher.check_once()
        _emit_success("Single pass completed.", {"synced": synced})
        return

    pid_file = PID_FILE
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    try:
        watcher.run(max_iterations=iterations)
    finally:
        try:
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except Exception:
            pass


def discovery_status() -> dict:
    """Ask the network the way computers do, and see whether this device answers.

    Windows sends a WS-Discovery probe; macOS and Linux browse mDNS. Both are
    asked here from the device itself, so a daemon that is running but silent
    shows up as a failure instead of a green light.
    """
    result = {"windows_discovery": False, "mdns": False}
    probe = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope '
             'xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
             'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
             'xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"><soap:Header>'
             '<wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>'
             '<wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>'
             f'<wsa:MessageID>urn:uuid:{os.urandom(16).hex()}</wsa:MessageID></soap:Header>'
             '<soap:Body><wsd:Probe/></soap:Body></soap:Envelope>')
    own = set()
    for info in network_interfaces().values():
        if info.get("ip"):
            own.add(info["ip"])
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(2.5)
        sock.sendto(probe.encode(), ("239.255.255.250", 3702))
        while True:
            data, addr = sock.recvfrom(65535)
            if b"ProbeMatch" in data and addr[0] in own:
                result["windows_discovery"] = True
                break
    except (OSError, socket.timeout):
        pass
    finally:
        with contextlib.suppress(Exception):
            sock.close()
    if shutil.which("avahi-browse"):
        listing = _run(["avahi-browse", "-t", "-r", "-p", "_smb._tcp"], timeout=6)
        result["mdns"] = any(line.startswith("=") and "OpenFile" in line for line in listing.splitlines())
    return result


def health(*_):
    """System health check for drive subsystem."""
    data = {
        "discovery": discovery_status(),
        "image_exists": os.path.exists(DRIVE_IMG),
        "mount_exists": os.path.exists(DRIVE_MOUNT),
        "mounted": is_drive_mounted(),
        "watcher_active": is_watcher_running(),
    }
    if not data["mounted"]:
        spoken = "Open file is installed but the drive is off."
    elif data["discovery"]["windows_discovery"] and data["discovery"]["mdns"]:
        spoken = "Open file is ready, and the drive is visible on the network."
    else:
        missing = [n for n, ok in (("Windows", data["discovery"]["windows_discovery"]),
                                   ("Mac and Linux", data["discovery"]["mdns"])) if not ok]
        spoken = f"Open file is ready, but the drive is not announcing itself to {' or '.join(missing)} computers. Say open file, restart."
    _emit_success(spoken, data)


FUNCTION_REGISTRY = {
    "enable_drive": enable_drive,
    "disable_drive": disable_drive,
    "sync_drive": sync_drive,
    "refresh_drive": sync_drive,
    "reseed_drive": reseed_drive,
    "undo_last_change": undo_last_change,
    "show_history": show_history,
    "restart_services": restart_services,
    "watch_drive": watch_drive,
    "get_status": get_status,
    "get_ports": get_ports,
    "health": health,
    "sync_usb": sync_usb,
    "eject_usb": eject_usb,
    "web": web_portal,
    "_sync_worker": _sync_worker,
}


def main():
    if len(sys.argv) < 2:
        _emit_error("no_function", "No function specified.")
        return

    fn_name = sys.argv[1]
    args = sys.argv[2:]

    func = FUNCTION_REGISTRY.get(fn_name)
    if not func:
        _emit_error("unknown_function", f"Function '{fn_name}' is not in FUNCTION_REGISTRY.")
        return

    try:
        func(*args)
    except Exception as e:
        _emit_error("execution_error", str(e), f"Drive action failed: {e}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the device files release asset, byte for byte the same every time, and print its SHA-256.

OpenHome delivers devkit_functions.py and requirements.txt to the DevKit. Everything else the
device needs (installer, services, share and discovery config, web page, command line) comes
from this one archive, downloaded on the first "turn on drive" and checked against the SHA-256
pinned in devkit_functions.py before anything in it is extracted or run. The archive leaves out
devkit_functions.py itself, so the pin never depends on the file that holds it.

    python3 tools/release_asset.py 0.4.0      writes dist/openfile-device-0.4.0.tar.gz
"""
import gzip
import hashlib
import io
import pathlib
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# What the device needs beyond OpenHome's two files. Tests, tools and the platform side stay out.
DEVICE = ["install.sh", "README.md", "CHANGELOG.md", "LICENSE", "bin", "device", "systemd", "samba", "avahi"]


def files():
    for name in DEVICE:
        path = ROOT / name
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and "__pycache__" not in child.parts and not child.name.startswith("."):
                    yield child
        elif path.is_file():
            yield path


def build(version: str) -> pathlib.Path:
    top = f"openfile-{version}"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in files():
            rel = path.relative_to(ROOT).as_posix()
            info = tarfile.TarInfo(f"{top}/{rel}")
            data = path.read_bytes()
            info.size = len(data)
            info.mode = 0o755 if (path.stat().st_mode & 0o111) else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = ROOT / "dist" / f"openfile-device-{version}.tar.gz"
    out.parent.mkdir(exist_ok=True)
    with open(out, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(raw.getvalue())
    return out


if __name__ == "__main__":
    version = sys.argv[1] if len(sys.argv) > 1 else "dev"
    path = build(version)
    print(path)
    print(hashlib.sha256(path.read_bytes()).hexdigest())

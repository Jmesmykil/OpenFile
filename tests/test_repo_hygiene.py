#!/usr/bin/env python3
"""Nothing that identifies one person's device or network belongs in this repository."""
import pathlib
import re
import subprocess
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent

PATTERNS = {
    "hardware address": re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b"),
    "private network address": re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "personal home folder": re.compile(r"/Users/[A-Za-z]|/home/(?!openhome\b)[a-z][a-z0-9_-]+/|C:\\Users\\"),
    "email address": re.compile(r"(?<![/:\w])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}"),
    "embedded credential": re.compile(r"(?i)\b(?:api[_-]?key|password|passwd|secret|token)\s*[=:]\s*[\"'][^\"'\s]{8,}[\"']"),
}
# Text that matches a pattern on purpose, with the reason it is allowed.
ALLOWED = {
    "tests/test_security.py": {"embedded credential"},  # planted fake secrets the tests must catch
    "tests/test_repo_hygiene.py": set(PATTERNS),  # the samples this file plants to prove the scanner works
}


def shipped_files():
    """Tracked and untracked-but-not-ignored files. A deployed copy has no .git, so walk it."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=PACKAGE_DIR, capture_output=True, text=True, check=True).stdout
        return [PACKAGE_DIR / line for line in out.splitlines() if (PACKAGE_DIR / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        skipped = {"__pycache__", ".git"}
        return [p for p in sorted(PACKAGE_DIR.rglob("*"))
                if p.is_file() and not skipped & set(p.parts) and p.suffix not in (".pyc", ".img", ".log")]


def scan(files, root=PACKAGE_DIR):
    findings = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root).as_posix()
        for label, pattern in PATTERNS.items():
            if label in ALLOWED.get(rel, ()):
                continue
            for match in pattern.finditer(text):
                findings.append(f"{rel}: {label}: {match.group(0)}")
    return findings


class TestRepoHygiene(unittest.TestCase):
    def test_no_personal_or_device_specific_data(self):
        files = shipped_files()
        self.assertGreater(len(files), 10, "too few files found, so this check inspected nothing useful")
        self.assertEqual(scan(files), [])

    def test_the_scanner_can_see_each_kind_of_leak(self):
        import tempfile
        planted = {
            "hardware address": "mac = '02:00:00:aa:bb:cc'",
            "private network address": "ip = 192.168.0.99",
            "personal home folder": "path = /Users/someone/project",
            "email address": "mail someone@example.com and not smb://openhome:admin@openhome.local",
            "embedded credential": "API_KEY = 'abcd1234efgh5678'",
        }
        self.assertEqual(set(planted), set(PATTERNS))
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for label, text in planted.items():
                (root / f"{label.replace(' ', '_')}.py").write_text(text + "\n")
            found = scan(sorted(root.iterdir()), root=root)
        for label in planted:
            self.assertTrue(any(f": {label}: " in f for f in found), f"scanner is blind to: {label}")


if __name__ == "__main__":
    unittest.main()


class TestOwnerSettingsSurviveAReinstall(unittest.TestCase):
    """Every setting the README tells an owner to put in /etc/default/openfile is kept when
    install.sh rebuilds that file. OPENFILE_EXTENDED_LEVELS was documented and then dropped
    by the next install, which reset a tuned microphone's limit without a word."""

    def test_documented_settings_are_kept(self):
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        readme = (root / "README.md").read_text()
        documented = set(re.findall(r"`(OPENFILE_[A-Z_]+)(?:=[^`]*)?` (?:in|to) `/etc/default/openfile`", readme))
        self.assertIn("OPENFILE_EXTENDED_LEVELS", documented, "the README no longer documents it")
        kept = re.search(r"\^OPENFILE_\(([A-Z_|]+)\)=", (root / "install.sh").read_text())
        self.assertIsNotNone(kept, "install.sh no longer keeps owner settings at all")
        kept = {"OPENFILE_" + k for k in kept.group(1).split("|")}
        self.assertEqual(documented - kept, set())

#!/usr/bin/env python3
"""A user must not be able to break the device in a way one command does not fix."""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401  must come before devkit_functions

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import devkit_functions as df  # noqa: E402

ENV = "# account\nAPI_KEY=k\n# audio\nSPEAKER_VOLUME=50\nMIC_SENSITIVITY=160\nAUTO_INTERRUPT=false\n"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_safe_"))
        self.drive, self.home = self.tmp / "drive", self.tmp / "home"
        self.caps = self.home / "openhome_devkit" / "local_capabilities"
        self.caps.mkdir(parents=True)
        self.drive.mkdir()
        (self.home / ".env").write_text(ENV)
        self.patches = [mock.patch.object(df, "SNAPSHOT_DIR", str(self.tmp / "snapshots")),
                        mock.patch.object(df, "STATE_FILE", str(self.tmp / "state.json")),
                        mock.patch.object(df, "LOCK_FILE", str(self.tmp / "lock"))]
        for patch in self.patches:
            patch.start()
        self.engine = df.DriveSyncEngine(drive_path=self.drive, caps_dir=self.caps, device_home=str(self.home))

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ability(self, name, body="version = 1\n", **extra):
        folder = self.caps / name
        folder.mkdir(parents=True)
        (folder / "devkit_functions.py").write_text(body)
        for filename, text in extra.items():
            (folder / filename.replace("__", ".")).write_text(text)
        return folder

    def on_drive(self, name, rel="devkit_functions.py"):
        return self.drive / "abilities" / name / rel

    def log(self):
        return self.engine.log_file.read_text()


class TestUndo(Sandbox):
    def test_a_bad_edit_that_parses_is_reversed_by_one_undo(self):
        """The case no check can catch: valid Python, wrong behaviour."""
        device = self.ability("weather") / "devkit_functions.py"
        self.engine.run_full_sync()
        self.on_drive("weather").write_text("version = 'broken but it parses'\n")
        self.engine.run_full_sync()
        self.assertIn("broken", device.read_text())

        result = self.engine.undo_last()

        self.assertTrue(result["success"])
        self.assertEqual(device.read_text(), "version = 1\n")
        self.assertEqual(self.on_drive("weather").read_text(), "version = 1\n", "the volume must show what is running")
        self.assertEqual(self.engine.run_full_sync()["abilities_synced"], 0, "and the next sync must not redo the edit")
        self.assertEqual(device.read_text(), "version = 1\n")

    def test_undo_takes_back_a_newly_added_ability_without_losing_it(self):
        self.ability("keep")
        self.engine.run_full_sync()
        (self.drive / "abilities" / "fresh").mkdir()
        self.on_drive("fresh").write_text("new = 1\n")
        self.engine.run_full_sync()
        self.assertTrue((self.caps / "fresh").is_dir())

        self.engine.undo_last()

        self.assertFalse((self.caps / "fresh").exists())
        self.assertFalse((self.drive / "abilities" / "fresh").exists())
        self.assertEqual([p.read_text() for p in (self.drive / "backup").rglob("devkit_functions.py")], ["new = 1\n"])
        self.engine.run_full_sync()
        self.assertFalse((self.caps / "fresh").exists(), "it must not creep back")
        self.assertTrue((self.caps / "keep").is_dir())

    def test_undo_walks_back_one_change_at_a_time(self):
        device = self.ability("weather") / "devkit_functions.py"
        self.engine.run_full_sync()
        for version in (2, 3):
            self.on_drive("weather").write_text(f"version = {version}\n")
            self.engine.run_full_sync()   # back to back, well inside one second
        self.assertEqual(device.read_text(), "version = 3\n")
        self.engine.undo_last()
        self.assertEqual(device.read_text(), "version = 2\n")
        self.engine.undo_last()
        self.assertEqual(device.read_text(), "version = 1\n")
        self.assertFalse(self.engine.undo_last()["success"])

    def test_a_setting_change_is_reversed_too(self):
        self.engine.run_full_sync()
        settings = self.drive / "config" / "settings.env"
        settings.write_text(settings.read_text().replace("SPEAKER_VOLUME=50", "SPEAKER_VOLUME=95"))
        self.engine.run_full_sync()
        self.assertIn("SPEAKER_VOLUME=95", (self.home / ".env").read_text())

        self.engine.undo_last()

        self.assertEqual((self.home / ".env").read_text(), ENV, "every line, comments included, as it was")
        self.assertIn("SPEAKER_VOLUME=50", settings.read_text())

    def test_old_snapshots_are_trimmed(self):
        root = pathlib.Path(df.SNAPSHOT_DIR)
        for i in range(8):
            (root / f"2030010{i}-000000").mkdir(parents=True)
        (root / "not-a-snapshot").mkdir()
        self.assertEqual(df.keep_newest(root, 3), 5)
        self.assertEqual(sorted(p.name for p in root.iterdir()),
                         ["20300105-000000", "20300106-000000", "20300107-000000", "not-a-snapshot"])


class TestGuards(Sandbox):
    def test_openfile_cannot_be_broken_from_the_volume(self):
        with mock.patch.object(df, "SELF_NAME", "openfile"):
            device = self.ability("openfile", body="working = 1\n") / "devkit_functions.py"
            self.engine.run_full_sync()
            self.on_drive("openfile").write_text("import nothing_that_exists\n")
            (self.drive / "abilities" / "openfile" / "planted.py").write_text("x = 1\n")
            result = self.engine.run_full_sync()

            self.assertEqual(device.read_text(), "working = 1\n")
            self.assertFalse((self.caps / "openfile" / "planted.py").exists())
            self.assertEqual(self.on_drive("openfile").read_text(), "working = 1\n", "the volume shows the running copy again")
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("ATTENTION", self.log())
            held = sorted(p.name for p in (self.drive / "backup").rglob("*.py"))
            self.assertEqual(held, ["devkit_functions.py", "planted.py"], "the user's work is kept")
            self.assertEqual(self.engine.run_full_sync()["warnings"], [], "and it does not nag on every pass")

    def test_the_owner_can_lift_that_protection(self):
        with mock.patch.object(df, "SELF_NAME", "openfile"), mock.patch.object(df, "ALLOW_SELF_EDIT", True):
            device = self.ability("openfile", body="working = 1\n") / "devkit_functions.py"
            self.engine.run_full_sync()
            self.on_drive("openfile").write_text("working = 2\n")
            self.engine.run_full_sync()
            self.assertEqual(device.read_text(), "working = 2\n")

    def test_broken_json_is_never_sent(self):
        device = self.ability("weather", config__json='{"name": "ok"}') / "config.json"
        self.engine.run_full_sync()
        self.on_drive("weather", "config.json").write_text('{"name": "missing quote}')
        self.on_drive("weather").write_text("version = 2\n")
        result = self.engine.run_full_sync()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("not valid JSON", result["errors"][0])
        self.assertEqual(json.loads(device.read_text()), {"name": "ok"})
        self.assertEqual((self.caps / "weather" / "devkit_functions.py").read_text(), "version = 1\n")

    def test_settings_outside_their_range_are_refused_and_explained(self):
        self.engine.run_full_sync()
        settings = self.drive / "config" / "settings.env"
        settings.write_text("SPEAKER_VOLUME=900\nMIC_SENSITIVITY=loud\nAUTO_INTERRUPT=maybe\n")
        result = self.engine.run_full_sync()
        self.assertEqual((self.home / ".env").read_text(), ENV)
        self.assertEqual(len(result["warnings"]), 1)
        for fragment in ("SPEAKER_VOLUME=900", "from 0 to 100", "MIC_SENSITIVITY=loud", "true or false"):
            self.assertIn(fragment, result["warnings"][0])
        self.assertIn("SPEAKER_VOLUME=50", settings.read_text(), "the file is set back so it shows the truth")

    def test_a_good_setting_beside_a_bad_one_is_still_applied(self):
        self.engine.run_full_sync()
        (self.drive / "config" / "settings.env").write_text("SPEAKER_VOLUME=70\nMIC_SENSITIVITY=9999\n")
        self.engine.run_full_sync()
        env = (self.home / ".env").read_text()
        self.assertIn("SPEAKER_VOLUME=70", env)
        self.assertIn("MIC_SENSITIVITY=160", env)

    def test_every_setting_has_a_rule(self):
        self.assertEqual(set(df.SETTINGS_RULES), set(df.SETTINGS_KEYS))
        self.assertEqual(df.check_setting("SPEAKER_VOLUME", "0"), "")
        self.assertEqual(df.check_setting("SPEAKER_VOLUME", "100"), "")
        self.assertNotEqual(df.check_setting("SPEAKER_VOLUME", "101"), "")
        self.assertNotEqual(df.check_setting("SPEAKER_VOLUME", "-1"), "")
        self.assertEqual(df.check_setting("AUTO_INTERRUPT", "TRUE"), "")

    def test_a_failed_wifi_join_returns_to_the_previous_network(self):
        wifi = self.drive / "config" / "wifi.txt"
        wifi.parent.mkdir(parents=True)
        wifi.write_text("SSID=Cafe\nPASSWORD=wrongpass\n")
        calls = []

        def fake_run(cmd, **_):
            calls.append(cmd)
            return mock.Mock(returncode=1 if "connect" in cmd else 0, stdout="", stderr="secrets were required")

        with mock.patch.object(df.shutil, "which", return_value="/usr/bin/nmcli"), \
                mock.patch.object(df, "active_wifi_ssid", return_value="Home"), \
                mock.patch.object(df, "_run", return_value="Home Network:802-11-wireless\nlo:loopback\n"), \
                mock.patch.object(df.subprocess, "run", side_effect=fake_run):
            ok, _ = self.engine.sync_wifi()
        self.assertFalse(ok)
        self.assertEqual(calls[-1], ["nmcli", "con", "up", "id", "Home Network"])
        self.assertNotIn("wrongpass", wifi.read_text())
        self.assertIn("ATTENTION", self.log())

    def test_old_backups_are_trimmed_and_recent_ones_kept(self):
        for i in range(12):
            (self.drive / "backup" / f"203001{i:02d}-000000").mkdir(parents=True)
        with mock.patch.object(df, "BACKUP_KEEP", 4):
            self.engine.run_full_sync()
        left = sorted(p.name for p in (self.drive / "backup").iterdir())
        self.assertEqual(left, ["20300108-000000", "20300109-000000", "20300110-000000", "20300111-000000"])

    def test_desktop_scrap_files_are_swept_and_nothing_else(self):
        self.ability("weather")
        self.engine.run_full_sync()
        folder = self.drive / "abilities" / "weather"
        old = [folder / "._devkit_functions.py", folder / ".DS_Store", self.drive / "Thumbs.db"]
        fresh = folder / "._still_copying.py"
        keep = [folder / "devkit_functions.py", folder / "._not_a_scrap_dir", self.drive / "backup" / "x" / "._kept"]
        keep[1].mkdir()
        keep[2].parent.mkdir(parents=True)
        for f in old + [fresh, keep[2]]:
            f.write_text("scrap")
        for f in old + [keep[2]]:
            os.utime(f, (1, 1))
        self.engine.run_full_sync()
        self.assertEqual([f.exists() for f in old], [False, False, False])
        self.assertTrue(fresh.exists(), "a file that may still be arriving is left alone")
        self.assertTrue(all(k.exists() for k in keep), "real files, folders and backups are never swept")

    def test_the_volume_explains_itself(self):
        self.engine.run_full_sync()
        guide = (self.drive / "abilities" / "READ ME FIRST.txt").read_text()
        for phrase in ("undo", "requirements.txt", "openhome deploy", "held back"):
            self.assertIn(phrase, guide)
        self.assertIn("disconnects you", (self.drive / "config" / "READ ME FIRST.txt").read_text())
        self.assertIn("CAREFUL", (self.drive / "config" / "wifi.txt").read_text())


if __name__ == "__main__":
    unittest.main()


class TestFirstTimeSetup(Sandbox):
    """OpenHome delivers an ability's files, not the installer's folders. The first call fetches them."""

    def release_tarball(self, with_installer=True):
        import io
        import tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            def add(name, text, mode=0o644):
                data = text.encode()
                info = tarfile.TarInfo(f"OpenFile-0.3.0/{name}")
                info.size, info.mode = len(data), mode
                tar.addfile(info, io.BytesIO(data))
            if with_installer:
                add("install.sh", "#!/bin/bash\necho installed > installed.marker\n", 0o755)
            add("samba/openfile.conf", "[OpenFile]\n")
            add("devkit_functions.py", "# a replacement from outside: must never land\n")
            add("../escape.sh", "echo no\n")
            link = tarfile.TarInfo("OpenFile-0.3.0/evil"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
            tar.addfile(link)
        return buffer.getvalue()

    def _setup_with(self, archive: bytes, pinned: str):
        folder = self.tmp / "openfile"
        folder.mkdir(exist_ok=True)
        (folder / "devkit_functions.py").write_text("# the one file OpenHome delivered\n")
        fake = mock.MagicMock()
        fake.__enter__.return_value = __import__("io").BytesIO(archive)
        launched = []
        with mock.patch.object(df, "DEFAULTS_FILE", str(self.tmp / "absent-defaults")), \
                mock.patch.object(df, "RELEASE_SHA256", pinned), \
                mock.patch.object(df, "ability_dir", return_value=folder), \
                mock.patch("urllib.request.urlopen", return_value=fake), \
                mock.patch.object(df.subprocess, "Popen", side_effect=lambda cmd, **kw: launched.append(cmd)):
            spoken = df.first_time_setup()
        return folder, spoken, launched

    def test_a_download_that_is_not_the_pinned_one_installs_nothing(self):
        archive = self.release_tarball()
        folder, spoken, launched = self._setup_with(archive, "f" * 64)
        self.assertIn("nothing was installed", spoken)
        self.assertEqual(launched, [], "the installer never runs")
        self.assertFalse((folder / "install.sh").exists(), "and nothing is extracted")

    def test_a_bare_ability_folder_fetches_the_release_and_runs_the_installer(self):
        import hashlib
        archive = self.release_tarball()
        folder, spoken, launched = self._setup_with(archive, hashlib.sha256(archive).hexdigest())
        self.assertEqual((folder / "devkit_functions.py").read_text(), "# the one file OpenHome delivered\n",
                         "the delivered file is never replaced by one from the archive")
        self.assertIn("first time", spoken)
        self.assertTrue((folder / "install.sh").is_file())
        self.assertTrue((folder / "samba" / "openfile.conf").is_file())
        self.assertFalse((self.tmp / "escape.sh").exists(), "no member may leave the ability folder")
        self.assertFalse((folder / "evil").exists(), "links are never extracted")
        self.assertEqual(launched, [["/bin/bash", str(folder / "install.sh")]])

    def test_an_installed_device_does_nothing(self):
        with mock.patch.object(df, "DEFAULTS_FILE", str(self.home / ".env")):  # any existing file
            self.assertEqual(df.first_time_setup(), "")

    def test_a_failed_fetch_is_explained_not_hidden(self):
        folder = self.tmp / "openfile"
        folder.mkdir()
        with mock.patch.object(df, "DEFAULTS_FILE", str(self.tmp / "absent-defaults")), \
                mock.patch.object(df, "ability_dir", return_value=folder), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("no route to host")):
            spoken = df.first_time_setup()
        self.assertIn("internet", spoken)
        self.assertFalse((folder / "install.sh").exists())


class TestRunFromACopy(unittest.TestCase):
    """OpenHome's stock node server copies an ability's file to openhome_devkit/ and runs the
    copy there. The copy must still find the ability's own folder, never openhome_devkit."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_copy_"))
        self.caps = self.tmp / "openhome_devkit" / "local_capabilities"
        source = pathlib.Path(df.__file__).read_bytes()
        (self.caps / "SomeOtherAbility").mkdir(parents=True)
        (self.caps / "SomeOtherAbility" / "devkit_functions.py").write_text("# another ability\n")
        (self.caps / "OpenFile").mkdir()
        (self.caps / "OpenFile" / "devkit_functions.py").write_bytes(source)
        self.copy = self.tmp / "openhome_devkit" / "devkit_functions.py"
        self.copy.write_bytes(source)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _folder_seen_from(self, path, caps):
        import subprocess
        probe = ("import runpy; m = runpy.run_path(%r, run_name='probe'); "
                 "print(m['_own_folder']()); print(m['SELF_NAME'])" % str(path))
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                             env={**os.environ, "LOCAL_CAPABILITIES_DIR": str(caps)}, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        return out.stdout.split()

    def test_the_copy_finds_the_ability_folder(self):
        folder, name = self._folder_seen_from(self.copy, self.caps)
        self.assertEqual(folder, str(self.caps / "OpenFile"))
        self.assertEqual(name, "OpenFile")

    def test_the_file_in_its_own_folder_stays_there(self):
        folder, _ = self._folder_seen_from(self.caps / "OpenFile" / "devkit_functions.py", self.caps)
        self.assertEqual(folder, str(self.caps / "OpenFile"))

    def test_with_no_copy_of_itself_anywhere_it_takes_the_default_folder(self):
        folder, name = self._folder_seen_from(self.copy, self.tmp / "absent")
        self.assertEqual((folder, name), (str(self.tmp / "absent" / "openfile"), "openfile"))


class TestWithinTheCall(unittest.TestCase):
    """OpenHome ends a capability call at 15 seconds: slow work answers in time or says so."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_call_"))
        (self.tmp / "devkit_functions.py").write_text(
            "import sys, time, json\n"
            "if sys.argv[1] == 'slow':\n    time.sleep(3)\n"
            "print(json.dumps({'success': True, 'spoken_response': sys.argv[1] + ' done'}))\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def said(self, worker):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with mock.patch.object(df, "ability_dir", return_value=self.tmp), \
                mock.patch.object(df, "CALL_BUDGET_SECONDS", 1.0), redirect_stdout(out):
            df._within_the_call(worker, "still going")
        return json.loads(out.getvalue().strip().splitlines()[-1])["spoken_response"]

    def test_quick_work_answers_itself(self):
        self.assertEqual(self.said("fast"), "fast done")

    def test_slow_work_says_it_is_still_going(self):
        self.assertEqual(self.said("slow"), "still going")

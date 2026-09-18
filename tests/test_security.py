#!/usr/bin/env python3
"""The volume is reachable without a password, so these hold the line on what it can expose or accept."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401  must come before devkit_functions
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))
sys.path.insert(0, str(PACKAGE_DIR / "device"))

import devkit_functions as df  # noqa: E402
import web_portal as wp  # noqa: E402

DEVICE_ENV = (
    "# account\n"
    "API_KEY=super-secret-key\n"
    "MQTT_PASSWORD=broker-secret\n"
    "DEFAULT_AGENT=12345\n"
    "\n"
    "# audio\n"
    "SPEAKER_VOLUME=50\n"
    "MIC_SENSITIVITY=160\n"
)


def make_zip(entries, symlink=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_sec_"))
        self.drive, self.home = self.tmp / "drive", self.tmp / "home"
        self.caps = self.home / "openhome_devkit" / "local_capabilities"
        self.caps.mkdir(parents=True)
        self.drive.mkdir()
        (self.home / ".env").write_text(DEVICE_ENV)
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

    def everything_on_drive(self) -> str:
        files = [f for f in self.drive.rglob("*") if f.is_file()]
        self.assertTrue(files, "the drive is empty, so this check looked at nothing")
        return "\n".join(f.read_text(errors="ignore") for f in files)


class TestSecretsStayOnDevice(Sandbox):
    def test_seeding_never_copies_secrets(self):
        df.scaffold_drive_tree(self.drive, caps_dir=str(self.caps), device_home=str(self.home))
        text = self.everything_on_drive()
        self.assertIn("SPEAKER_VOLUME=50", text)  # the export did run
        for secret in ("super-secret-key", "broker-secret", "API_KEY", "MQTT_PASSWORD"):
            self.assertNotIn(secret, text)
        self.assertFalse((self.drive / "config" / ".env").exists())

    def test_a_leaked_copy_from_an_older_release_is_scrubbed(self):
        config = self.drive / "config"
        config.mkdir(parents=True)
        (config / ".env").write_text(DEVICE_ENV)
        (config / "settings.env").write_text(DEVICE_ENV)
        self.engine.run_full_sync()
        self.assertNotIn("secret", self.everything_on_drive())

    def test_drive_cannot_write_keys_outside_the_tuning_list(self):
        config = self.drive / "config"
        config.mkdir(parents=True)
        (config / "settings.env").write_text("SPEAKER_VOLUME=80\nAPI_KEY=attacker\nSERVER_URL_API=http://evil\n")
        self.assertTrue(self.engine.sync_settings())
        env = (self.home / ".env").read_text()
        self.assertIn("SPEAKER_VOLUME=80", env)
        self.assertIn("API_KEY=super-secret-key", env)
        self.assertNotIn("evil", env)

    def test_device_env_keeps_its_comments_and_order(self):
        config = self.drive / "config"
        config.mkdir(parents=True)
        (config / "settings.env").write_text("MIC_SENSITIVITY=170\n")
        self.engine.sync_settings()
        self.assertEqual((self.home / ".env").read_text(), DEVICE_ENV.replace("MIC_SENSITIVITY=160", "MIC_SENSITIVITY=170"))

    def test_a_level_written_to_env_is_applied_to_the_mixer_at_once(self):
        from unittest import mock
        config = self.drive / "config"
        config.mkdir(parents=True)
        (config / "settings.env").write_text("MIC_SENSITIVITY=170\nSPEAKER_VOLUME=53\n")
        calls = []
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        import pwd
        me = pwd.getpwuid(os.getuid())
        owner_lives_here = mock.Mock(pw_name=me.pw_name, pw_dir=str(self.home))
        with mock.patch.dict(os.environ, {"OPENFILE_AUDIO": "1", "OPENFILE_EXTENDED_LEVELS": "1"}), \
             mock.patch.object(pwd, "getpwuid", return_value=owner_lives_here), \
             mock.patch.object(df.shutil, "which", return_value="/usr/bin/pactl"), \
             mock.patch.object(df.subprocess, "run", side_effect=lambda cmd, **k: calls.append(cmd) or ok):
            self.assertTrue(self.engine.sync_settings())
        verbs = {tuple(c[-3:]) for c in calls}
        self.assertIn(("set-source-volume", "@DEFAULT_SOURCE@", "170%"), verbs)
        self.assertIn(("set-sink-volume", "@DEFAULT_SINK@", "53%"), verbs)

    def test_a_copy_of_the_settings_never_moves_the_live_mixer(self):
        # The device stress suite writes a sandboxed .env whose owner is the real account.
        # Its levels once reached the real speaker. Only the owner's own home may.
        from unittest import mock
        calls = []
        ok = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"OPENFILE_AUDIO": "1"}), \
             mock.patch.object(df.shutil, "which", return_value="/usr/bin/pactl"), \
             mock.patch.object(df.subprocess, "run", side_effect=lambda cmd, **k: calls.append(cmd) or ok):
            applied = self.engine.apply_audio_levels({"SPEAKER_VOLUME": "92", "MIC_SENSITIVITY": "170"})
        self.assertEqual((applied, calls), ([], []))

    def test_audio_is_never_touched_under_the_test_sandbox(self):
        self.assertEqual(self.engine.apply_audio_levels({"MIC_SENSITIVITY": "170"}), [])

    def test_parked_abilities_are_not_published(self):
        for name in ("weather", "weather.retired-20260101", "old.bak"):
            (self.caps / name).mkdir()
            (self.caps / name / "devkit_functions.py").write_text("x = 1\n")
        df.scaffold_drive_tree(self.drive, caps_dir=str(self.caps), device_home=str(self.home))
        self.assertEqual(sorted(p.name for p in (self.drive / "abilities").iterdir() if p.is_dir()), ["weather"])

    def test_nothing_is_invented_for_agents(self):
        df.scaffold_drive_tree(self.drive, caps_dir=str(self.caps), device_home=str(self.home))
        self.assertEqual([p.name for p in (self.drive / "agents").iterdir()], ["README.md"])


class TestLinks(Sandbox):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "private.txt").write_text("super-secret-key\n")
        (self.outside / "victim.py").write_text("original = 1\n")
        ability = self.caps / "linked"
        ability.mkdir()
        (ability / "devkit_functions.py").write_text("x = 1\n")
        (ability / "escape").symlink_to(self.outside, target_is_directory=True)
        (ability / "leak.txt").symlink_to(self.outside / "private.txt")
        (self.caps / "alias").symlink_to(ability, target_is_directory=True)

    def test_links_are_not_followed_onto_the_volume(self):
        self.engine.run_full_sync()
        self.assertNotIn("super-secret-key", self.everything_on_drive())
        self.assertEqual(sorted(p.name for p in (self.drive / "abilities").iterdir() if p.is_dir()), ["linked"])
        self.assertEqual(sorted(p.name for p in (self.drive / "abilities" / "linked").iterdir()), ["devkit_functions.py"])

    def test_a_volume_folder_cannot_write_through_a_device_link(self):
        self.engine.run_full_sync()
        planted = self.drive / "abilities" / "linked" / "escape"
        planted.mkdir()
        (planted / "victim.py").write_text("replaced = 1\n")
        (planted / "new_file.py").write_text("dropped = 1\n")
        self.engine.run_full_sync()
        self.engine.run_full_sync()
        self.assertEqual((self.outside / "victim.py").read_text(), "original = 1\n")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["private.txt", "victim.py"])


class TestUploads(Sandbox):
    def test_good_python_file_is_staged_on_the_drive_only(self):
        name = wp.stage_upload("hello.py", b"def run():\n    return 1\n", self.drive)
        self.assertEqual(name, "hello")
        self.assertTrue((self.drive / "abilities" / "hello" / "devkit_functions.py").is_file())
        self.assertEqual(list(self.caps.iterdir()), [], "an upload must not reach live abilities before sync")

    def test_broken_python_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaises(wp.UploadError):
            wp.stage_upload("bad.py", b"def (:\n", self.drive)
        self.assertEqual([p.name for p in (self.drive / "abilities").iterdir() if p.is_dir()], [])

    def test_zip_cannot_write_outside_its_folder(self):
        for label, body in {
            "parent path": make_zip({"../../escaped.py": "x = 1\n"}),
            "absolute path": make_zip({"/tmp/escaped.py": "x = 1\n"}),
            "symlink": make_zip({"a/devkit_functions.py": "x = 1\n"}, symlink="a/link"),
        }.items():
            with self.subTest(label), self.assertRaises(wp.UploadError):
                wp.stage_upload("mod.zip", body, self.drive)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["drive", "home"])
        self.assertFalse(pathlib.Path("/tmp/escaped.py").exists())

    def test_zip_folder_installs_under_its_own_name(self):
        body = make_zip({"lights/devkit_functions.py": "x = 1\n", "lights/config.json": "{}", "__MACOSX/._junk": "z"})
        self.assertEqual(wp.stage_upload("download (3).zip", body, self.drive), "lights")
        self.assertEqual(sorted(p.name for p in (self.drive / "abilities" / "lights").iterdir()),
                         ["config.json", "devkit_functions.py"])

    def test_hostile_file_names_are_tamed(self):
        self.assertEqual(wp.stage_upload("../../etc/cron.d/x.py", b"x = 1\n", self.drive), "x")
        self.assertTrue((self.drive / "abilities" / "x").is_dir())

    def test_oversized_and_unknown_files_are_refused(self):
        with self.assertRaises(wp.UploadError):
            wp.stage_upload("big.py", b"#" * (wp.MAX_UPLOAD_BYTES + 1), self.drive)
        with self.assertRaises(wp.UploadError):
            wp.stage_upload("run.sh", b"echo hi\n", self.drive)


class TestListening(unittest.TestCase):
    def test_the_web_page_answers_on_ipv4_and_ipv6(self):
        import http.client
        import socket
        import threading
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = wp.make_server(port)
        if server.address_family != socket.AF_INET6:
            server.server_close()
            self.skipTest("this system has no IPv6, so the IPv4 fallback is what runs")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for host in ("127.0.0.1", "::1"):
                with self.subTest(host=host):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("GET", "/")
                    self.assertEqual(conn.getresponse().status, 200)
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()


class TestRequestOrigin(unittest.TestCase):
    def test_device_names_and_addresses_are_accepted(self):
        import socket
        own = socket.gethostname().lower().split(".")[0]
        for host in ("192.0.2.10:8088", "198.51.100.7", "[fe80::1]:8088", "localhost:8088", f"{own}.local:8088"):
            with self.subTest(host=host):
                self.assertTrue(wp.host_allowed(host))

    def test_a_rebound_foreign_domain_is_refused(self):
        for host in ("evil.example.com:8088", "attacker.test", ""):
            with self.subTest(host=host):
                self.assertFalse(wp.host_allowed(host))


if __name__ == "__main__":
    unittest.main()


class TestLevelsFollowOpenHome(unittest.TestCase):
    """Speaker and microphone stop where OpenHome's own controls stop, unless the owner says."""

    def test_openhome_ranges_by_default(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"OPENFILE_EXTENDED_LEVELS": "0"}):
            self.assertEqual(df.check_setting("SPEAKER_VOLUME", "80"), "")
            self.assertNotEqual(df.check_setting("SPEAKER_VOLUME", "81"), "")
            self.assertEqual(df.check_setting("MIC_SENSITIVITY", "100"), "")
            self.assertNotEqual(df.check_setting("MIC_SENSITIVITY", "170"), "")

    def test_an_owner_who_tuned_past_them_can_say_so(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"OPENFILE_EXTENDED_LEVELS": "1"}):
            self.assertEqual(df.check_setting("MIC_SENSITIVITY", "170"), "")
            self.assertEqual(df.check_setting("SPEAKER_VOLUME", "100"), "")
            self.assertNotEqual(df.check_setting("MIC_SENSITIVITY", "201"), "")


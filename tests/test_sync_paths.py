#!/usr/bin/env python3
"""Flash drive ingest, reseed, the inbox, Wi-Fi handover and the sync lock."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401  must come before devkit_functions
import multiprocessing
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import devkit_functions as df  # noqa: E402


def hold_lock(path, seconds, ready):
    with df.sync_lock(path) as acquired:
        ready.put(acquired)
        time.sleep(seconds)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="openfile_sync_"))
        self.drive, self.home, self.stick = self.tmp / "drive", self.tmp / "home", self.tmp / "stick"
        self.caps = self.home / "openhome_devkit" / "local_capabilities"
        for d in (self.drive, self.caps, self.stick):
            d.mkdir(parents=True)
        (self.home / ".env").write_text("API_KEY=k\nSPEAKER_VOLUME=50\n")
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

    def ability(self, root, name, body="x = 1\n", **extra):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "devkit_functions.py").write_text(body)
        for filename, text in extra.items():
            (folder / filename.replace("__", ".")).write_text(text)
        return folder


class TestFlashDrive(Sandbox):
    def test_abilities_settings_and_wifi_on_a_stick_are_all_handled(self):
        """The shipped-broken path: requirements, settings.env and wifi.txt each crashed it."""
        self.ability(self.stick / "abilities", "lights", requirements__txt="# nothing to install\n")
        self.ability(self.stick, "loose_one")
        self.ability(self.stick / "abilities", "broken", body="def (:\n")
        (self.stick / "settings.env").write_text("SPEAKER_VOLUME=70\nAPI_KEY=nope\n")
        (self.stick / "wifi.txt").write_text("SSID=\nPASSWORD=\n")

        result = self.engine.ingest_usb_tree(self.stick)

        self.assertEqual(sorted(result["abilities_synced"]), ["lights", "loose_one"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("broken", result["errors"][0])
        for name in ("lights", "loose_one"):
            self.assertTrue((self.caps / name / "devkit_functions.py").is_file())
            self.assertTrue((self.drive / "abilities" / name / "devkit_functions.py").is_file())
        self.assertFalse((self.caps / "broken").exists())
        env = (self.home / ".env").read_text()
        self.assertIn("SPEAKER_VOLUME=70", env)
        self.assertIn("API_KEY=k", env)

    def test_a_read_only_stick_is_read_and_left_alone(self):
        """A real case: an installer image. Nothing to install, nowhere to write, no error."""
        self.ability(self.caps, "weather")
        (self.stick / "arch").mkdir()
        # A read-only filesystem refuses root too, which a chmod cannot imitate.
        with mock.patch.object(df.os, "access", return_value=False):
            result = self.engine.ingest_usb_tree(self.stick)
            self.engine.write_usb_backup(self.stick)
        self.assertEqual(result, {"abilities_synced": [], "errors": []})
        self.assertFalse((self.stick / "OPENFILE_BACKUP").exists())
        self.assertIn("read-only", self.engine.log_file.read_text())
        self.assertNotIn("Errno", self.engine.log_file.read_text())

    def test_backup_lands_on_the_stick(self):
        self.ability(self.caps, "weather")
        self.ability(self.caps, "weather.retired-1")
        self.engine.write_usb_backup(self.stick)
        backed_up = sorted(p.name for p in (self.stick / "OPENFILE_BACKUP" / "abilities").iterdir())
        self.assertEqual(backed_up, ["weather"])


class TestReseed(Sandbox):
    def test_reseed_restores_device_copy_and_keeps_the_edit(self):
        self.ability(self.caps, "weather", body="device = 1\n")
        self.engine.run_full_sync()
        edited = self.drive / "abilities" / "weather" / "devkit_functions.py"
        self.assertEqual(edited.read_text(), "device = 1\n")

        # An edit that cannot reach the device, so device and drive now disagree.
        edited.write_text("def (:\n")
        self.ability(self.drive / "abilities", "mine")
        result = self.engine.run_full_sync(reseed=True)

        self.assertTrue(result["reseeded"])
        self.assertEqual(edited.read_text(), "device = 1\n")
        kept = list((self.drive / "backup").rglob("devkit_functions.py"))
        self.assertEqual([k.read_text() for k in kept], ["def (:\n"])
        self.assertTrue((self.drive / "abilities" / "mine").is_dir(), "reseed must not delete the user's own abilities")

    def test_plain_sync_never_overwrites_an_edit(self):
        self.ability(self.caps, "weather", body="device = 1\n")
        self.engine.run_full_sync()
        edited = self.drive / "abilities" / "weather" / "devkit_functions.py"
        edited.write_text("mine = 2\n")
        self.engine.run_full_sync()
        self.assertEqual(edited.read_text(), "mine = 2\n")
        self.assertEqual((self.caps / "weather" / "devkit_functions.py").read_text(), "mine = 2\n")

    def test_unchanged_files_are_left_alone(self):
        self.ability(self.caps, "weather")
        self.engine.run_full_sync()
        target = self.caps / "weather" / "devkit_functions.py"
        before = target.stat().st_mtime_ns
        time.sleep(0.02)
        self.assertEqual(self.engine.run_full_sync()["abilities_synced"], 0)
        self.assertEqual(target.stat().st_mtime_ns, before)


class TestTwoWay(Sandbox):
    def test_an_update_made_on_the_device_is_pulled_not_reverted(self):
        """The data-loss case: a fresh deploy must not be overwritten by the older drive copy."""
        device_file = self.ability(self.caps, "brain", body="version = 1\n") / "devkit_functions.py"
        self.engine.run_full_sync()
        drive_file = self.drive / "abilities" / "brain" / "devkit_functions.py"

        device_file.write_text("version = 2\n")          # openhome deploy
        (self.caps / "brain" / "extra.py").write_text("e = 1\n")
        self.engine.run_full_sync()

        self.assertEqual(device_file.read_text(), "version = 2\n")
        self.assertEqual(drive_file.read_text(), "version = 2\n")
        self.assertTrue((self.drive / "abilities" / "brain" / "extra.py").is_file())

    def test_an_edit_on_the_drive_still_reaches_the_device(self):
        device_file = self.ability(self.caps, "brain", body="version = 1\n") / "devkit_functions.py"
        self.engine.run_full_sync()
        (self.drive / "abilities" / "brain" / "devkit_functions.py").write_text("version = 3\n")
        self.engine.run_full_sync()
        self.assertEqual(device_file.read_text(), "version = 3\n")

    def test_when_both_sides_changed_the_device_copy_is_kept(self):
        device_file = self.ability(self.caps, "brain", body="version = 1\n") / "devkit_functions.py"
        self.engine.run_full_sync()
        device_file.write_text("from_deploy = 1\n")
        (self.drive / "abilities" / "brain" / "devkit_functions.py").write_text("from_drive = 1\n")
        self.engine.run_full_sync()
        self.assertEqual(device_file.read_text(), "from_drive = 1\n")
        kept = list((self.drive / "backup").rglob("devkit_functions.py"))
        self.assertEqual([k.read_text() for k in kept], ["from_deploy = 1\n"])

    def test_with_no_history_the_running_device_copy_wins(self):
        """An upgrade: fresh code on the device, a stale copy on the volume, no manifest yet."""
        device_file = self.ability(self.caps, "openfile", body="new_build = 1\n") / "devkit_functions.py"
        drive_file = self.ability(self.drive / "abilities", "openfile", body="old_build = 1\n") / "devkit_functions.py"
        self.engine.run_full_sync()
        self.assertEqual(device_file.read_text(), "new_build = 1\n")
        self.assertEqual(drive_file.read_text(), "new_build = 1\n")
        kept = list((self.drive / "backup").rglob("devkit_functions.py"))
        self.assertEqual([k.read_text() for k in kept], ["old_build = 1\n"])

    def test_deleting_on_the_drive_never_deletes_from_the_device(self):
        self.ability(self.caps, "brain", helper__py="h = 1\n")
        self.engine.run_full_sync()
        (self.drive / "abilities" / "brain" / "helper.py").unlink()
        self.engine.run_full_sync()
        self.engine.run_full_sync()
        self.assertTrue((self.caps / "brain" / "helper.py").is_file())
        self.assertFalse((self.drive / "abilities" / "brain" / "helper.py").exists(), "and it must not keep coming back")

    def test_a_broken_helper_file_blocks_the_whole_ability(self):
        device_file = self.ability(self.caps, "brain", body="version = 1\n") / "devkit_functions.py"
        self.engine.run_full_sync()
        (self.drive / "abilities" / "brain" / "devkit_functions.py").write_text("version = 2\n")
        (self.drive / "abilities" / "brain" / "helper.py").write_text("def (:\n")
        result = self.engine.run_full_sync()
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(device_file.read_text(), "version = 1\n")
        self.assertFalse((self.caps / "brain" / "helper.py").exists())


    def test_removing_an_ability_on_the_device_retires_the_drive_copy(self):
        self.ability(self.caps, "gone", helper__py="h = 1\n")
        self.ability(self.caps, "stays")
        self.engine.run_full_sync()
        shutil.rmtree(self.caps / "gone")
        self.engine.run_full_sync()
        self.engine.run_full_sync()
        self.assertFalse((self.caps / "gone").exists(), "an uninstalled ability must not be pushed back")
        self.assertFalse((self.drive / "abilities" / "gone").exists())
        kept = sorted(p.name for p in (self.drive / "backup").rglob("*.py"))
        self.assertEqual(kept, ["devkit_functions.py", "helper.py"])
        self.assertTrue((self.caps / "stays").is_dir())

    def test_a_nested_ability_removed_on_the_device_leaves_no_empty_folders(self):
        folder = self.ability(self.caps, "nested")
        (folder / "lib" / "deep").mkdir(parents=True)
        (folder / "lib" / "deep" / "h.py").write_text("h = 1\n")
        self.engine.run_full_sync()
        shutil.rmtree(folder)
        self.engine.run_full_sync()
        self.assertFalse((self.drive / "abilities" / "nested").exists())
        self.assertEqual(len(list((self.drive / "backup").rglob("*.py"))), 2)

    def test_an_ability_folder_deleted_on_the_volume_stays_deleted_until_a_reseed(self):
        self.ability(self.caps, "unwanted")
        self.engine.run_full_sync()
        shutil.rmtree(self.drive / "abilities" / "unwanted")
        self.engine.run_full_sync()
        self.engine.run_full_sync()
        self.assertFalse((self.drive / "abilities" / "unwanted").exists(), "it must not keep coming back")
        self.assertTrue((self.caps / "unwanted" / "devkit_functions.py").is_file(), "and the device keeps it")
        self.engine.run_full_sync(reseed=True)
        self.assertTrue((self.drive / "abilities" / "unwanted" / "devkit_functions.py").is_file())

    def test_a_drive_copy_edited_after_the_removal_is_sent_as_new(self):
        self.ability(self.caps, "gone")
        self.engine.run_full_sync()
        shutil.rmtree(self.caps / "gone")
        (self.drive / "abilities" / "gone" / "devkit_functions.py").write_text("edited = 1\n")
        self.engine.run_full_sync()
        self.assertEqual((self.caps / "gone" / "devkit_functions.py").read_text(), "edited = 1\n")


class TestCallBudget(Sandbox):
    """OpenHome ends a capability call at 15 seconds. The sync must survive that."""

    def call(self, budget):
        import json
        import os
        import subprocess
        env = dict(os.environ, OPENFILE_MOUNT=str(self.drive), OPENHOME_DEVICE_HOME=str(self.home),
                   LOCAL_CAPABILITIES_DIR=str(self.caps), OPENFILE_STATE=str(self.tmp / "state.json"),
                   OPENFILE_LOCK=str(self.tmp / "lock"), OPENFILE_PLAIN_DIR="1", OPENFILE_CHIMES="0",
                   OPENFILE_CALL_BUDGET=str(budget))
        out = subprocess.run([sys.executable, str(PACKAGE_DIR / "devkit_functions.py"), "sync_drive"],
                             env=env, capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_sync_that_fits_the_budget_reports_its_result(self):
        self.ability(self.drive / "abilities", "quick")
        reply = self.call(budget=10)
        self.assertTrue(reply["success"])
        self.assertEqual(reply["data"]["abilities_synced"], 1)
        self.assertIn("1 ability updated", reply["spoken_response"])

    def test_a_sync_that_outlasts_the_budget_still_finishes(self):
        self.ability(self.drive / "abilities", "slow")
        reply = self.call(budget=0)
        self.assertTrue(reply["success"])
        self.assertTrue(reply["data"]["running"])
        target = self.caps / "slow" / "devkit_functions.py"
        deadline = time.time() + 10
        while time.time() < deadline and not target.is_file():
            time.sleep(0.1)
        self.assertTrue(target.is_file(), "the detached sync must complete after the caller has gone")
        # Let the detached child finish before the sandbox is removed, or its last log line re-creates it.
        while time.time() < deadline + 10:
            try:
                os.kill(reply["data"]["pid"], 0)
            except OSError:
                break
            time.sleep(0.1)


class TestHermetic(Sandbox):
    def test_defaults_point_nowhere_during_tests(self):
        import _sandbox
        for value in (df.DEVICE_HOME, df.CAPS_DIR, df.DRIVE_MOUNT, df.DRIVE_IMG, df.USB_EXTERNAL_MOUNT):
            self.assertTrue(value.startswith(_sandbox.ROOT + "/absent/"), value)
            self.assertFalse(os.path.exists(value), value)
        for live in ("/home/openhome", "/mnt/openfile", "/var/lib/openhome", "/run/"):
            for value in (df.STATE_FILE, df.LOCK_FILE, df.PID_FILE):
                self.assertFalse(value.startswith(live), value)
        self.assertFalse(df.PIP_ENABLED)

    def test_requirements_are_stamped_only_after_a_successful_install(self):
        folder = self.ability(self.caps, "needs", requirements__txt="somepackage==1.0\n")
        req, stamp = folder / "requirements.txt", folder / ".requirements.hash"
        with mock.patch.object(df, "PIP_ENABLED", True):
            for returncode, stamped in ((1, False), (0, True)):
                done = mock.Mock(returncode=returncode, stdout="", stderr="boom")
                with mock.patch.object(df.subprocess, "run", return_value=done) as run:
                    error = self.engine.install_requirements(req, "needs")
                self.assertEqual(bool(error), returncode != 0)
                self.assertEqual(stamp.exists(), stamped)
                self.assertIn("pip", run.call_args[0][0])
            with mock.patch.object(df.subprocess, "run") as run:
                self.engine.install_requirements(req, "needs")
            run.assert_not_called()  # unchanged requirements are not reinstalled

    def test_tests_cannot_see_or_mount_a_real_flash_drive(self):
        """A root test run once mounted the owner's real drive inside a folder that gets deleted."""
        self.assertFalse(df.USB_SCAN_ENABLED)
        with mock.patch.object(df.glob, "glob", return_value=["/sys/block/sda"]) as scan, \
                mock.patch.object(df.subprocess, "run") as run:
            self.assertEqual(self.engine.detect_external_usb_devices(), [])
            self.assertFalse(self.engine.sync_external_usb()["success"])
            watcher = df.DriveWatcher(drive_path=self.drive, caps_dir=self.caps, device_home=str(self.home))
            with mock.patch.object(df, "is_drive_mounted", return_value=False):
                watcher.check_once()
        scan.assert_not_called()
        self.assertFalse(any("mount" in str(call) for call in run.call_args_list))

    def test_cleanup_refuses_to_delete_across_a_mount(self):
        import _sandbox
        victim = self.tmp / "holder"
        (victim / "drive").mkdir(parents=True)
        (victim / "drive" / "precious.txt").write_text("keep me\n")
        with mock.patch.object(_sandbox.os.path, "ismount", side_effect=lambda p: p.endswith("/drive")):
            self.assertFalse(_sandbox.remove_tree_without_crossing_mounts(str(victim)))
        self.assertEqual((victim / "drive" / "precious.txt").read_text(), "keep me\n")
        self.assertTrue(_sandbox.remove_tree_without_crossing_mounts(str(victim)))
        self.assertFalse(victim.exists())

    def test_pip_is_never_run_while_installs_are_off(self):
        folder = self.ability(self.caps, "needs", requirements__txt="somepackage==1.0\n")
        with mock.patch.object(df.subprocess, "run") as run:
            self.assertEqual(self.engine.install_requirements(folder / "requirements.txt", "needs"), "")
        run.assert_not_called()


class TestWifi(Sandbox):
    def test_password_is_erased_whether_or_not_the_join_works(self):
        wifi = self.drive / "config" / "wifi.txt"
        wifi.parent.mkdir(parents=True)
        for returncode in (0, 1):
            wifi.write_text("SSID=Cafe\nPASSWORD=hunter2\n")
            done = mock.Mock(returncode=returncode, stdout="", stderr="no")
            with mock.patch.object(df.shutil, "which", return_value="/usr/bin/nmcli"), \
                    mock.patch.object(df, "active_wifi_ssid", return_value="Home"), \
                    mock.patch.object(df.subprocess, "run", return_value=done) as run:
                ok, _ = self.engine.sync_wifi()
            self.assertEqual(ok, returncode == 0)
            self.assertIn("hunter2", run.call_args[0][0])
            self.assertNotIn("hunter2", wifi.read_text())
            self.assertIn("SSID=Cafe", wifi.read_text())
            self.assertNotIn("hunter2", self.engine.log_file.read_text())


class TestInbox(Sandbox):
    def test_every_volume_has_an_inbox(self):
        self.engine.run_full_sync()
        self.assertTrue((self.drive / "inbox").is_dir())

    def test_a_file_is_installed_only_after_it_stops_growing(self):
        watcher = df.DriveWatcher(drive_path=self.drive, caps_dir=self.caps, device_home=str(self.home))
        inbox = self.drive / "inbox"
        inbox.mkdir()
        (inbox / "beam.py").write_text("x = 1\n")
        (inbox / "bad.py").write_text("def (:\n")
        (inbox / "photo.jpg").write_text("not code")
        (inbox / "._beam.py").write_text("a scrap file a Mac left beside the file it copied")

        self.assertEqual(watcher.process_inbox(), 0, "first sight only records the size")
        self.assertEqual(watcher.process_inbox(), 2)
        self.assertTrue((self.drive / "abilities" / "beam" / "devkit_functions.py").is_file())
        self.assertTrue((inbox / "installed" / "beam.py").is_file())
        self.assertTrue((inbox / "refused" / "bad.py").is_file())
        self.assertTrue((inbox / "photo.jpg").is_file())
        self.assertTrue((inbox / "._beam.py").is_file(), "scrap files are not uploads")
        self.assertFalse((inbox / "refused" / "._beam.py").exists())


class TestLock(Sandbox):
    def test_a_sync_waits_its_turn_and_then_runs(self):
        """A request made while another sync holds the lock must be carried out, not dropped."""
        self.ability(self.drive / "abilities", "queued")
        ready = multiprocessing.Queue()
        holder = multiprocessing.Process(target=hold_lock, args=(df.LOCK_FILE, 1.5, ready))
        holder.start()
        try:
            self.assertTrue(ready.get(timeout=5))
            self.assertTrue(self.engine.run_full_sync(wait_seconds=0)["skipped"], "no patience: steps aside")
            self.assertFalse((self.caps / "queued").exists())
            started = time.monotonic()
            result = self.engine.run_full_sync(wait_seconds=10)
            waited = time.monotonic() - started
        finally:
            holder.join()
        self.assertFalse(result["skipped"])
        self.assertGreater(waited, 0.5, "it should have had to wait for the holder")
        self.assertTrue((self.caps / "queued" / "devkit_functions.py").is_file())

    def test_the_watcher_pulls_device_updates_on_its_periodic_pass(self):
        device_file = self.ability(self.caps, "deployed", body="version = 1\n") / "devkit_functions.py"
        watcher = df.DriveWatcher(drive_path=self.drive, caps_dir=self.caps, device_home=str(self.home))
        with mock.patch.object(df, "is_drive_mounted", return_value=True), \
                mock.patch.object(df, "RESYNC_SECONDS", 0.2):
            watcher.check_once()                                # startup pass seeds the volume
            drive_file = self.drive / "abilities" / "deployed" / "devkit_functions.py"
            self.assertEqual(drive_file.read_text(), "version = 1\n")
            watcher.check_once()                                # absorb the seeding writes
            time.sleep(1.2)
            watcher.check_once()
            device_file.write_text("version = 2\n")            # nothing on the volume changes
            time.sleep(0.3)
            for _ in range(3):
                watcher.check_once()
                time.sleep(0.25)
        self.assertEqual(drive_file.read_text(), "version = 2\n")


if __name__ == "__main__":
    unittest.main()

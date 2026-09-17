#!/usr/bin/env python3
"""Comprehensive test suite for OpenFile capability."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401  must come before devkit_functions
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE_DIR = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(PACKAGE_DIR))

import devkit_functions
from devkit_functions import scaffold_drive_tree, DriveSyncEngine


class TestDriveSkill(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="openfile_test_")
        self.root_path = pathlib.Path(self.temp_dir)
        self.mock_drive = self.root_path / "mock_drive"
        self.mock_caps = self.root_path / "mock_caps"
        self.mock_home = self.root_path / "mock_home"

        self.mock_drive.mkdir(parents=True)
        self.mock_caps.mkdir(parents=True)
        self.mock_home.mkdir(parents=True)
        # Each test gets its own manifest and lock. A shared one carries what
        # an earlier test synced into this one.
        self.patches = [mock.patch.object(devkit_functions, "SNAPSHOT_DIR", str(self.root_path / "snapshots")),
                        mock.patch.object(devkit_functions, "STATE_FILE", str(self.root_path / "state.json")),
                        mock.patch.object(devkit_functions, "LOCK_FILE", str(self.root_path / "lock"))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_json_validity(self):
        """Verify config.json meets OpenHome capability standards."""
        config_path = PACKAGE_DIR / "config.json"
        self.assertTrue(config_path.exists(), "config.json must exist")
        data = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(data.get("category"), "local")
        self.assertEqual(data.get("unique_name"), "openfile")
        self.assertEqual(data.get("name"), "OpenFile")
        self.assertIsInstance(data.get("matching_hotwords"), list)
        self.assertGreater(len(data["matching_hotwords"]), 0)

    def test_scaffold_generation(self):
        """Verify drive_scaffold creates clean required directories."""
        scaffold_drive_tree(self.mock_drive, caps_dir=str(self.mock_caps), device_home=str(self.mock_home))

        # Check required directories
        for sub in ["abilities", "agents", "sounds", "config", "logs"]:
            self.assertTrue((self.mock_drive / sub).is_dir(), f"Missing directory: {sub}")

    def test_syntax_validation(self):
        """Test Python validation detects syntax errors before sync."""
        engine = DriveSyncEngine(self.mock_drive, self.mock_caps, device_home=str(self.mock_home))

        # Valid file
        valid_file = self.root_path / "valid.py"
        valid_file.write_text("def hello(): return 42\n")
        ok, _ = engine.validate_python_file(valid_file)
        self.assertTrue(ok)

        # Corrupted syntax file
        broken_file = self.root_path / "broken.py"
        broken_file.write_text("def broken_syntax(:\n")
        ok, reason = engine.validate_python_file(broken_file)
        self.assertFalse(ok)
        self.assertIn("SyntaxError", reason)

    def test_sync_engine_stages_valid_ability(self):
        """Test sync_abilities copies valid abilities and rejects broken ones."""
        # Add a valid ability
        good_ability = self.mock_drive / "abilities" / "good_skill"
        good_ability.mkdir(parents=True)
        (good_ability / "devkit_functions.py").write_text("def run(): return 42\n")

        # Add a broken ability
        bad_ability = self.mock_drive / "abilities" / "bad_skill"
        bad_ability.mkdir(parents=True)
        (bad_ability / "devkit_functions.py").write_text("this is completely invalid python syntax :::")

        engine = DriveSyncEngine(self.mock_drive, self.mock_caps, device_home=str(self.mock_home))
        res = engine.sync_abilities()

        # The valid good_skill should sync; bad_skill should be rejected
        self.assertEqual(res["synced"], 1)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("bad_skill", res["errors"][0])

        # Verify good_skill landed in caps directory
        staged_good = self.mock_caps / "good_skill" / "devkit_functions.py"
        self.assertTrue(staged_good.exists())

        # Verify bad_skill did not land in caps directory
        staged_bad = self.mock_caps / "bad_skill"
        self.assertFalse(staged_bad.exists())

    def test_settings_sync(self):
        """Test updating settings.env writes to destination .env."""
        os.environ["OPENHOME_DEVICE_HOME"] = str(self.mock_home)
        dest_env = self.mock_home / ".env"
        dest_env.write_text("MIC_SENSITIVITY=30\nSPEAKER_VOLUME=40\n")

        config_dir = self.mock_drive / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "settings.env").write_text("MIC_SENSITIVITY=160\nSPEAKER_VOLUME=60\n")

        engine = DriveSyncEngine(self.mock_drive, self.mock_caps, device_home=str(self.mock_home))
        changed = engine.sync_settings()
        self.assertTrue(changed)

        updated_text = dest_env.read_text()
        self.assertIn("MIC_SENSITIVITY=160", updated_text)
        self.assertIn("SPEAKER_VOLUME=60", updated_text)

    def test_devkit_functions_cli(self):
        """Test running devkit_functions.py directly via CLI produces valid JSON."""
        # Test status call
        proc = subprocess.run(
            [sys.executable, str(PACKAGE_DIR / "devkit_functions.py"), "health"],
            capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        output = json.loads(proc.stdout.strip())
        self.assertTrue(output.get("success"))
        self.assertIn("spoken_response", output)
        self.assertIn("data", output)

    def test_main_py_contract(self):
        """Verify main.py has required OpenHome capability registration tag."""
        main_path = PACKAGE_DIR / "main.py"
        content = main_path.read_text(encoding="utf-8")
        self.assertIn("#{{register capability}}", content)
        self.assertIn("resume_normal_flow", content)
        self.assertIn("MatchingCapability", content)

    def test_drive_watcher_debounced_sync(self):
        """Test DriveWatcher debounce window and change detection."""
        from devkit_functions import DriveWatcher
        scaffold_drive_tree(self.mock_drive, caps_dir=str(self.mock_caps), device_home=str(self.mock_home))

        watcher = DriveWatcher(
            drive_path=self.mock_drive,
            caps_dir=self.mock_caps,
            device_home=str(self.mock_home),
            debounce_seconds=0.2,
            poll_interval=0.05
        )

        # Monkey-patch is_drive_mounted so it treats mock_drive as mounted
        import devkit_functions
        orig_mounted = devkit_functions.is_drive_mounted
        devkit_functions.is_drive_mounted = lambda: True

        try:
            # First snapshot initialization
            watcher.check_once()
            self.assertIsNone(watcher._dirty_since)

            # Create a new ability file
            new_skill = self.mock_drive / "abilities" / "new_skill"
            new_skill.mkdir(parents=True)
            (new_skill / "devkit_functions.py").write_text("print('hello')\n")
            (new_skill / "config.json").write_text("{}")

            # First check detects change, marks dirty
            synced = watcher.check_once()
            self.assertFalse(synced)
            self.assertIsNotNone(watcher._dirty_since)

            # Check immediately before debounce period: should not sync yet
            synced = watcher.check_once()
            self.assertFalse(synced)

            # Wait for debounce window to expire
            import time
            time.sleep(0.25)

            # Check again: should trigger debounced sync
            synced = watcher.check_once()
            self.assertTrue(synced)
            self.assertIsNone(watcher._dirty_since)

            # Staged file should now exist in mock_caps
            self.assertTrue((self.mock_caps / "new_skill" / "devkit_functions.py").exists())
        finally:
            devkit_functions.is_drive_mounted = orig_mounted

    def test_autopopulate_from_device(self):
        """Verify existing abilities, sounds, and settings auto-populate onto the drive."""
        from devkit_functions import autopopulate_from_device

        # Create an existing ability on the mock device
        existing_ability = self.mock_caps / "existing_weather_skill"
        existing_ability.mkdir(parents=True)
        (existing_ability / "devkit_functions.py").write_text("def weather(): return 'sunny'\n")
        (existing_ability / "config.json").write_text('{"name": "Weather"}')

        # Create an existing audio file on mock device
        audio_dir = self.mock_home / "openhome_devkit" / "audio_files"
        audio_dir.mkdir(parents=True)
        (audio_dir / "custom_chime.wav").write_bytes(b"RIFFdummywavdata")

        # Create mock ~/.env
        dest_env = self.mock_home / ".env"
        dest_env.write_text("MIC_SENSITIVITY=180\nSPEAKER_VOLUME=75\nAUTOMATIC_LEDS_ON=true\n")

        # Run autopopulate onto mock_drive
        stats = autopopulate_from_device(
            root_path=self.mock_drive,
            caps_dir=str(self.mock_caps),
            device_home=str(self.mock_home)
        )

        # Assertions
        self.assertGreaterEqual(stats["abilities_copied"], 1)
        self.assertTrue((self.mock_drive / "abilities" / "existing_weather_skill" / "devkit_functions.py").exists())
        self.assertTrue((self.mock_drive / "sounds" / "custom_chime.wav").exists())
        self.assertTrue((self.mock_drive / "config" / "settings.env").exists())

        settings_text = (self.mock_drive / "config" / "settings.env").read_text()
        self.assertIn("MIC_SENSITIVITY=180", settings_text)
        self.assertIn("SPEAKER_VOLUME=75", settings_text)


if __name__ == "__main__":
    unittest.main()

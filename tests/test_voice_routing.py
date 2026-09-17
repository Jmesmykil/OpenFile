#!/usr/bin/env python3
"""What the ability does with what it hears."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402,F401  must come before devkit_functions
import importlib.util
import json
import pathlib
import sys
import types
import unittest

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent


def load_main():
    """Import main.py with stand-ins for the OpenHome runtime it is loaded into."""
    for name, attrs in {
        "src": {}, "src.agent": {},
        "src.agent.capability": {"MatchingCapability": type("MatchingCapability", (), {})},
        "src.main": {"AgentWorker": type("AgentWorker", (), {})},
        "src.agent.capability_worker": {"CapabilityWorker": type("CapabilityWorker", (), {})},
    }.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules.setdefault(name, module)
    spec = importlib.util.spec_from_file_location("openfile_main", PACKAGE_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAIN = load_main()

EXPECTED = {
    # turning the volume off must never be read as turning it on
    "unmount drive": "disable_drive",
    "disconnect the drive": "disable_drive",
    "open file eject": "disable_drive",
    "turn off drive": "disable_drive",
    "turn on drive": "enable_drive",
    "mount the drive": "enable_drive",
    # the safety net has to be reachable by voice, and nothing else may be mistaken for it
    "open file undo": "undo_last_change",
    "undo that": "undo_last_change",
    "roll back my last change": "undo_last_change",
    "open file history": "show_history",
    "what changed": "show_history",
    # the three words asked for by name
    "open file reseed": "reseed_drive",
    "reseed the drive": "reseed_drive",
    "open file refresh": "sync_drive",
    "refresh abilities": "sync_drive",
    "reload abilities": "sync_drive",
    "open file restart": "restart_services",
    "restart open file": "restart_services",
    # "restart" contains "start", "ports" sits inside "reports"
    "restart": "restart_services",
    "sync drive": "sync_drive",
    "check ports": "get_ports",
    "open file ports": "get_ports",
    "drive status": "get_status",
    "open file status": "get_status",
    "eject drive": "disable_drive",
    "how much space is free on the drive": "get_status",
    "open file health": "health",
    "sync flash drive": "sync_usb",
    "eject flash drive": "eject_usb",
    "eject usb": "eject_usb",
    # nothing recognisable: report, never change anything
    "open file": "get_status",
    "": "get_status",
    "Open File, REFRESH!": "sync_drive",
}


class TestVoiceRouting(unittest.TestCase):
    def test_phrases_reach_the_right_function(self):
        for phrase, action in EXPECTED.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(MAIN.resolve_action(phrase), action)

    def test_every_action_exists_on_the_device_side(self):
        sys.path.insert(0, str(PACKAGE_DIR))
        import devkit_functions
        routed = {action for action, _, _ in MAIN.INTENT_RULES} | {"sync_usb", "eject_usb", "get_status"}
        self.assertTrue(routed, "no routes were collected")
        self.assertEqual(routed - set(devkit_functions.FUNCTION_REGISTRY), set())

    def test_every_hotword_routes_somewhere_deliberate(self):
        hotwords = json.loads((PACKAGE_DIR / "config.json").read_text())["matching_hotwords"]
        self.assertGreater(len(hotwords), 5)
        bare = {"open file", "openfile"}
        for phrase in hotwords:
            with self.subTest(hotword=phrase):
                if phrase not in bare:
                    self.assertIn(phrase, EXPECTED, "hotword has no routing expectation in this test")


if __name__ == "__main__":
    unittest.main()

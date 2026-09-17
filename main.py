import json
import re
from src.agent.capability import MatchingCapability
from src.main import AgentWorker
from src.agent.capability_worker import CapabilityWorker

# Checked top to bottom, on whole words. Order matters: "unmount" must never
# reach the rule for "mount", and a flash drive request must not be read as a
# request about the built-in volume.
FLASH_WORDS = {"usb", "flash", "thumb", "stick"}
EJECT_WORDS = {"eject", "unmount", "disconnect", "disable", "remove", "stop"}
INTENT_RULES = (
    ("undo_last_change", {"undo", "revert", "rollback"}, ("roll back", "take that back", "go back")),
    ("show_history", {"history"}, ("what changed", "what did i change")),
    ("reseed_drive", {"reseed", "repopulate", "rebuild", "restore"}, ("start over", "from scratch")),
    ("restart_services", {"restart", "reboot", "relaunch"}, ()),
    ("get_ports", {"port", "ports", "plugged", "hardware", "interfaces"}, ()),
    ("disable_drive", EJECT_WORDS, ("turn off", "switch off", "shut off")),
    ("get_status", {"status", "space", "free", "info", "storage"}, ("how much", "how many")),
    ("health", {"health", "healthy", "diagnostic", "diagnostics", "check"}, ()),
    ("enable_drive", {"enable", "mount", "connect", "start"}, ("turn on", "switch on")),
    ("sync_drive", {"refresh", "reload", "rescan", "sync", "update", "apply"}, ()),
)


def resolve_action(transcript: str) -> str:
    """Map what was said to a devkit function name."""
    text = re.sub(r"[^a-z0-9 ]+", " ", (transcript or "").lower())
    words = set(text.split())
    padded = f" {' '.join(text.split())} "

    if words & FLASH_WORDS:
        return "eject_usb" if words & EJECT_WORDS else "sync_usb"
    for action, keywords, phrases in INTENT_RULES:
        if words & keywords or any(f" {p} " in padded for p in phrases):
            return action
    return "get_status"


class OpenFileCapability(MatchingCapability):
    """OpenFile Capability.

    Voice control for the DevKit's modding volume: turn it on or off, apply
    edits, reseed it from the device, restart its services, and report state.
    """

    worker: AgentWorker = None
    capability_worker: CapabilityWorker = None

    #{{register capability}}  # noqa: E265 (OpenHome requires this line verbatim)

    async def handle_drive_action(self):
        try:
            transcript = await self.capability_worker.wait_for_complete_transcription()
            if not transcript or not transcript.strip():
                return

            result = await self.capability_worker.send_devkit_capability_action(
                function_name=resolve_action(transcript),
                args=[],
                timeout=15,  # the DevKit ends a capability call at 15 seconds
            )

            spoken = self._spoken_response_from_result(result)
            if spoken:
                await self.capability_worker.speak(spoken)

        except Exception as error:
            if self.worker and hasattr(self.worker, "editor_logging_handler"):
                self.worker.editor_logging_handler.error(f"OpenFile error: {error}")
        finally:
            self.capability_worker.resume_normal_flow()

    def _spoken_response_from_result(self, result):
        if not isinstance(result, dict):
            return "Open file did not get an answer from the device."
        output = (result.get("output") or "").strip()
        try:
            payload = json.loads(output) if output else {}
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("spoken_response"):
            return payload["spoken_response"]
        if not result.get("success"):
            return "Open file could not finish that."
        return ""

    def call(self, worker: AgentWorker):
        self.worker = worker
        self.capability_worker = CapabilityWorker(self)
        self.worker.session_tasks.create(self.handle_drive_action())

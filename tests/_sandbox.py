"""Imported first by every test. Points the code at a private, empty place.

On a DevKit the default paths are the live abilities folder, the live .env
and the live volume. A test that forgets to pass a path must find nothing
there, and must never be able to install a package.

The place is a fresh temporary folder, removed when the run ends. A fixed
path would not do: root can create it, and a sync manifest left behind by
one run changes what the next run sees.
"""
import atexit
import os
import shutil
import tempfile

ROOT = tempfile.mkdtemp(prefix="openfile_unit_")


def remove_tree_without_crossing_mounts(path: str) -> bool:
    """Delete a temporary folder, unless anything is mounted inside it.

    A recursive delete follows a mount point into whatever is mounted there.
    If a drive ever ends up mounted under a test folder, the only safe move is
    to leave the folder alone and say so.
    """
    real = os.path.realpath(path)
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts:
            inside = [line.split()[1] for line in mounts if line.split()[1].startswith(real + "/")]
    except OSError:
        inside = []
    for root, dirs, _ in os.walk(real):
        inside.extend(os.path.join(root, d) for d in dirs if os.path.ismount(os.path.join(root, d)))
    if inside:
        print(f"NOT removing {real}: something is mounted inside it: {sorted(set(inside))}")
        return False
    shutil.rmtree(real, ignore_errors=True)
    return True


atexit.register(remove_tree_without_crossing_mounts, ROOT)

os.environ.update({
    "OPENHOME_DEVICE_HOME": ROOT + "/absent/home",
    "LOCAL_CAPABILITIES_DIR": ROOT + "/absent/caps",
    "OPENFILE_MOUNT": ROOT + "/absent/volume",
    "OPENFILE_IMG": ROOT + "/absent/volume.img",
    "OPENFILE_USB_MOUNT": ROOT + "/absent/usb",
    "OPENFILE_STATE": ROOT + "/state.json",
    "OPENFILE_SNAPSHOTS": ROOT + "/snapshots",
    "OPENFILE_LOCK": ROOT + "/lock",
    "OPENFILE_PID": ROOT + "/pid",
    "OPENFILE_PIP": "0",
    "OPENFILE_USB_SCAN": "0",
    "OPENFILE_CHIMES": "0",
    "OPENFILE_AUDIO": "0",
})

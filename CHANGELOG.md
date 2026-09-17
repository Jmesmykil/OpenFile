# Changelog

## 0.2.0

- The drive announces itself, so it appears under Network on macOS, Windows and Linux without an address being typed.
- Copies from Finder work. They used to arrive empty, because the volume could not store the extra data macOS attaches to files.
- Opens as Guest or as `openhome` with the published default password `admin`. `openfile password` sets your own and turns guests away.
- The web page is optional, installed with `--web`.
- Scrap files left by desktops (`._name`, `.DS_Store`, `Thumbs.db`) are swept from the volume.
- Undo. The running version of every file is saved before it is replaced, and one command by voice, command line or web page puts it back. Settings included.
- Settings are range checked. JSON is checked like Python. A failed Wi-Fi join returns to the previous network.
- OpenFile's own folder is protected from edits made on the volume.
- Guidance files on the volume, confirmations on the web page, and a "Needs attention" list.
- Old backups and snapshots are trimmed. The package list is saved before each install.
- Secrets stay on the device. The volume receives six tuning keys and nothing else.
- The installer shares the modding volume only. Developer shares are opt-in and need a login.
- Sync runs both ways with a manifest, so an update deployed to the device is kept.
- New commands by voice, command line and web page: reseed, refresh, restart.
- Web page rewritten: refuses cross-site and foreign-host requests, checks uploads before writing them.
- Symbolic links inside ability folders are never followed or written through.
- The watcher applies edits made while it was down, and overlapping syncs are serialised.
- Voice routing matches whole words.
- Device facts are read from the running system.
- The Bluetooth receiver and the USB-C gadget are removed: neither could be shown to work on the DevKit.
- One sandboxed on-device stress test replaces the earlier hardware suites.
- An ability removed on the device has its volume copy moved to `backup/`.
- Voice and command line syncs finish on their own when they outlast the 15 second call limit.
- Tests cannot reach live paths or install packages.
- Tests cannot see or mount a real flash drive, and test cleanup never deletes across a mount point.
- The web page listens on IPv4 and IPv6.
- Whatever is deleted on the volume stays deleted until a reseed. Emptied folders are pruned.
- A read-only flash drive is read and left alone.
- A sync that finds another one running waits its turn. The watcher also checks the device once a minute.

## 0.1.0

- First version: FAT volume image, USB gadget, sync watcher, voice commands.

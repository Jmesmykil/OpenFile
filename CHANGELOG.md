# Changelog

## 0.4.0

Made to meet OpenHome's rules for local abilities.

- Installing from the OpenHome marketplace works on a stock DevKit. OpenHome runs a copy of the ability's file from its own folder, and the first "turn on drive" used to set up there instead of in the ability's folder. OpenFile now finds its own folder under the local abilities directory.
- The files downloaded on the first "turn on drive" are a release asset whose SHA-256 is pinned in `devkit_functions.py`. A download that does not match is refused and nothing is installed. The archive can never replace `devkit_functions.py`.
- "open file, uninstall" asks first, then removes the services and the share. The volume stays. The services also stop by themselves once the ability folder is gone.
- The speaker and microphone limits follow OpenHome's own controls, 80 and 100. `OPENFILE_EXTENDED_LEVELS=1` in `/etc/default/openfile` allows 100 and 200 on a device tuned past that.
- `config/device_info.json` no longer holds the agent id, the Wi-Fi name or the network hardware addresses.
- A flash drive is mounted with `nosuid,nodev,noexec`.
- Turning the drive off, undo and a flash drive sync answer within OpenHome's 15 second limit, or say they are still working and finish on their own.
- Every trigger phrase starts with "open file", apart from the two flash drive ones, so OpenFile does not answer something meant for another ability.
- `make package` builds the six files OpenHome accepts for an upload. The code passes OpenHome's lint settings.

## 0.3.2

- A settings file outside its owner's home, such as the stress suite's sandbox, no longer moves the live speaker or microphone. In 0.3.1 the stress suite's copy set the real speaker to its own level.

## 0.3.1

- Mic sensitivity and speaker volume take effect as soon as you change them. They were written to the device's settings file, but OpenHome only reads that file when it boots, so a new level waited for the next restart.

## 0.3.0

- Installs through OpenHome. When the ability arrives from the marketplace or `openhome deploy`, the first "open file, turn on drive" fetches this release from GitHub and runs the installer on the device.

## 0.2.3

- README: the share has now been opened and written to from a Linux machine, and says so.

## 0.2.2

- `install.sh --ethernet` switches on the DevKit's wired port, which its firmware ships switched off. Tested over a cable.

## 0.2.1

- README: the flash drive path has now been tried end to end on hardware, and says so.

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

# OpenFile

OpenFile gives the OpenHome DevKit a modding volume. Your abilities, sounds and device settings show up as ordinary files on your computer or phone. You edit them there, and the DevKit applies the change. No SSH and no deploy step for device code.

The idea comes from console homebrew. The PSP and Switch scenes took off once people had a folder on the device that they could open and change. The DevKit is a Raspberry Pi running Linux, so the same thing is possible here.

## What you get

A volume named `OPENFILE` with this layout:

```text
OPENFILE/
├── abilities/      one folder per ability, copied from the device
├── sounds/         the chimes and tones the device plays
├── config/
│   ├── settings.env       speaker, microphone and interrupt levels
│   ├── wifi.txt           move the device to another network
│   └── device_info.json   read-only report, rewritten on every sync
├── agents/         your prompt drafts and notes (see "Agents" below)
├── inbox/          drop a .py or .zip here and it gets installed
├── logs/           sync_history.log
└── backup/         anything a reseed or a conflict replaced
```

Everything in `abilities/` and `sounds/` is copied from your own device the first time the volume turns on. Nothing in it is sample content.

## Ways in

Use whichever one suits the machine in front of you. They all lead to the same files.

| Way in | Works from | What you do |
|---|---|---|
| Network share | macOS, Windows, Linux, iOS, Android | It shows up by itself under Network. Open it as Guest, or as `openhome` with the password `admin` |
| Web page | anything with a browser | Optional, installed with `--web`. Open `http://openhome.local:8088` and drop a file on it. Undo lives here too |
| Flash drive | anything that can write a USB stick | Put ability folders on the stick, plug it into a USB-A port on the DevKit |

### Connecting to the network share

The DevKit announces the drive on your network, so you should not have to type an address. Look for **openhome OpenFile** under Network in Finder, File Explorer or your Linux file manager, and open it.

If it does not appear, or you would rather connect by address:

- **macOS:** Finder, Go, Connect to Server, `smb://openhome:admin@openhome.local/OpenFile`. With the login in the address, macOS does not stop to ask.
- **Windows:** type `\\openhome.local\OpenFile` into the File Explorer address bar.
- **Linux:** enter `smb://openhome.local/OpenFile` in your file manager.
- **iOS and iPadOS:** Files app, the `...` menu, Connect to Server, `smb://openhome.local`, Guest.
- **Android:** any file manager with SMB support, host `openhome.local`, guest access.

### Logging in

Out of the box the drive opens two ways, and you can use whichever your computer offers:

- as **Guest**, with nothing typed;
- as the DevKit account, **`openhome`**, with the password **`admin`**.

The default password is published here on purpose. Anyone on your network can already get in as a guest, so a known password takes nothing away, and it gives Macs and Windows a named login. Macs will not send an empty password for a named account, and recent Windows 11 releases refuse guests, so on those the `openhome` login is the one that works.

On a Mac, mistyping the password connects you as a guest instead, for as long as guests are allowed. Once you set your own password, a wrong one is refused.

To choose your own password, run this on the DevKit:

```bash
openfile password
```

From then on the share asks for `openhome` and that password, and guests are turned away. `openfile password --remove` goes back to the default. This is the share's own password. It has nothing to do with the DevKit account's real password, so it changes nothing about SSH.

## Install

There are two ways. Through OpenHome is the one most people want.

### Through OpenHome

Install OpenFile from the OpenHome marketplace onto your agent, or deploy it with `openhome deploy`. OpenHome puts the ability's files on your DevKit. Then say:

> open file, turn on drive

The first time, the DevKit fetches the rest of this release from GitHub, sets itself up, and says so. Give it a minute, then say "open file, status". From then on the drive is on at every boot. The DevKit needs internet for that first step only.

### From this repository

For working on OpenFile itself. You need a DevKit you can reach over SSH.

```bash
git clone https://github.com/Jmesmykil/OpenFile.git
cd OpenFile
make install
```

`make install` copies the ability to the DevKit and runs `install.sh` there. If you would rather do it by hand, copy this folder to `~/openhome_devkit/local_capabilities/openfile` on the DevKit and run `sudo ./install.sh`.

Options, passed as `make install INSTALL_FLAGS="..."` or straight to `install.sh`:

| Flag | Effect |
|---|---|
| `--web` | Also serves a web page on port 8088: drop files on it, press Undo, see warnings |
| `--set-password` | Same as `openfile password`: the share asks for a password you choose, and guests are turned away |
| `--no-password` | Back to the default: guests in, `openhome` with `admin` |
| `--dev-shares` | Adds two more shares, `Abilities-Live` and `DevKit-Home`. These need a share password, and you are asked to set one |
| `--ethernet` | Switches on the wired port. The DevKit firmware ships with it switched off, Wi-Fi only. With this, a cable works alongside Wi-Fi |
| `--uninstall` | Removes the services and shares. Your volume image is kept |

To register the voice commands with your OpenHome account, run `openhome deploy` from this folder.

## Voice commands

Say "open file", then one of these.

| You say | What happens |
|---|---|
| "undo" | Puts back the version that was running before your last change |
| "history" | Says how many changes can still be undone |
| "refresh" | Applies your edits now instead of waiting for the watcher |
| "reseed" | Rebuilds the volume from what is on the device. Whatever it replaces is moved to `backup/` |
| "restart" | Restarts the OpenFile services |
| "status" | Free space and how many abilities are on the volume |
| "ports" | What is plugged into the DevKit and which network links are up |
| "turn on drive" / "turn off drive" | Mounts or unmounts the volume |
| "sync flash drive" / "eject flash drive" | Reads or releases a stick in a USB-A port |
| "health" | Checks the installation |

The same actions are on the command line as `openfile status`, `openfile refresh`, `openfile reseed`, `openfile restart` and so on. Run `openfile help` for the full list.

## How an edit reaches the device

1. A watcher looks at the volume once a second. When files change it waits for two quiet seconds, so a large folder is copied over completely before anything reads it.
2. Every Python file that is about to be sent to the device is parsed first. If one file in an ability fails to parse, nothing from that ability is sent. The reason is written to `logs/sync_history.log` and the version already on the device keeps running.
3. Changed files are copied into the live abilities folder. If the ability has a `requirements.txt` that changed, the packages are installed.

Sync goes both ways. An edit on the volume is applied within a few seconds. A change made on the device is picked up within a minute, or at once if you say "refresh". OpenFile keeps a manifest of every file as it stood after the last sync, which is how it tells your edit on the volume apart from an update that arrived on the device, for example from `openhome deploy`. Whichever side changed is copied to the other. If both sides changed the same file, your copy on the volume is applied and the device's copy is saved under `backup/` first. On a first run, when there is no manifest yet, the copy running on the device is used and the volume's copy is saved instead.

Two things are deliberate:

- Deleting a file or a whole ability folder on the volume does not delete it from the device, and it stays gone from the volume until you reseed. To remove an ability, remove it on the device. At the next sync the volume's copy moves to `backup/`, so nothing is lost if you change your mind.
- Folders whose names end in `.retired*`, `.disabled`, `.bak` or `.old` are treated as parked copies and are left off the volume.

### What runs where

The DevKit runs `devkit_functions.py`. Edit that file and the next voice command uses your new code.

`main.py` and `config.json`, which hold the trigger phrases and the conversation logic, run on OpenHome's side. OpenFile keeps the device copies in step, but OpenHome only picks up a change to those two files when you run `openhome deploy`.

### Agents

Agent personalities live in your OpenHome account. The DevKit stores only the id of the agent it starts with, which you can read in `config/device_info.json`. There is no agent definition on the device for OpenFile to expose, so `agents/` is yours for prompt drafts and notes. Nothing in it is applied automatically.

## Safety nets

OpenFile is meant to be poked at. The aim is that nothing you do from the volume needs a reinstall to fix, without taking away your freedom to change things.

**Undo.** Before any file of yours replaces one on the device, the version that was running is saved on the device, off the volume. Say "open file, undo", run `openfile undo`, or press Undo on the web page, and it is put back on the device and on the volume. Undo works one change at a time, back through the last 30. It covers abilities and settings. An ability that undo removes is kept in `backup/`.

Undo exists for what a check cannot catch. OpenFile refuses Python that does not parse and JSON that does not load, but code can parse and still do the wrong thing, and usually that is the mistake people actually make.

**What is checked before anything is sent**

| You change | The check | If it fails |
|---|---|---|
| any `.py` in an ability | it must parse | nothing from that ability is sent, the device keeps running what it had |
| any `.json` in an ability | it must load | the same |
| `settings.env` | each value must be in range, for example volume 0 to 100 | that value is not applied, and the file is set back so it shows what the device has |
| `wifi.txt` | the device must manage to join | it returns to the network it was on |

**OpenFile protects itself.** Edits to `abilities/openfile/` are held back and kept in `backup/`, because a mistake there could take away undo. If you are working on OpenFile itself, add `OPENFILE_ALLOW_SELF_EDIT=1` to `/etc/default/openfile` on the device.

**Warnings.** A network share cannot show you a pop-up, so the warnings are where you will look. Each folder has a `READ ME FIRST.txt` naming the files that deserve care. `wifi.txt` warns at the top that a successful join disconnects you. If you installed the web page, it asks before it replaces an installed ability, reseeds, restarts or undoes, and it lists anything that was refused or held back under "Needs attention". By voice, the device tells you when a change was held back.

**What is not protected, on purpose.** You can still write code that misbehaves, install a package that upsets another ability (the package list from before each install is saved in `backup/` as `pip-freeze.txt`), or use the opt-in developer shares to edit live files with no checks at all. Those are yours to do.

## Settings and Wi-Fi

`config/settings.env` carries six keys: `SPEAKER_VOLUME`, `MIC_SENSITIVITY`, `INTERRUPTION_SENSITIVITY`, `AUTO_INTERRUPT`, `INTERACTIVE_INTERRUPT` and `AUTOMATIC_LEDS_ON`. Change a value, save, and the device's `.env` gets that one line updated. Every other line in the device file, comments included, is left completely alone.

To move the DevKit to another network, fill in `SSID` and `PASSWORD` in `config/wifi.txt` and save. The password is erased from the file as soon as the join has been attempted, whether or not it worked.

## Security

OpenFile is built for a home network you trust. With the default install, anyone on that network can read and change the modding volume without a password, and an ability they put there runs as root on the DevKit, because that is how OpenHome runs abilities. If that is more trust than you want to give, run `openfile password` on the DevKit.

What OpenFile does to keep the damage bounded:

- Your account API key and broker password never leave the device. The volume gets the six tuning keys and nothing else. A `settings.env` that carries any other key has that key ignored and logged.
- Only the modding volume is shared by default. The deeper shares are opt-in and cannot be added until the share has a password. OpenFile never shares the root filesystem.
- The web page is off unless you install it. When on, it refuses requests that come from other websites, refuses requests addressed to a host name that is not the device's own, accepts only `.py` and `.zip` up to 20 MB, and checks every archive path before writing anything. Set `OPENFILE_WEB_TOKEN` in `/etc/default/openfile` to require a token as well.
- Uploads, flash drives and files dropped in `inbox/` all go through the same parse check as an edit made on the volume. None of them has a shortcut into the live abilities folder.

## The USB-C port

On the DevKit the USB-C port is the power input, and OpenFile does not use it. A Raspberry Pi 4 can present a drive over that port, but only to the computer that is also powering it, and two systems writing one FAT volume can corrupt it. Earlier builds offered that as an option. It could not be verified on hardware, so it is gone. The flash drive path uses the USB-A ports instead.

## What has been tested

On a DevKit from a Mac and from a Linux machine, over Wi-Fi and over the wired port: the network share appearing under Network and opening as Guest and as `openhome`; setting a password of your own and going back to the default; folders copied in from Finder; the web page; voice routing; sync in both directions; undo; reseed; settings; the installer. The stress test below covers most of that, and the share and web page were also checked from a second computer. From Linux: the share is announced to the file manager, opens as guest and as `openhome`, takes a folder written through the GNOME and KDE file-manager path, and the ability runs on the device. The DevKit answers the discovery probe Windows sends, checked from a Mac and from the device itself, but no Windows, iOS or Android machine has opened the share yet.

A flash drive in a USB-A port has been tried end to end: a read-only installer stick was detected, mounted and left alone; a FAT32 stick got the backup written to it, an ability installed from it, a broken one refused, and a clean eject. The wired port needs `--ethernet`, because the DevKit firmware ships with it switched off; with that, the share and the web page were opened over a cable too.

## Tests

```bash
make test          # unit tests, run anywhere
make validate      # the official OpenHome ability validator
make device-test   # the same unit tests, run on the DevKit
make stress-test   # on the DevKit
```

The stress test runs the shipped code on an actual DevKit, against a sandbox: a temporary copy of the device's abilities, sounds and `.env`, with its own watcher and web page and with chimes turned off. It never writes to your live abilities, your live `.env` or the speaker. Its last phase reads the live install and reports any secret found on the volume and any share that should not exist.

## Layout of this repository

| Path | Purpose |
|---|---|
| `main.py`, `config.json` | The OpenHome ability: trigger phrases and voice routing |
| `devkit_functions.py` | Everything that runs on the device: volume, sync, ports, watcher |
| `device/web_portal.py` | The web page |
| `install.sh` | Installer and uninstaller, run on the DevKit |
| `bin/openfile` | Command line tool |
| `systemd/`, `samba/` | Service and share templates that `install.sh` fills in |
| `tests/` | Unit tests and the on-device stress test |

## License

MIT. Copyright (c) 2026 Jamesmykil Weber.

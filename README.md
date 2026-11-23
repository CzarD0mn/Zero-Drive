# Zero-Drive

Source: https://github.com/CzarD0mn/Zero-Drive

Use the **M5Stack Cardputer Zero** as a USB flash drive. Pick a folder, plug
the Cardputer into a laptop or Android phone, and the folder shows up as a
normal USB stick. Eject it on the computer and every change lands back in the
folder — owned by your user, ready to use on the Cardputer.

## Built-in protections

* **Folders:** only folders inside your home folder, or under `/media` or `/mnt`,
  can be shared. System folders and the home folder itself are refused.
* **Hidden files** (`.ssh`, `.config`, …) never go on the drive and are never
  touched. Junk files a computer leaves behind (`.DS_Store`,
  `System Volume Information`, `.Trashes`…) are never copied back.
* **Newer Cardputer files win:** a file you changed on the Cardputer while the
  drive was shared is never overwritten by an older copy from the computer, or
  deleted because the computer deleted it.

## Install

```sh
sudo apt install ./zero-drive_1.3.0-1_arm64.deb
systemctl --user restart APPLaunch.service   # if the icon doesn't appear yet
```

Zero-Drive appears in the launcher. Open it with Enter.

## Root access

When Zero-Drive opens, it asks for your password once. It keeps the password
in memory until you close the app, so starting and stopping the drive never
asks again. Esc on that screen quits. With passwordless sudo you won't be asked.

## USB mode switches live (no reboot)

The USB controller only goes into **device mode while you're sharing**. On
Debian trixie (Raspberry Pi kernel 6.12, arm64), the `dwc2` and legacy `dwc_otg`
USB drivers are both built into the kernel, so Zero-Drive doesn't load or
unload modules. Instead it:

1. detaches the controller from `dwc2` (sysfs `unbind`),
2. applies a runtime `dtoverlay dwc2 dr_mode=peripheral` (`raspi-utils-dt`),
3. reattaches it, which creates the USB device port the computer sees.

When sharing stops, the runtime overlay is removed and the controller is
reattached in its boot-time host mode: USB-A, Ethernet and the hub work again.
`config.txt` is never touched for this. The helper reads the controller's
effective `dr_mode`:

* `host`: switched to device mode while sharing, then back to host.
* `otg` (a plain `dtoverlay=dwc2`): forced to device mode while sharing, so
  the connection doesn't depend on the ID pin, then back to otg.
* `peripheral` (for example ADB is on): left as it is.

The **physical USB switch** on the left side still has to be on **Slave
(left)** while sharing, because it routes the wires. Software can't move it.

**Needs `dtoverlay=dwc2` at boot.** Without it, the kernel uses the legacy
`dwc_otg` driver, which crashes if it's detached, so it can never switch live.
The Cardputer Zero image normally runs on `dwc2`. If yours doesn't, Zero-Drive
detects that and offers a **one-time** change: it adds `dtoverlay=dwc2,dr_mode=host`,
which keeps host mode, and reboots once. Every switch after that is live.
Check yours with `sudo /usr/share/APPLaunch/apps/zero-drive/zd-helper status`
and look for `usb_driver=dwc2` (`usb_dr_mode` shows the current mode).

## Using it

1. **Folder:** choose the folder to share (default `~/ZeroDrive`). You can make
   a new one in the picker.
2. **Extra space:** how much free room the computer gets for new files
   (1–32 GB, capped by free space on the card).
3. **Format:** exFAT (default, no 4 GB file limit) or FAT32 (for older devices).
4. **Start USB drive:** enter your password when asked, then plug the
   **right-hand USB-C port** into the computer or phone.
5. When you're done, **eject the drive on the computer**. Zero-Drive notices,
   saves the changes and shows "done". You can also press Enter on the
   Cardputer to stop. Pulling the cable also saves, but eject first to be safe.

### Keys

| Key | Action |
|---|---|
| ↑ ↓ (or Fn + ; .) | move |
| ← → (or Fn + , /) | change a value / go up a folder |
| Enter | select |
| Backspace | go up a folder, delete a character |
| Esc | back / quit |

## How it works

* `zerodrive.py` is the screen + keyboard UI (Python, Pillow, DejaVu Sans,
  framebuffer + evdev — the CardputerZero AppBuilder Python flow).
* `zd-helper` does the root parts (USB switching, the disk image, copying).
  It runs through `sudo` with the password you typed when the app opened.
* **Start:** the folder is copied into a fresh disk image
  (`/var/lib/zero-drive/drive.img`, sparse). The image is shared through the
  Linux USB gadget API (configfs, `mass_storage`). If ADB is on, it is paused
  and turned back on afterwards. The port is switched to device mode live,
  right before sharing.
* **Keep-newer:** a file the Cardputer changed after sharing started is never
  overwritten by an older copy from the computer, or deleted because the
  computer deleted it. Copying back uses `rsync --update`, and a deletion is
  skipped if the local file is newer than the moment sharing started.
* **Stop:** the gadget is removed, the port goes back to host mode, and the image is checked with fsck and
  mounted read-only. New and changed files are copied back, and files the
  computer deleted are deleted from the folder. Only files that were on the drive
  can be deleted, so symlinks and files too big for FAT32 are left alone.
  Files written back (owned by root) are then given to your user with `chown`.
  Hidden files are never touched. The image is removed.
* If the app is closed while sharing, the drive keeps working. Reopen Zero-Drive
  and it goes straight back to the "sharing" screen.
* If the drive can't be read back (for example, the cable was pulled during a
  write), nothing in your folder is changed. The image stays at
  `/var/lib/zero-drive/drive.img` so you can recover it. The log is in
  `/run/zero-drive/log`.

## Building the .deb

```sh
./build-deb.sh      # uses CardputerZero AppBuilder's pack-deb.sh -> dist/
```

### Publishing to the CardputerZero AppStore (optional)

Check `author.display_name` in `app-builder.json`, then:

```sh
.appbuilder/czdev login
.appbuilder/czdev publish --deb dist/zero-drive_1.3.0-1_arm64.deb
```

## Files

```
zerodrive.py        UI
zd-helper           root helper (status/start/stop/reset/livemode/reboot)
packaging/          meta.env, build.sh, stage.sh, postinst, prerm, icons
app-builder.json    AppStore listing for czdev publish
screenshots/        320x170 store screenshots (python3 zerodrive.py --screens DIR)
```

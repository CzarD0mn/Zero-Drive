#!/usr/bin/env python3
"""Zero-Drive — use the Cardputer Zero as a USB flash drive.

Draws straight to the framebuffer (320x170, Pillow + DejaVu Sans) and reads the
built-in keyboard through evdev, following the CardputerZero AppBuilder Python
framebuffer example. Everything that needs root is done by `zd-helper`, run
through `sudo` with a password typed on the device.

Root: asked for once when the app opens, kept in memory until it closes.
USB:  the port is switched to device mode only while sharing (live, no reboot).

Keys:  arrows (or Fn + ; . , /)  move   Enter  select   Esc  back / quit
Run `zerodrive.py --screens DIR` on any PC to render every screen to PNG.
"""

import fcntl
import json
import mmap
import os
import pwd
import select
import signal
import struct
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:  # pragma: no cover
    InputDevice = ecodes = list_devices = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(APP_DIR, "zd-helper")
W, H = 320, 170

NAME = "zero-drive"
GADGET_DIR = "/sys/kernel/config/usb_gadget/" + NAME.replace("-", "")
LUN_FILE = GADGET_DIR + "/functions/mass_storage.usb0/lun.0/file"
CFG_PATH = os.path.expanduser("~/.config/%s/config.json" % NAME)
HOME = os.path.expanduser("~")


def folder_allowed(path):
    """Mirror of zd-helper's folder rules, for a friendly message up front:
    sub-folders of a home folder, or anything under /media or /mnt."""
    p = os.path.realpath(path)
    system = ("/", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run",
              "/sbin", "/sys", "/usr", "/var", "/home", "/root", "/opt", "/srv")
    if p in system or any(p.startswith(d + "/") for d in system
                          if d not in ("/", "/home", "/root", "/opt", "/srv")):
        return False
    parts = p.split("/")
    return (p.startswith("/home/") and len(parts) >= 4) or p.startswith(("/media/", "/mnt/"))
SIZES_GB = [1, 2, 4, 8, 16, 32]

# ---------------------------------------------------------------- look ------
BG = (18, 20, 26)
PANEL = (34, 38, 48)
FG = (236, 236, 236)
DIM = (140, 146, 158)
ACCENT = (255, 168, 0)
OK = (90, 200, 120)
ERR = (235, 85, 85)
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
             os.path.join(APP_DIR, "fonts")]


def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        try:
            return ImageFont.truetype(os.path.join(d, name), size)
        except OSError:
            continue
    return ImageFont.load_default()


F_TITLE = font(14, True)
F_BIG = font(17, True)
F_TXT = font(13)
F_BOLD = font(13, True)
F_SMALL = font(11)


def text_w(f, s):
    return f.getlength(s)


def fit(f, s, width):
    """Shorten text with a leading ellipsis (paths) to fit width."""
    if text_w(f, s) <= width:
        return s
    while s and text_w(f, "…" + s) > width:
        s = s[1:]
    return "…" + s


def wrap(f, s, width):
    lines, cur = [], ""
    for word in s.split():
        t = (cur + " " + word).strip()
        if text_w(f, t) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def human(mb):
    return "%.1f GB" % (mb / 1024) if mb >= 1024 else "%d MB" % mb


# ------------------------------------------------------------- display ------
class FrameBuffer:
    FBIOGET_VSCREENINFO = 0x4600
    FBIOGET_FSCREENINFO = 0x4602

    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDWR)
        vbuf = bytearray(160)
        fcntl.ioctl(self.fd, self.FBIOGET_VSCREENINFO, vbuf)
        self.xres, self.yres, _, _, _, _, self.bpp, _ = struct.unpack_from("8I", vbuf, 0)
        fmt = "16sL" + "I" * 6 + "HHHI"
        fbuf = bytearray(struct.calcsize(fmt))
        fcntl.ioctl(self.fd, self.FBIOGET_FSCREENINFO, fbuf)
        fields = struct.unpack(fmt, fbuf)
        self.size, self.line = fields[2], fields[8]
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

    def show(self, img):
        if img.size != (self.xres, self.yres):
            canvas = Image.new("RGB", (self.xres, self.yres), BG)
            canvas.paste(img, ((self.xres - img.width) // 2, (self.yres - img.height) // 2))
            img = canvas
        if self.bpp == 16:
            if np is not None:
                a = np.asarray(img, dtype=np.uint16)
                v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
                data = v.astype("<u2").tobytes()
            else:  # slow pure-Python fallback
                out = bytearray()
                for r, g, b_ in img.getdata():
                    v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b_ >> 3)
                    out += bytes((v & 0xFF, v >> 8))
                data = bytes(out)
            row = self.xres * 2
        else:
            data = img.convert("RGBA").tobytes("raw", "BGRA")
            row = self.xres * 4
        if row == self.line:
            self.mm.seek(0)
            self.mm.write(data)
        else:
            for y in range(self.yres):
                self.mm.seek(y * self.line)
                self.mm.write(data[y * row:(y + 1) * row])

    def close(self):
        self.mm.close()
        os.close(self.fd)


# ------------------------------------------------------------ keyboard ------
class Keyboard:
    """Turns raw evdev events into ('up'|'down'|'left'|'right'|'enter'|'back'|
    'esc'|'char', ch) tuples. Handles Shift / Caps and the Fn arrow layer."""

    def __init__(self):
        self.devs, self.shift, self.caps, self.fn = [], False, False, False
        if InputDevice is None:
            return
        e = ecodes
        self.FN = getattr(e, "KEY_FN", 0x1d0)
        self.chars = {}
        for c in "abcdefghijklmnopqrstuvwxyz":
            self.chars[getattr(e, "KEY_" + c.upper())] = (c, c.upper())
        for c, s in zip("1234567890", "!@#$%^&*()"):
            self.chars[getattr(e, "KEY_" + c)] = (c, s)
        for name, pair in {"MINUS": "-_", "EQUAL": "=+", "LEFTBRACE": "[{",
                           "RIGHTBRACE": "]}", "SEMICOLON": ";:", "APOSTROPHE": "'\"",
                           "GRAVE": "`~", "BACKSLASH": "\\|", "COMMA": ",<",
                           "DOT": ".>", "SLASH": "/?", "SPACE": "  "}.items():
            self.chars[getattr(e, "KEY_" + name)] = (pair[0], pair[1])
        self.arrows = {e.KEY_UP: "up", e.KEY_DOWN: "down", e.KEY_LEFT: "left",
                       e.KEY_RIGHT: "right"}
        # Cardputer layout: ; . , / carry the arrow symbols
        self.fn_arrows = {e.KEY_SEMICOLON: "up", e.KEY_DOT: "down",
                          e.KEY_COMMA: "left", e.KEY_SLASH: "right"}
        paths = []
        pref = os.environ.get("APPLAUNCH_LINUX_KEYBOARD_DEVICE",
                              "/dev/input/by-path/platform-3f804000.i2c-event")
        if os.path.exists(pref):
            paths.append(pref)
        paths += [p for p in list_devices() if p not in paths]
        for p in paths:
            try:
                d = InputDevice(p)
                if e.KEY_ENTER in d.capabilities().get(e.EV_KEY, []):
                    self.devs.append(d)
                else:
                    d.close()
            except OSError:
                pass

    def poll(self, timeout):
        if not self.devs:
            time.sleep(timeout)
            return []
        r, _, _ = select.select([d.fd for d in self.devs], [], [], timeout)
        out = []
        for d in self.devs:
            if d.fd not in r:
                continue
            try:
                for ev in d.read():
                    if ev.type == ecodes.EV_KEY:
                        k = self._key(ev.code, ev.value)
                        if k:
                            out.append(k)
            except (BlockingIOError, OSError):
                pass
        return out

    def _key(self, code, value):
        e = ecodes
        if code in (e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT):
            self.shift = value != 0
            return None
        if code == self.FN:
            self.fn = value != 0
            return None
        if value == 0:
            return None
        if code == e.KEY_CAPSLOCK and value == 1:
            self.caps = not self.caps
            return None
        if code in self.arrows:
            return (self.arrows[code], None)
        if self.fn and code in self.fn_arrows:
            return (self.fn_arrows[code], None)
        if code in (e.KEY_ENTER, e.KEY_KPENTER):
            return ("enter", None) if value == 1 else None
        if code == e.KEY_ESC:
            return ("esc", None) if value == 1 else None
        if code in (e.KEY_BACKSPACE, e.KEY_DELETE):
            return ("back", None)
        if code in self.chars:
            lo, hi = self.chars[code]
            upper = self.shift != (self.caps and lo.isalpha())
            return ("char", hi if upper else lo)
        return None


# ---------------------------------------------------------------- app -------
class App:
    def __init__(self, demo=False):
        self.demo = demo
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.cfg = {"folder": os.path.expanduser("~/ZeroDrive"), "extra_gb": 2, "fs": "exfat"}
        self.load_cfg()
        if not demo and not folder_allowed(self.cfg["folder"]):
            self.cfg["folder"] = os.path.expanduser("~/ZeroDrive")
        self.home_top = 0
        self.password = None
        self.running = True
        self.state = "home"
        self.sel = 0
        self.msg = None            # (title, text, color, next_state)
        self.confirm = None        # (title, text, yes_fn)
        self.busy = None           # dict: title, stage, proc, buf, kv, err, warn, done_fn
        self.text = ""             # text entry buffer
        self.after_password = None
        self.startup_prompt = False
        self.pick_dir, self.pick_items, self.pick_sel, self.pick_top = "/", [], 0, 0
        self.conn = {"seen": False, "state": "", "since": time.time()}
        self.anim = 0
        self.disk_info = ""
        if not demo:
            try:
                os.makedirs(self.cfg["folder"], exist_ok=True)
            except OSError:
                pass
            self.refresh_info()
            # Ask for root right away and keep it (in memory) for the whole
            # session, so sharing can start/stop without further prompts.
            self.startup_prompt = True
            self.need_root(self.after_startup_root)

    def after_startup_root(self):
        self.startup_prompt = False
        if os.path.isdir(GADGET_DIR):          # app was closed while sharing
            self.enter_connected(resumed=True)

    # ----- config
    def load_cfg(self):
        try:
            with open(CFG_PATH) as fh:
                self.cfg.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def save_cfg(self):
        if self.demo:
            return
        os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
        with open(CFG_PATH, "w") as fh:
            json.dump(self.cfg, fh, indent=1)

    def refresh_info(self):
        f = self.cfg["folder"]
        try:
            used = 0
            for root, dirs, files in os.walk(f):      # hidden files are never shared
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                files = [x for x in files if not x.startswith(".")]
                for n in files:
                    try:
                        used += os.lstat(os.path.join(root, n)).st_size
                    except OSError:
                        pass
            st = os.statvfs(f)
            free = st.f_bavail * st.f_frsize
            self.disk_info = "%s used · %s free on card" % (human(used // 2**20), human(free // 2**20))
        except OSError:
            self.disk_info = "Folder not found"

    # ----- sudo
    def need_root(self, then):
        """Run `then` once we know sudo works, asking for the password if needed."""
        if self.password is not None:
            return then()
        try:
            if subprocess.run(["sudo", "-n", "-k", "true"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
                self.password = ""
                return then()
        except (OSError, subprocess.SubprocessError):
            pass
        self.after_password, self.text, self.state = then, "", "password"
        self.pw_error = ""

    def check_password(self):
        self.pw_error = "Checking…"
        self.render()
        try:
            r = subprocess.run(["sudo", "-S", "-k", "-p", "", "true"], input=self.text + "\n",
                               text=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
            good = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            good = False
        if good:
            self.password, then, self.text = self.text, self.after_password, ""
            self.state = "home"
            then()
        else:
            self.text, self.pw_error = "", "Wrong password (or user can't sudo)"

    def run_helper(self, args, title, done, detach=False):
        cmd = ["sudo", "-S", "-k", "-p", "", "--", HELPER] + args
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)   # survives the launcher's ESC-kill
        try:
            proc.stdin.write(((self.password or "") + "\n").encode())
            proc.stdin.close()
        except OSError:
            pass
        os.set_blocking(proc.stdout.fileno(), False)
        self.busy = {"title": title, "stage": "Starting…", "proc": proc, "buf": "",
                     "kv": {}, "err": None, "warn": [], "done": done}
        self.state = "busy"
        if detach:
            return proc

    def poll_busy(self):
        b = self.busy
        try:
            chunk = os.read(b["proc"].stdout.fileno(), 4096).decode(errors="replace")
        except BlockingIOError:
            chunk = None
        except OSError:
            chunk = ""
        if chunk:
            b["buf"] += chunk
            *lines, b["buf"] = b["buf"].split("\n")
            for ln in lines:
                if ln.startswith("STAGE "):
                    b["stage"] = ln[6:]
                elif ln.startswith("ERROR "):
                    b["err"] = ln[6:]
                elif ln.startswith("WARN "):
                    b["warn"].append(ln[5:])
                elif ln.startswith("KV ") and "=" in ln:
                    k, v = ln[3:].split("=", 1)
                    b["kv"][k] = v
                elif ln.strip() and not ln.startswith("DONE"):
                    b["last"] = ln.strip()
        rc = b["proc"].poll()
        if rc is not None and chunk is not None and not chunk:   # exited + EOF
            self.busy = None
            if rc != 0 and not b["err"]:
                b["err"] = b.get("last") or "Command failed (%d)" % rc
                if "password" in b["err"].lower() or "sudo" in b["err"].lower():
                    self.password = None
            b["done"](rc, b)

    # ----- actions
    def start_drive(self):
        f = self.cfg["folder"]
        if os.path.isdir(GADGET_DIR):      # already sharing — never rebuild over it
            return self.enter_connected(resumed=True)
        try:
            os.makedirs(f, exist_ok=True)
        except OSError as ex:
            return self.show_msg("Folder problem", str(ex), ERR)

        def done(rc, b):
            if rc == 0:
                self.enter_connected(warn=b["warn"])
            elif rc == 12:
                self.confirm = ("One-time USB setup",
                                "This system uses the old dwc_otg USB driver, which can't "
                                "switch live. Move it to dwc2 now? Needs one reboot; after "
                                "that USB switches instantly.",
                                self.enable_livemode)
                self.state = "confirm"
            else:
                self.show_msg("Could not start", b["err"], ERR)

        self.need_root(lambda: self.run_helper(
            ["start", f, str(int(self.cfg["extra_gb"]) * 1024), self.cfg["fs"]],
            "Preparing drive", done))

    def enter_connected(self, resumed=False, warn=None):
        self.state = "connected"
        self.conn = {"seen": resumed, "state": "", "since": time.time(),
                     "warn": "; ".join(warn or [])}

    def poll_connected(self):
        try:
            udcs = os.listdir("/sys/class/udc")
            with open("/sys/class/udc/%s/state" % udcs[0]) as fh:
                st = fh.read().strip()
        except (OSError, IndexError):
            st = "unknown"
        self.conn["state"] = st
        if st in ("configured", "suspended"):
            self.conn["seen"] = True
        ejected = False
        try:
            with open(LUN_FILE) as fh:
                ejected = fh.read().strip() == ""
        except OSError:
            pass
        if self.conn["seen"] and ejected:
            self.stop_drive("Ejected by computer")
        elif self.conn["seen"] and st == "not attached":
            self.stop_drive("Cable unplugged")

    def stop_drive(self, why="Stopped"):
        f = self.cfg["folder"]

        def done(rc, b):
            self.refresh_info()
            if rc == 0:
                changed = b["kv"].get("changed") == "1"
                text = ("Changes saved to " if changed else "No changes. Files are in ") + f
                if b["warn"]:
                    text += ". " + "; ".join(b["warn"])
                self.show_msg(why + " — done", text, OK)
            else:
                self.show_msg("Save problem", b["err"], ERR)

        self.need_root(lambda: self.run_helper(["stop", f, self.user], "Saving files", done))

    def enable_livemode(self):
        def done(rc, b):
            if rc != 0:
                return self.show_msg("USB setup", b["err"], ERR)
            if b["kv"].get("reboot") == "1":
                self.confirm = ("Reboot needed", "Reboot now to finish the one-time setup?",
                                lambda: self.run_helper(["reboot"], "Rebooting",
                                                        lambda r, b: None))
                self.state = "confirm"
            else:
                self.show_msg("USB setup", "Already done.", OK)
        self.need_root(lambda: self.run_helper(["livemode"], "USB setup", done))

    def show_msg(self, title, text, color=FG, nxt="home"):
        self.msg = (title, text, color, nxt)
        self.state = "msg"

    # ----- folder picker
    def open_picker(self, path=None):
        path = path or (self.cfg["folder"] if os.path.isdir(self.cfg["folder"])
                        else os.path.expanduser("~"))
        self.pick_dir = os.path.abspath(path)
        try:
            subs = sorted((n for n in os.listdir(self.pick_dir)
                           if not n.startswith(".") and
                           os.path.isdir(os.path.join(self.pick_dir, n))), key=str.lower)
        except OSError:
            subs = []
        items = [("use", "✔ Use this folder"), ("new", "+ New folder")]
        if self.pick_dir != "/":
            items.append(("up", ".. (up)"))
        items += [("dir", n) for n in subs]
        self.pick_items, self.pick_sel, self.pick_top = items, 0, 0
        self.state = "picker"

    # ----- input
    def on_key(self, k):
        name, ch = k
        getattr(self, "key_" + self.state, lambda *_: None)(name, ch)

    def home_items(self):
        f = self.cfg["folder"].rstrip("/")
        return [
            ("start", "Start USB drive"),
            ("folder", "Folder: " + (os.path.basename(f) or "/")),
            ("size", "Extra space: %d GB" % self.cfg["extra_gb"]),
            ("fs", "Format: " + ("exFAT" if self.cfg["fs"] == "exfat" else "FAT32")),
        ]

    def key_home(self, name, ch):
        items = self.home_items()
        if name == "esc":
            self.running = False
        elif name == "up":
            self.sel = (self.sel - 1) % len(items)
        elif name == "down":
            self.sel = (self.sel + 1) % len(items)
        elif name in ("enter", "left", "right"):
            key = items[self.sel][0]
            step = -1 if name == "left" else 1
            if key == "size":
                i = SIZES_GB.index(self.cfg["extra_gb"]) if self.cfg["extra_gb"] in SIZES_GB else 1
                self.cfg["extra_gb"] = SIZES_GB[(i + step) % len(SIZES_GB)]
                self.save_cfg()
            elif key == "fs":
                self.cfg["fs"] = "fat32" if self.cfg["fs"] == "exfat" else "exfat"
                self.save_cfg()
            elif name == "enter":
                if key == "start":
                    self.start_drive()
                elif key == "folder":
                    self.open_picker()

    def key_picker(self, name, ch):
        if name == "esc":
            self.state = "home"
        elif name == "up":
            self.pick_sel = max(0, self.pick_sel - 1)
        elif name == "down":
            self.pick_sel = min(len(self.pick_items) - 1, self.pick_sel + 1)
        elif name in ("back", "left") and self.pick_dir != "/":
            self.open_picker(os.path.dirname(self.pick_dir))
        elif name in ("enter", "right"):
            kind, label = self.pick_items[self.pick_sel]
            if kind == "dir":
                self.open_picker(os.path.join(self.pick_dir, label))
            elif kind == "up":
                self.open_picker(os.path.dirname(self.pick_dir))
            elif kind == "new" and name == "enter":
                self.text, self.state = "", "newfolder"
            elif kind == "use" and name == "enter":
                self.choose_folder(self.pick_dir)

    def choose_folder(self, path):
        if not folder_allowed(path):
            return self.show_msg("Folder not allowed",
                                 "Pick a folder inside your home folder, or under /media or "
                                 "/mnt. The whole home folder can't be shared.", ERR, "picker")
        self.cfg["folder"] = path
        self.save_cfg()
        self.refresh_info()
        self.state = "home"

    def key_newfolder(self, name, ch):
        if name == "esc":
            self.state = "picker"
        elif name == "back":
            self.text = self.text[:-1]
        elif name == "char" and ch not in "/\\" and len(self.text) < 40:
            self.text += ch
        elif name == "enter" and self.text.strip():
            p = os.path.join(self.pick_dir, self.text.strip())
            try:
                os.makedirs(p, exist_ok=True)
                self.open_picker(p)
            except OSError as ex:
                self.show_msg("Could not create folder", str(ex), ERR, "picker")

    def key_password(self, name, ch):
        if name == "esc":
            if self.startup_prompt:            # can't work without root
                self.running = False
            else:
                self.state = "connected" if os.path.isdir(GADGET_DIR) else "home"
        elif name == "back":
            self.text = self.text[:-1]
        elif name == "char":
            self.text += ch
        elif name == "enter":
            self.check_password()

    def key_connected(self, name, ch):
        if name in ("enter", "esc", "back"):
            self.confirm = ("Stop sharing?",
                            "Eject the drive on your computer first, then confirm.",
                            lambda: self.stop_drive())
            self.state = "confirm"

    def key_confirm(self, name, ch):
        if name == "enter":
            self.state = "home"
            self.confirm[2]()
        elif name in ("esc", "back"):
            self.state = "connected" if os.path.isdir(GADGET_DIR) else "home"

    def key_msg(self, name, ch):
        if name in ("enter", "esc"):
            self.state = self.msg[3]

    # ----- drawing
    def header(self, d, title, right=""):
        d.rectangle([0, 0, W, 21], fill=PANEL)
        d.rectangle([0, 21, W, 22], fill=ACCENT)
        d.text((8, 3), title, font=F_TITLE, fill=FG)
        if right:
            d.text((W - 8 - text_w(F_SMALL, right), 5), right, font=F_SMALL, fill=DIM)

    def footer(self, d, hint):
        d.text((8, H - 15), hint, font=F_SMALL, fill=DIM)

    def menu(self, d, items, sel, y0, row=19, top=0, rows=5):
        for i, label in enumerate(items[top:top + rows]):
            idx = top + i
            y = y0 + i * row
            if idx == sel:
                d.rounded_rectangle([4, y, W - 5, y + row - 2], radius=4, fill=PANEL,
                                    outline=ACCENT)
            d.text((12, y + 1), fit(F_TXT, label, W - 24), font=F_TXT,
                   fill=FG if idx == sel else (205, 205, 210))

    def draw_drive_icon(self, d, cx, cy, color):
        d.rounded_rectangle([cx - 14, cy - 18, cx + 14, cy + 22], radius=5, fill=color)
        d.rectangle([cx - 8, cy - 30, cx + 8, cy - 18], outline=color, width=3)
        d.rectangle([cx - 4, cy - 26, cx - 1, cy - 22], fill=color)
        d.rectangle([cx + 2, cy - 26, cx + 5, cy - 22], fill=color)

    def render_img(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        getattr(self, "draw_" + self.state)(d)
        return img

    def draw_home(self, d):
        self.header(d, "ZERO-DRIVE", "USB flash drive")
        d.text((8, 27), fit(F_SMALL, self.cfg["folder"], W - 16), font=F_SMALL, fill=ACCENT)
        d.text((8, 41), self.disk_info, font=F_SMALL, fill=DIM)
        items = [l for _, l in self.home_items()]
        rows = 5
        if self.sel < self.home_top:
            self.home_top = self.sel
        if self.sel >= self.home_top + rows:
            self.home_top = self.sel - rows + 1
        self.menu(d, items, self.sel, 57, top=self.home_top, rows=rows)
        if len(items) > rows:                 # tiny scroll hint
            if self.home_top > 0:
                d.text((W - 14, 55), "▴", font=F_SMALL, fill=DIM)
            if self.home_top + rows < len(items):
                d.text((W - 14, 140), "▾", font=F_SMALL, fill=DIM)
        self.footer(d, "↑↓ move  ←→ change  Enter select  Esc quit")

    def draw_picker(self, d):
        self.header(d, "CHOOSE FOLDER")
        d.text((8, 27), fit(F_SMALL, self.pick_dir, W - 16), font=F_SMALL, fill=ACCENT)
        rows = 5
        if self.pick_sel < self.pick_top:
            self.pick_top = self.pick_sel
        if self.pick_sel >= self.pick_top + rows:
            self.pick_top = self.pick_sel - rows + 1
        labels = [("▸ " + l) if k == "dir" else l for k, l in self.pick_items]
        self.menu(d, labels, self.pick_sel, 43, top=self.pick_top, rows=rows)
        self.footer(d, "Enter open/select  ⌫ up  Esc cancel")

    def draw_textbox(self, d, title, prompt, value, note="", note_color=DIM):
        self.header(d, title)
        y = 32
        for ln in wrap(F_TXT, prompt, W - 16):
            d.text((8, y), ln, font=F_TXT, fill=FG)
            y += 17
        d.rounded_rectangle([8, 82, W - 9, 108], radius=4, fill=PANEL, outline=ACCENT)
        cursor = "_" if int(time.time() * 2) % 2 == 0 else " "
        d.text((14, 87), fit(F_BIG, value, W - 40) + cursor, font=F_BIG, fill=FG)
        if note:
            d.text((8, 116), note, font=F_SMALL, fill=note_color)

    def draw_newfolder(self, d):
        self.draw_textbox(d, "NEW FOLDER", "Name for the new folder:", self.text)
        self.footer(d, "Enter create  Esc cancel")

    def draw_password(self, d):
        err = getattr(self, "pw_error", "")
        self.draw_textbox(d, "ROOT ACCESS",
                          "Zero-Drive needs root to switch USB and share the drive. "
                          "Password for %s:" % self.user,
                          "•" * len(self.text), err, ERR if "Wrong" in err else DIM)
        self.footer(d, "Enter OK  Esc %s  (kept until app closes)"
                    % ("quit" if self.startup_prompt else "cancel"))

    def draw_busy(self, d):
        b = self.busy
        self.header(d, b["title"].upper())
        cx, cy, r = W // 2, 72, 22
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=PANEL, width=5)
        a = (self.anim * 30) % 360
        d.arc([cx - r, cy - r, cx + r, cy + r], a, a + 90, fill=ACCENT, width=5)
        s = fit(F_TXT, b["stage"], W - 16)
        d.text(((W - text_w(F_TXT, s)) / 2, 108), s, font=F_TXT, fill=FG)
        self.footer(d, "Please wait…")

    def draw_connected(self, d):
        st = self.conn.get("state", "")
        if st in ("configured",):
            title, col, line = "CONNECTED", OK, "In use by your computer"
        elif st == "suspended":
            title, col, line = "CONNECTED", OK, "Computer is asleep"
        elif st in ("not attached", "unknown", ""):
            title, col, line = "READY", ACCENT, ("USB switch to Slave (left), then plug "
                                                 "the right USB-C port into a computer")
        else:
            title, col, line = "CONNECTING", ACCENT, "Waiting for computer…"
        self.header(d, "ZERO-DRIVE", "sharing")
        self.draw_drive_icon(d, 44, 82, col)
        d.text((84, 40), title, font=F_BIG, fill=col)
        y = 64
        for ln in wrap(F_TXT, line, W - 92)[:3]:
            d.text((84, y), ln, font=F_TXT, fill=FG)
            y += 17
        tip = self.conn.get("warn") or "Eject on the computer when done; files save automatically."
        y = max(y + 4, 102)
        for ln in wrap(F_SMALL, tip, W - 92)[:3]:
            d.text((84, y), ln, font=F_SMALL, fill=DIM)
            y += 14
        self.footer(d, "Enter stop sharing")

    def draw_confirm(self, d):
        title, text, _ = self.confirm
        self.header(d, title.upper())
        y = 36
        for ln in wrap(F_TXT, text, W - 16):
            d.text((8, y), ln, font=F_TXT, fill=FG)
            y += 18
        self.footer(d, "Enter yes  Esc no")

    def draw_msg(self, d):
        title, text, color, _ = self.msg
        self.header(d, "ZERO-DRIVE")
        d.text((8, 30), fit(F_BIG, title, W - 16), font=F_BIG, fill=color)
        y = 56
        for ln in wrap(F_TXT, text or "", W - 16)[:5]:
            d.text((8, y), ln, font=F_TXT, fill=FG)
            y += 17
        self.footer(d, "Enter OK")

    # ----- main loop
    def render(self):
        self.screen.show(self.render_img())

    def run(self, screen, keys):
        self.screen = screen
        last_poll = 0
        while self.running:
            for k in keys.poll(0.1):
                self.on_key(k)
            self.anim += 1
            if self.state == "busy" and self.busy:
                self.poll_busy()
            if self.state == "connected" and time.time() - last_poll > 0.5:
                last_poll = time.time()
                self.poll_connected()
            self.render()


def main():
    if "--screens" in sys.argv:
        return render_screens(sys.argv[sys.argv.index("--screens") + 1])
    app = App()
    signal.signal(signal.SIGINT, lambda *_: setattr(app, "running", False))

    def on_term(*_):
        # Launcher ESC-hold sends SIGTERM. If sharing, save in the background
        # (helper runs in its own session, so it outlives us).
        if os.path.isdir(GADGET_DIR) and app.password is not None:
            app.run_helper(["stop", app.cfg["folder"], app.user], "Saving files",
                           lambda *a: None, detach=True)
        app.running = False
    signal.signal(signal.SIGTERM, on_term)

    fb = FrameBuffer(os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0"))
    keys = Keyboard()
    try:
        app.run(fb, keys)
        img = Image.new("RGB", (W, H), (0, 0, 0))
        fb.show(img)
    finally:
        for d in keys.devs:
            try:
                d.close()
            except OSError:
                pass
        fb.close()
    return 0


def render_screens(out):
    """Render every screen to PNG (for the store listing / previewing on a PC)."""
    os.makedirs(out, exist_ok=True)
    a = App(demo=True)
    a.cfg["folder"] = "/home/pi/ZeroDrive"
    a.disk_info = "1.2 GB used · 21.4 GB free on card"
    a.anim = 4

    def save(name):
        a.render_img().save(os.path.join(out, name + ".png"))

    save("home")
    a.pick_dir = "/home/pi"
    a.pick_items = [("use", "✔ Use this folder"), ("new", "+ New folder"), ("up", ".. (up)"),
                    ("dir", "Documents"), ("dir", "Music"), ("dir", "ZeroDrive")]
    a.pick_sel = 3
    a.state = "picker"; save("picker")
    a.state, a.text, a.pw_error, a.startup_prompt = "password", "secret", "", True
    save("password")
    a.busy = {"title": "Preparing drive", "stage": "Copying files to drive"}
    a.state = "busy"; save("busy")
    a.state = "connected"; a.conn = {"state": "configured", "seen": True}; save("connected")
    a.conn = {"state": "not attached", "seen": False}; save("ready")
    a.show_msg("Ejected by computer — done", "Changes saved to /home/pi/ZeroDrive", OK); save("done")
    print("screens written to", out)


if __name__ == "__main__":
    sys.exit(main())

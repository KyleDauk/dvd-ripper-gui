#!/usr/bin/env python3
"""
dvdrip.py - Terminal DVD ripper for Plex libraries.

Orchestrates lsdvd -> dvdbackup -> ffmpeg. Does no decryption itself;
libdvdcss (already installed via Homebrew) handles CSS.

Requires: lsdvd, dvdbackup, ffmpeg, ffprobe  (all via Homebrew)
"""

import curses
import datetime
import glob
import json
import os
import re
import shutil
import difflib
import hashlib
import json
import select
import signal
import termios
import tty
import plistlib
import sqlite3
import subprocess
import threading
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------- config

APP_DIR = Path.home() / ".dvdrip"
DB_PATH = APP_DIR / "state.db"
WORK_DIR = Path.home() / "rips"

ENCODE_ARGS = [
    "-vf", "bwdif=mode=send_frame",
    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
    "-c:a", "aac", "-b:a", "192k", "-ac", "2",
    "-movflags", "+faststart",
]

# Anything shorter than this is a logo, trailer or menu loop, never content.
FLOOR_SEC = 4 * 60
# Anything longer than this on a TV disc is a bonus documentary or a play-all.
CEILING_SEC = 90 * 60
# Two runtimes within this fraction of each other count as the same "kind"
# of thing. Episodes on one disc vary by a minute or two, so this needs slack.
CLUSTER_TOLERANCE = 0.10

# Copy watchdog: if the output folder stops growing for this long, decide
# whether we already have what we need instead of waiting forever.
STALL_SECONDS = 150
# dvdbackup silently truncates its -n argument at 32 characters.
MAX_BACKUP_NAME = 32
POLL_SECONDS = 2


def load_settings(store):
    """Pull tunables out of the database so they can be changed from the menu."""
    global FLOOR_SEC, CEILING_SEC, CLUSTER_TOLERANCE, STALL_SECONDS
    FLOOR_SEC = int(store.get("floor_sec", FLOOR_SEC))
    CEILING_SEC = int(store.get("ceiling_sec", CEILING_SEC))
    CLUSTER_TOLERANCE = float(store.get("cluster_tolerance", CLUSTER_TOLERANCE))
    STALL_SECONDS = int(store.get("stall_seconds", STALL_SECONDS))

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[38;5;160m"
GREEN = "\033[38;5;29m"
YELLOW = "\033[38;5;130m"
CYAN = "\033[38;5;31m"


# 256-colour codes chosen to stay legible on both light and dark terminals.
BGREEN = "\033[38;5;35m"
BCYAN = "\033[38;5;31m"
HIDE = "\033[?25l"
SHOW = "\033[?25h"

# Frames that read as a disc turning on the spindle.
DISC_FRAMES = ["◐", "◓", "◑", "◒"]
ARC_FRAMES = ["◜", "◝", "◞", "◟"]


def c(text, color):
    return f"{color}{text}{RESET}"


def hdr(text):
    print()
    print(c("=" * 68, CYAN))
    print(c(f" {text}", CYAN + BOLD))
    print(c("=" * 68, CYAN))


def mmss(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def bar(pct, width=30, color=BGREEN):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    return (color + "\u2588" * filled + RESET
            + DIM + "\u2591" * (width - filled) + RESET)


class Board:
    """Draws a fixed block of lines and redraws it in place."""

    def __init__(self, height):
        self.height = height
        self.drawn = False
        sys.stdout.write(HIDE)
        sys.stdout.flush()

    def render(self, lines):
        lines = (lines + [""] * self.height)[:self.height]
        buf = []
        if self.drawn:
            buf.append(f"\033[{self.height}A")
        for line in lines:
            buf.append("\033[2K" + line + "\n")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()
        self.drawn = True

    def close(self):
        sys.stdout.write(SHOW)
        sys.stdout.flush()


class Keys:
    """Reads single keypresses without waiting for enter."""

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.old = None

    def __enter__(self):
        if self.fd is not None:
            try:
                self.old = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
            except termios.error:
                self.fd = None
        return self

    def __exit__(self, *exc):
        self.restore()

    def restore(self):
        if self.fd is not None and self.old:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            except termios.error:
                pass
            self.old = None

    def get(self):
        """Return a lowercase key, or None if nothing is waiting."""
        if self.fd is None:
            return None
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if not ready:
                return None
            ch = sys.stdin.read(1)
        except (OSError, ValueError):
            return None
        if ch == "\x1b":  # swallow escape sequences (arrow keys etc)
            try:
                while select.select([sys.stdin], [], [], 0.01)[0]:
                    sys.stdin.read(1)
            except (OSError, ValueError):
                pass
            return None
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch.lower()


def buttons(paused, extra=None):
    """A row of key hints styled to read as buttons."""
    def btn(key, text, color=BCYAN):
        return f"{color}{BOLD}[{key.upper()}]{RESET} {text}"

    parts = []
    if paused:
        parts.append(btn("p", c("resume", YELLOW), YELLOW))
    else:
        parts.append(btn("p", "pause"))
    if extra:
        parts.append(btn(*extra))
    parts.append(btn("q", "stop", RED))
    return "   " + "     ".join(parts)


class Spinner:
    def __init__(self, frames=None, every=0.12):
        self.frames = frames or DISC_FRAMES
        self.every = every
        self.start = time.time()

    def frame(self):
        i = int((time.time() - self.start) / self.every) % len(self.frames)
        return self.frames[i]


def disc_art(spinner):
    """A small spinning disc drawn from arc characters."""
    i = int((time.time() - spinner.start) / 0.15) % 4
    rot = ARC_FRAMES[i:] + ARC_FRAMES[:i]
    return (f"{BCYAN}{rot[0]}{rot[1]}{RESET}",
            f"{BCYAN}{rot[3]}{rot[2]}{RESET}")


def eta_clock(seconds_remaining):
    if seconds_remaining <= 0 or seconds_remaining > 86400:
        return "--:--"
    done_at = time.localtime(time.time() + seconds_remaining)
    return time.strftime("%I:%M %p", done_at).lstrip("0")


def hhmmss(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return answer or (default or "")


def ask_required(prompt, default=None):
    """Like ask(), but will not accept an empty answer unless there is a default."""
    while True:
        answer = ask(prompt, default)
        if answer:
            return answer
        print(c("  This one can't be left blank.", YELLOW))


def ask_int(prompt, default=None, low=None, high=None):
    """Numeric prompt. Re-asks on anything that is not a valid number."""
    while True:
        raw = ask(prompt, default)
        try:
            value = int(str(raw).strip())
        except ValueError:
            print(c(f"  '{raw}' is not a number. Enter digits only.", YELLOW))
            continue
        if low is not None and value < low:
            print(c(f"  Must be {low} or higher.", YELLOW))
            continue
        if high is not None and value > high:
            print(c(f"  Must be {high} or lower.", YELLOW))
            continue
        return value


def pick_or_type(prompt, existing, default=None):
    """Show existing folder names as numbered options; allow typing a new one."""
    if existing:
        print()
        for i, name in enumerate(existing, 1):
            print(f"  {i}. {name}")
        print(f"  {DIM}or type a new name{RESET}")
    answer = ask_required(prompt, default)
    if answer.isdigit() and existing and 1 <= int(answer) <= len(existing):
        return existing[int(answer) - 1]
    return answer


def subfolders(path):
    try:
        return sorted([p.name for p in Path(path).iterdir()
                       if p.is_dir() and not p.name.startswith(".")])
    except Exception:
        return []


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    if not answer:
        return default
    return answer.startswith("y")


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS discs (
    disc_id    TEXT PRIMARY KEY,
    label      TEXT,
    note       TEXT,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS rips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    disc_id     TEXT,
    title_ix    INTEGER,
    output_path TEXT,
    src_seconds REAL,
    out_seconds REAL,
    status      TEXT,
    created     TEXT
);
CREATE TABLE IF NOT EXISTS series (
    show      TEXT,
    season    INTEGER,
    next_ep   INTEGER,
    PRIMARY KEY (show, season)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(DB_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    def see_disc(self, disc_id, label):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(
            "INSERT INTO discs(disc_id,label,first_seen,last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(disc_id) DO UPDATE SET last_seen=excluded.last_seen",
            (disc_id, label, now, now),
        )
        self.db.commit()

    def disc_note(self, disc_id):
        row = self.db.execute(
            "SELECT note, first_seen FROM discs WHERE disc_id=?", (disc_id,)
        ).fetchone()
        return row

    def set_disc_note(self, disc_id, note):
        self.db.execute("UPDATE discs SET note=? WHERE disc_id=?", (note, disc_id))
        self.db.commit()

    def prior_rips(self, disc_id):
        return self.db.execute(
            "SELECT * FROM rips WHERE disc_id=? ORDER BY title_ix", (disc_id,)
        ).fetchall()

    def log_rip(self, disc_id, title_ix, path, src_sec, out_sec, status):
        self.db.execute(
            "INSERT INTO rips(disc_id,title_ix,output_path,src_seconds,"
            "out_seconds,status,created) VALUES(?,?,?,?,?,?,?)",
            (disc_id, title_ix, str(path), src_sec, out_sec, status,
             time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.db.commit()

    def next_episode(self, show, season):
        row = self.db.execute(
            "SELECT next_ep FROM series WHERE show=? AND season=?", (show, season)
        ).fetchone()
        return row["next_ep"] if row else 1

    def set_next_episode(self, show, season, n):
        self.db.execute(
            "INSERT INTO series(show,season,next_ep) VALUES(?,?,?) "
            "ON CONFLICT(show,season) DO UPDATE SET next_ep=excluded.next_ep",
            (show, season, n),
        )
        self.db.commit()

    def known_shows(self):
        return [r["show"] for r in self.db.execute(
            "SELECT DISTINCT show FROM series ORDER BY show")]


# ---------------------------------------------------------------- deps

def which(name):
    return shutil.which(name)


def check_deps():
    missing = [t for t in ("lsdvd", "dvdbackup", "ffmpeg", "ffprobe") if not which(t)]
    if missing:
        print(c(f"Missing required tools: {', '.join(missing)}", RED))
        print("Install with:  brew install " + " ".join(missing))
        sys.exit(1)

    css = any(Path(p).exists() for p in (
        "/usr/local/lib/libdvdcss.2.dylib",
        "/opt/homebrew/lib/libdvdcss.2.dylib",
        "/usr/lib/libdvdcss.2.dylib",
    ))
    if not css:
        print(c("Warning: libdvdcss.2.dylib not found in the usual places.", YELLOW))
        print("Encrypted discs will fail. Fix with:")
        print("  brew install libdvdcss")
        print("  sudo ln -s /opt/homebrew/lib/libdvdcss.2.dylib "
              "/usr/local/lib/libdvdcss.2.dylib")
        if not confirm("Continue anyway?", False):
            sys.exit(1)


# ---------------------------------------------------------------- drives

def is_optical(dev):
    """Ask diskutil whether this node is really an optical drive."""
    if not well_formed_device(dev):
        return False
    out = run_cmd(["diskutil", "info", str(dev)], timeout=15)
    if not out or "Could not find" in out:
        return False
    if re.search(r"Optical Drive|CD-ROM|DVD|Blu-?ray|UDF|ISO ?9660",
                 out, re.I):
        return True
    # Optical media is always removable, ejectable and read-only.
    ejectable = bool(re.search(r"Ejectable:\s*Yes", out, re.I))
    readonly = bool(re.search(r"Read-Only Media:\s*Yes", out, re.I))
    return ejectable and readonly


def find_optical_drives():
    """Return [(device, description)] for every optical drive we can spot."""
    found = {}

    # drutil is the most direct source on macOS.
    try:
        out = subprocess.run(["drutil", "status"], capture_output=True,
                             text=True, timeout=15).stdout
        name = None
        m = re.search(r"Name:\s*(.+)", out)
        if m:
            name = m.group(1).strip()
        for m in re.finditer(r"(/dev/disk\d+)", out):
            found[m.group(1)] = name or "optical drive"
    except Exception:
        pass

    # diskutil as a cross-check: optical media shows a CD/DVD partition scheme.
    try:
        out = subprocess.run(["diskutil", "list"], capture_output=True,
                             text=True, timeout=15).stdout
        for block in out.split("/dev/"):
            if not block.strip():
                continue
            dev = "/dev/" + block.split()[0].rstrip(":")
            if re.search(r"CD_partition_scheme|DVD|UDF|ISO9660", block, re.I):
                found.setdefault(dev, "optical media detected")
    except Exception:
        pass

    # Device numbers shift when a drive is replugged, so interrogate every
    # external physical node rather than trusting a remembered number.
    try:
        out = subprocess.run(["diskutil", "list"], capture_output=True,
                             text=True, timeout=15).stdout
        for m in re.finditer(r"(/dev/disk\d+) \(external, physical\)", out):
            dev = m.group(1)
            if dev in found:
                continue
            if is_optical(dev):
                found[dev] = "optical drive (external)"
    except Exception:
        pass

    return sorted(found.items())


def probe_device(dev):
    """Cheap test: can lsdvd see a DVD structure here?"""
    try:
        r = subprocess.run(["lsdvd", dev], capture_output=True, text=True, timeout=30)
        return "Title:" in r.stdout
    except Exception:
        return False


def normalize_device(raw):
    """Accept /dev/disk4, disk4, or 4 - reject anything that is not a device."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        raw = f"/dev/disk{raw}"
    elif re.fullmatch(r"disk\d+", raw):
        raw = f"/dev/{raw}"
    if not re.fullmatch(r"/dev/[a-z]+\d+", raw):
        return None
    return raw


def well_formed_device(dev):
    """Correct shape for a device path. Says nothing about media presence."""
    return bool(dev) and bool(re.fullmatch(r"/dev/[a-z]+\d+", str(dev)))


def media_present(dev):
    """Optical device nodes on macOS appear only while a disc is loaded."""
    return well_formed_device(dev) and Path(str(dev)).exists()


# Kept for readability at call sites that only care about the shape.
valid_device = well_formed_device


def wait_for_media(dev, seconds=45):
    """Poll for the device node after a disc is loaded."""
    if media_present(dev):
        return True
    spin = Spinner()
    start = time.time()
    try:
        while time.time() - start < seconds:
            if media_present(dev):
                sys.stdout.write("\r\033[2K")
                print(c(f"  Disc detected on {dev}.", GREEN))
                time.sleep(2)          # let the drive finish spinning up
                return True
            left = int(seconds - (time.time() - start))
            sys.stdout.write(f"\r\033[2K  {spin.frame()} waiting for a disc "
                             f"in {dev}... {left}s  {DIM}(ctrl-c to give up)"
                             f"{RESET}")
            sys.stdout.flush()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\r\033[2K")
    return media_present(dev)


def run_cmd(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"(failed: {e})"


def drive_diagnostics(store):
    """Work out where in the chain the drive has gone missing."""
    hdr("Drive diagnostics")

    checks = []

    # 1. Does macOS know an optical drive exists at all?
    burn = run_cmd(["system_profiler", "SPDiscBurningDataType"])
    has_drive = bool(re.search(r"(Vendor|Model|Interconnect)", burn))
    checks.append(("Optical drive known to macOS", has_drive))
    model = ""
    m = re.search(r"Model:\s*(.+)", burn)
    if m:
        model = m.group(1).strip()

    # 2. Is it on the USB bus?
    usb = run_cmd(["system_profiler", "SPUSBDataType"])
    on_usb = bool(re.search(r"ASUS|BW-\d|Optical|Blu-?ray|DVD", usb, re.I))
    checks.append(("Present on the USB bus", on_usb))

    # 3. Does drutil see it?
    dr = run_cmd(["drutil", "status"])
    drutil_ok = "No Media" in dr or "/dev/disk" in dr or "Type:" in dr
    checks.append(("drutil can talk to it", drutil_ok))

    # 4. Is media loaded?
    media = "/dev/disk" in dr and "No Media" not in dr
    checks.append(("Disc loaded and readable", media))

    for label, ok in checks:
        mark = c("  yes", GREEN) if ok else c("  NO ", RED)
        print(f"  {mark}   {label}")

    if model:
        print(f"\n  Model: {c(model, BOLD)}")

    node = None
    mm = re.search(r"(/dev/disk\d+)", dr)
    if mm:
        node = mm.group(1)
        print(f"  Device node: {c(node, BOLD)}")

    # Interpret the first failure - that is where the chain breaks.
    print()
    if not on_usb and not has_drive:
        print(c("  macOS cannot see the drive at all.", RED))
        print("  This is a connection or power problem, not a software one:")
        print("    - unplug the USB cable, wait 10 seconds, plug it back in")
        print("    - use a port directly on the Mac, not a hub")
        print("    - if it has a Y-cable, connect BOTH USB plugs")
        print("    - external Blu-ray drives often need their own power brick")
    elif not drutil_ok:
        print(c("  The drive is connected but not responding.", YELLOW))
        print("  Try unplugging and reconnecting it.")
    elif not media:
        print(c("  Drive is fine - no readable disc loaded.", YELLOW))
        print("  Close the tray, or the disc may be dirty or inserted "
              "upside down.")
    else:
        print(c("  Everything looks healthy.", GREEN))
        if node:
            print(f"  Saving {node} as the drive.")
            store.set("drive", node)

    return node


def refresh_drive(store):
    """Re-detect the drive and update the saved path."""
    hdr("Refreshing drive")
    print(c("  Looking for optical drives...", DIM))

    drives = find_optical_drives()
    if drives:
        for i, (dv, desc) in enumerate(drives, 1):
            state = "disc loaded" if media_present(dv) else "no disc"
            print(f"  {i}. {c(dv, BOLD)}  {DIM}{desc} - {state}{RESET}")
        dev = drives[0][0] if len(drives) == 1 else None
        if dev is None:
            pick = ask(f"  Use which (1-{len(drives)})", "1")
            try:
                dev = drives[int(pick) - 1][0]
            except (ValueError, IndexError):
                dev = drives[0][0]
        store.set("drive", dev)
        print(c(f"  Drive set to {dev}", GREEN))
        return dev

    print(c("  Nothing found. Running diagnostics...", YELLOW))
    node = drive_diagnostics(store)
    if node:
        return node

    if confirm("\n  Reconnect the drive now and try once more?", True):
        ask("  Unplug it, wait 10 seconds, plug it back in, then press enter")
        print(c("  Waiting for the system to enumerate it...", DIM))
        for _ in range(15):
            time.sleep(1)
            drives = find_optical_drives()
            if drives:
                dev = drives[0][0]
                store.set("drive", dev)
                print(c(f"  Found it: {dev}", GREEN))
                return dev
        print(c("  Still nothing.", RED))
    return None


def choose_drive(store, force=False):
    saved = store.get("drive")
    if saved and not force:
        if well_formed_device(saved):
            # Device numbers shift between plug-ins, so confirm this node is
            # still the optical drive before trusting it.
            if media_present(saved) and not is_optical(saved):
                print(c(f"{saved} is no longer the optical drive "
                        f"(device numbers shift when it is replugged).",
                        YELLOW))
                found = find_optical_drives()
                if found:
                    saved = found[0][0]
                    store.set("drive", saved)
                    print(c(f"Now using {saved}", GREEN))
                    return saved
                print(c("Rescanning...", DIM))
            else:
                state = ("disc loaded" if media_present(saved)
                         else c("no disc", YELLOW))
                print(f"Using saved drive: {c(saved, BOLD)}  "
                      f"{DIM}({state}){RESET}")
                return saved
        print(c(f"Saved drive '{saved}' is not a device path - rescanning.",
                YELLOW))
        store.set("drive", "")

    hdr("Scanning for optical drives")
    drives = find_optical_drives()

    if not drives:
        print(c("No optical drive auto-detected.", YELLOW))
        print("Is a disc inserted? Some drives only appear once media is loaded.")
        while True:
            manual = ask("Enter device path (e.g. /dev/disk4), "
                         "or blank to abort")
            if not manual:
                return None
            dev = normalize_device(manual)
            if not dev:
                print(c(f"  '{manual}' is not a device path. Try /dev/disk4.",
                        YELLOW))
                continue
            store.set("drive", dev)
            if not media_present(dev):
                print(c(f"  No disc in {dev} yet - that is normal with the "
                        f"tray open.", DIM))
            return dev

    for i, (dev, desc) in enumerate(drives, 1):
        print(f"  {i}. {c(dev, BOLD)}  {DIM}{desc}{RESET}")

    if len(drives) == 1:
        dev = drives[0][0]
        print(f"\nOnly one candidate. Using {c(dev, GREEN)}.")
    else:
        dev = None
        while dev is None:
            pick = ask(f"Choose drive 1-{len(drives)}", "1")
            try:
                idx = int(pick) - 1
                if not 0 <= idx < len(drives):
                    raise IndexError
                dev = drives[idx][0]
            except (ValueError, IndexError):
                print(c(f"  Enter a number between 1 and {len(drives)}.",
                        YELLOW))

    store.set("drive", dev)
    return dev


def unmount(dev):
    """CSS key retrieval requires the disc be unmounted."""
    if not well_formed_device(dev):
        print(c(f"Refusing to unmount '{dev}' - not a device path.", YELLOW))
        return
    if not media_present(dev):
        return          # nothing loaded, nothing to unmount
    r = subprocess.run(["diskutil", "unmount", dev],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(c(f"Unmounted {dev}", DIM))
    else:
        msg = (r.stderr or r.stdout).strip()
        if "not mounted" in msg.lower() or "unmount failed" not in msg.lower():
            print(c(f"{dev} already unmounted", DIM))
        else:
            print(c(f"Unmount reported: {msg}", YELLOW))


# ---------------------------------------------------------------- volumes

def tray(action, dev=None):
    """action is 'eject', 'close' or 'open'. Returns True on success."""
    cmds = []
    if action == "eject":
        cmds = [["drutil", "tray", "eject"], ["drutil", "eject"]]
        if dev:
            cmds.append(["diskutil", "eject", dev])
    elif action == "close":
        cmds = [["drutil", "tray", "close"]]
    elif action == "open":
        cmds = [["drutil", "tray", "open"], ["drutil", "eject"]]

    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def eject_disc(store):
    dev = store.get("drive")
    print(c("  Ejecting...", DIM))
    if tray("eject", dev):
        print(c("  Tray open.", GREEN))
    else:
        print(c("  Could not eject. The disc may be in use - is a rip "
                "still running?", YELLOW))


def close_tray():
    print(c("  Closing tray...", DIM))
    if tray("close"):
        print(c("  Tray closed. Give the drive a few seconds to spin up.",
                GREEN))
        time.sleep(3)
        return True
    print(c("  Could not close the tray.", YELLOW))
    print(c("  Slot-loading and some external drives cannot be closed by "
            "software - push it in by hand.", DIM))
    return False


def swap_disc(store):
    """Eject, wait for the next disc, close, and report readiness."""
    dev = store.get("drive")
    hdr("Swap disc")
    print(c("  Ejecting...", DIM))
    if not tray("eject", dev):
        print(c("  Could not eject - eject it by hand.", YELLOW))
    ask("\n  Put in the next disc, then press enter")
    close_tray()
    if well_formed_device(dev) and not wait_for_media(dev):
        print(c("  No disc detected yet - it may still be spinning up.",
                YELLOW))
    else:
        print(c("  Ready.", GREEN))
    return True


def find_volumes():
    vols = []
    for p in sorted(Path("/Volumes").glob("*")):
        if not p.is_dir():
            continue
        try:
            usage = shutil.disk_usage(p)
            vols.append((p, usage.free))
        except Exception:
            continue
    return vols


def choose_library(store, kind):
    """kind is 'movies' or 'tv'. Remembers the choice."""
    key = f"lib_{kind}"
    saved = store.get(key)
    if saved and Path(saved).is_dir():
        if confirm(f"Use saved {kind} library: {saved}?"):
            return Path(saved)

    hdr(f"Choose {kind} library location")
    vols = find_volumes()
    if not vols:
        print(c("No mounted volumes found under /Volumes.", YELLOW))
    for i, (p, free) in enumerate(vols, 1):
        gb = free / (1024 ** 3)
        print(f"  {i}. {c(str(p), BOLD)}  {DIM}{gb:,.0f} GB free{RESET}")
    print(f"  {len(vols) + 1}. Enter a path manually")

    pick = ask(f"Choose 1-{len(vols) + 1}", "1")
    try:
        idx = int(pick) - 1
    except ValueError:
        idx = 0

    if 0 <= idx < len(vols):
        base = vols[idx][0]
        default_sub = "Movies" if kind == "movies" else "TV Shows"
        sub = ask("Subfolder on that volume", default_sub)
        target = base / sub
    else:
        target = Path(ask("Full path")).expanduser()

    target.mkdir(parents=True, exist_ok=True)
    store.set(key, str(target))
    return target


# ---------------------------------------------------------------- disc scan

class Title:
    def __init__(self, ix, seconds, vts, ttn, audio_count, chapters):
        self.ix = ix
        self.seconds = seconds
        self.vts = vts
        self.ttn = ttn
        self.audio_count = audio_count
        self.chapters = chapters
        self.selected = False
        self.duplicate_of = None
        self.reason = ""

    @property
    def length(self):
        return hhmmss(self.seconds)


def disc_volume_name(dev):
    """Volume label for a disc, from diskutil. Works for DVD and Blu-ray."""
    try:
        r = subprocess.run(["diskutil", "info", dev],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"Volume Name:\s*(.+)", r.stdout)
        if m:
            name = m.group(1).strip()
            if name and name.lower() != "not applicable":
                return name
    except (OSError, subprocess.SubprocessError):
        pass
    return "DISC"


def scan_disc_makemkv(dev):
    """MakeMKV-backed scan with scan_disc's signature.

    disc_id fingerprints the title layout rather than using a DVD discid,
    since Blu-ray has no equivalent. The same disc always hashes the same.
    """
    titles, notes = scan_makemkv()
    for n in notes:
        print(c("  " + n, DIM))
    if not titles:
        return "DISC", "", []
    mark_makemkv_playalls(titles)
    label = disc_volume_name(dev)
    seed = label + "|" + "|".join(
        f"{t.ix}:{int(t.seconds)}:{getattr(t, 'bytes', 0)}" for t in titles)
    disc_id = hashlib.sha1(seed.encode()).hexdigest()[:16]
    return label, disc_id, titles


def scan_disc(dev):
    """Run lsdvd and parse titles. Returns (disc_label, disc_id, [Title])."""
    print(c("Reading disc structure (this retrieves CSS keys, may take a moment)...", DIM))
    r = subprocess.run(["lsdvd", "-Ox", "-x", dev],
                       capture_output=True, text=True, timeout=300)
    xml_text = r.stdout

    if "lsdvd" not in xml_text:
        # Fall back to plain output parsing.
        r2 = subprocess.run(["lsdvd", dev], capture_output=True, text=True, timeout=300)
        return parse_plain(r2.stdout, dev)

    # lsdvd's XML is occasionally malformed; strip control chars and retry.
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    xml_text = xml_text[xml_text.find("<lsdvd>"):]

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return parse_plain(
            subprocess.run(["lsdvd", dev], capture_output=True,
                           text=True, timeout=300).stdout, dev)

    label = (root.findtext("title") or "DVD").strip()
    disc_id = (root.findtext("discid") or "").strip()

    titles = []
    for t in root.findall("track"):
        try:
            ix = int(t.findtext("ix"))
            seconds = float(t.findtext("length") or 0)
        except (TypeError, ValueError):
            continue
        vts = int(t.findtext("vts") or 0)
        ttn = int(t.findtext("ttn") or 0)
        audio = len(t.findall("audio"))
        chapters = len(t.findall("chapter"))
        titles.append(Title(ix, seconds, vts, ttn, audio, chapters))

    return label, disc_id, titles


def parse_plain(text, dev):
    label = "DVD"
    disc_id = ""
    m = re.search(r"Disc Title:\s*(.+)", text)
    if m:
        label = m.group(1).strip()
    m = re.search(r"DVDDiscID:\s*(\w+)", text)
    if m:
        disc_id = m.group(1)

    titles = []
    for m in re.finditer(
            r"Title:\s*(\d+),\s*Length:\s*(\d+):(\d+):(\d+)\.(\d+).*?"
            r"Chapters:\s*(\d+).*?Audio streams:\s*(\d+)", text):
        ix = int(m.group(1))
        seconds = (int(m.group(2)) * 3600 + int(m.group(3)) * 60
                   + int(m.group(4)) + int(m.group(5)) / 1000)
        titles.append(Title(ix, seconds, 0, 0, int(m.group(7)), int(m.group(6))))

    if not titles:
        for m in re.finditer(r"Title:\s*(\d+),\s*Length:\s*(\d+):(\d+):(\d+)", text):
            seconds = (int(m.group(2)) * 3600 + int(m.group(3)) * 60
                       + int(m.group(4)))
            titles.append(Title(int(m.group(1)), seconds, 0, 0, 0, 0))

    if titles and not any(t.vts for t in titles):
        print(c("Note: VTS mapping unavailable from this lsdvd output. "
                "Will match by size after backup.", YELLOW))
    return label, disc_id, titles


def find_clusters(titles):
    """Group non-duplicate titles by similar runtime. Longest group first."""
    pool = sorted([t for t in titles
                   if not t.duplicate_of and FLOOR_SEC <= t.seconds <= CEILING_SEC],
                  key=lambda x: x.seconds)
    clusters = []
    for t in pool:
        placed = False
        for group in clusters:
            anchor = group[0].seconds
            if abs(t.seconds - anchor) <= anchor * CLUSTER_TOLERANCE:
                group.append(t)
                placed = True
                break
        if not placed:
            clusters.append([t])
    # Most members wins; ties broken by longer runtime.
    clusters.sort(key=lambda g: (len(g), g[0].seconds), reverse=True)
    return clusters


def fingerprint(titles):
    """A stable per-disc id derived from its title layout. Used when the
    disc carries no DVDDiscID, which is common on box sets."""
    parts = [f"{t.ix}:{int(t.seconds)}" for t in sorted(titles, key=lambda x: x.ix)]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def structural_id(t):
    """A title's true identity on the disc.

    lsdvd reports vts (which video title set) and ttn (title number inside
    that set). Two tracks sharing both point at the same content - this is
    what MakeMKV means by "title #7 in VTS 1 is equal to title #5".
    Runtime is NOT identity: two different episodes routinely run the same
    length to the frame.
    """
    if t.vts and t.ttn:
        return (t.vts, t.ttn)
    return None


def mark_duplicates(titles):
    """Flag only titles that are structurally identical.

    When vts/ttn are unavailable nothing is marked. Ripping one title twice
    costs a few minutes; silently dropping an episode costs a re-rip you
    will not notice for weeks.
    """
    seen = {}
    structural = False
    for t in titles:
        key = structural_id(t)
        if key is None:
            continue
        structural = True
        if key in seen:
            t.duplicate_of = seen[key]
            t.reason = f"duplicate of title {seen[key]} (same VTS {t.vts}/{t.ttn})"
        else:
            seen[key] = t.ix
    return structural


def coverage_report(titles, ep_len, ep_count):
    """Does the play-all account for exactly the episodes we selected?

    A disc's play-all title is the whole side end to end. If selected
    episodes x average length does not reach it, something is not selected.
    This is the check that catches a missing episode before you eject.
    """
    playall = None
    for t in titles:
        if t.duplicate_of or t.selected:
            continue
        if ep_len and t.seconds >= ep_len * 1.8:
            if playall is None or t.seconds > playall.seconds:
                playall = t
    if not playall or not ep_len or not ep_count:
        return []

    selected = ep_len * ep_count
    gap = playall.seconds - selected
    lines = []
    lines.append(f"play-all (title {playall.ix}) runs {hhmmss(playall.seconds)}")
    lines.append(f"{ep_count} selected x ~{hhmmss(ep_len)} = {hhmmss(selected)}")

    if abs(gap) <= max(90, ep_len * 0.15):
        lines.append("ACCOUNTED FOR - every episode on this side is selected")
    elif gap > 0:
        n = gap / ep_len
        lines.append(f"UNACCOUNTED: {hhmmss(gap)} - about {n:.1f} more episode(s)")
        lines.append("check the greyed-out titles before you continue")
    else:
        lines.append(f"selected exceeds play-all by {hhmmss(-gap)} - "
                     "a duplicate may be checked")
    return lines


def classify(titles, mode):
    """Mark duplicates and pre-select likely keepers.

    Episode length is inferred from the disc rather than assumed, so 11-minute
    shorts, 22-minute comedies and 44-minute dramas all work without config.
    """
    structural = mark_duplicates(titles)
    if not structural:
        print(c("No VTS data from lsdvd - duplicates cannot be detected "
                "safely, so none are hidden. Expect repeated titles.", YELLOW))

    if mode == "movie":
        # Blu-ray often carries the same feature several times with
        # different audio sets. Same length, so break the tie on the
        # richest audio track count.
        longest = max((t for t in titles if not t.duplicate_of),
                      key=lambda x: (round(x.seconds / 60), x.audio_count),
                      default=None)
        for t in titles:
            if t is longest:
                t.selected = True
                t.reason = "main feature (longest title)"
            elif not t.reason:
                t.reason = "extra"
        return titles

    clusters = find_clusters(titles)
    winner = clusters[0] if clusters and len(clusters[0]) >= 2 else None

    if winner:
        ep_len = sum(t.seconds for t in winner) / len(winner)
        ep_chap = sum(t.chapters for t in winner) / len(winner)
        keep = {t.ix for t in winner}
        for t in titles:
            if t.ix in keep:
                t.selected = True
                t.reason = f"episode ({len(winner)} at ~{hhmmss(ep_len)})"
            elif t.duplicate_of:
                continue
            elif t.seconds < FLOOR_SEC:
                t.reason = "short (logo/promo)"
            else:
                mult = t.seconds / ep_len
                near = round(mult)
                # A play-all is the episodes back to back: its runtime is a
                # whole multiple of one episode and its chapter count scales
                # the same way. A supersized episode is ~2x but keeps roughly
                # one episode's chapter count.
                if near >= 2 and abs(mult - near) <= 0.12:
                    chap_mult = (t.chapters / ep_chap) if ep_chap else 0
                    if chap_mult >= near * 0.6:
                        t.reason = f"PLAY-ALL ({near} episodes joined) - skip this"
                    else:
                        t.reason = (f"{near} episodes in one title - "
                                    f"rip and split, or name S..E..-E..")
                        t.selected = True
                elif mult > 1.25:
                    t.reason = "long (bonus reel) - review"
                else:
                    t.reason = "odd length - check this one"

        for line in coverage_report(titles, ep_len, len(winner)):
            print(c("  " + line, CYAN))
    else:
        # One episode on the disc, or runtimes all over the place.
        for t in titles:
            if t.duplicate_of:
                continue
            if t.seconds < FLOOR_SEC:
                t.reason = "short (logo/promo)"
            elif t.seconds > CEILING_SEC:
                t.reason = "very long (bonus feature)"
            else:
                t.selected = True
                t.reason = "possible content - review"
        print(c("No repeating episode length found on this disc. "
                "Everything plausible is checked - review before continuing.",
                YELLOW))

    return titles


# ---------------------------------------------------------------- picker

HELP_LINES = [
    "up/down or j/k  move        space  toggle        a  select all",
    "n  select none   i  invert   s  select suggested  enter  confirm   q  cancel",
]


def pick_titles(stdscr, titles, header):
    curses.curs_set(0)
    curses.use_default_colors()
    for i in range(1, 5):
        try:
            curses.init_pair(i, [0, curses.COLOR_GREEN, curses.COLOR_YELLOW,
                                 curses.COLOR_CYAN, curses.COLOR_RED][i], -1)
        except curses.error:
            pass

    suggested = [t.selected for t in titles]
    pos = 0
    top = 0

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        rows = max(3, height - 7)

        stdscr.addnstr(0, 0, header[:width - 1], width - 1, curses.A_BOLD)
        count = sum(1 for t in titles if t.selected)
        total = sum(t.seconds for t in titles if t.selected)
        stdscr.addnstr(1, 0,
                       f"{count} selected  ({hhmmss(total)} total)",
                       width - 1, curses.A_DIM)

        if pos < top:
            top = pos
        elif pos >= top + rows:
            top = pos - rows + 1

        for row, t in enumerate(titles[top:top + rows]):
            idx = top + row
            box = "[x]" if t.selected else "[ ]"
            line = (f" {box} Title {t.ix:02d}  {t.length:>8}  "
                    f"{t.audio_count} audio  {t.chapters:>2} ch")
            if t.vts:
                line += f"  VTS {t.vts:02d}"
            if t.reason:
                line += f"   {t.reason}"

            attr = curses.A_REVERSE if idx == pos else curses.A_NORMAL
            if t.duplicate_of and idx != pos:
                attr |= curses.A_DIM
            try:
                stdscr.addnstr(row + 3, 0, line[:width - 1], width - 1, attr)
            except curses.error:
                pass

        for i, line in enumerate(HELP_LINES):
            try:
                stdscr.addnstr(height - 2 + i, 0, line[:width - 1],
                               width - 1, curses.A_DIM)
            except curses.error:
                pass

        stdscr.refresh()
        key = stdscr.getch()

        if key in (curses.KEY_DOWN, ord("j")):
            pos = min(pos + 1, len(titles) - 1)
        elif key in (curses.KEY_UP, ord("k")):
            pos = max(pos - 1, 0)
        elif key == curses.KEY_NPAGE:
            pos = min(pos + rows, len(titles) - 1)
        elif key == curses.KEY_PPAGE:
            pos = max(pos - rows, 0)
        elif key == ord(" "):
            titles[pos].selected = not titles[pos].selected
        elif key == ord("a"):
            for t in titles:
                t.selected = True
        elif key == ord("n"):
            for t in titles:
                t.selected = False
        elif key == ord("i"):
            for t in titles:
                t.selected = not t.selected
        elif key == ord("s"):
            for t, s in zip(titles, suggested):
                t.selected = s
        elif key in (curses.KEY_ENTER, 10, 13):
            return True
        elif key in (ord("q"), 27):
            return False


def fallback_picker(titles, header):
    """Used if curses cannot start."""
    print(header)
    for t in titles:
        mark = "x" if t.selected else " "
        print(f"  [{mark}] {t.ix:02d}  {t.length:>8}  {t.audio_count} audio  {t.reason}")
    print("\nEnter titles to keep, e.g. 1,3-6,10   (blank accepts the suggestions)")
    raw = ask("Keep")
    if not raw:
        return True
    wanted = set()
    for chunk in raw.replace(" ", "").split(","):
        if "-" in chunk:
            try:
                a, b = chunk.split("-")
                wanted.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        elif chunk.isdigit():
            wanted.add(int(chunk))
    for t in titles:
        t.selected = t.ix in wanted
    return True


# ---------------------------------------------------------------- backup

# ---------------------------------------------------------------- tvmaze

TVMAZE = "https://api.tvmaze.com"


def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "dvdrip/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tvmaze_search(query):
    """Return [(id, name, year, network)] for a show name."""
    q = urllib.parse.quote(query)
    try:
        results = http_json(f"{TVMAZE}/search/shows?q={q}")
    except Exception as e:
        print(c(f"  Lookup failed: {e}", YELLOW))
        return []
    out = []
    for item in results[:6]:
        s = item.get("show", {})
        net = (s.get("network") or s.get("webChannel") or {})
        country = (net.get("country") or {}).get("code", "")
        premiered = (s.get("premiered") or "")[:4]
        out.append((s.get("id"), s.get("name", "?"), premiered,
                    f"{net.get('name', '')} {country}".strip()))
    return out


PART_RE = re.compile(
    r"\s*(?:\(\s*(?:part\s*)?[0-9ivx]+\s*\)"
    r"|\[\s*(?:part\s*)?[0-9ivx]+\s*\]"
    r"|,?\s*part\s+[0-9ivx]+"
    r"|\s+pt\.?\s*[0-9ivx]+)\s*$", re.I)


def strip_part_suffix(name):
    out = PART_RE.sub("", name or "").strip()
    return out or (name or "")


def group_aired_together(eps):
    """Two TVMaze entries sharing an airdate were one broadcast."""
    eps = sorted(eps, key=lambda e: e.get("number") or 0)
    runs, i = [], 0
    while i < len(eps):
        run = [eps[i]]; j = i + 1
        while j < len(eps):
            prev, cur = eps[j - 1], eps[j]
            if (cur.get("airdate") and cur.get("airdate") == prev.get("airdate")
                    and (cur.get("number") or 0) == (prev.get("number") or 0) + 1
                    and strip_part_suffix(cur.get("name", "")).lower()
                        == strip_part_suffix(prev.get("name", "")).lower()):
                run.append(cur); j += 1
            else:
                break
        runs.append(run); i = j
    return runs


def run_title(run):
    if len(run) == 1:
        return run[0].get("name") or ""
    names = [strip_part_suffix(e.get("name", "")) for e in run]
    uniq = list(dict.fromkeys(n for n in names if n))
    return uniq[0] if len(uniq) == 1 else " + ".join(uniq)


def numbered_map(eps, merge=True):
    if not merge:
        return {e["number"]: e.get("name") or "" for e in eps if e.get("number")}
    return {slot: run_title(run)
            for slot, run in enumerate(group_aired_together(eps), start=1)}


def tvmaze_episodes(show_id, season, merge=True):
    """Return {episode_number: title} for one season."""
    try:
        eps = http_json(f"{TVMAZE}/shows/{show_id}/episodes")
    except Exception as e:
        print(c(f"  Episode lookup failed: {e}", YELLOW))
        return {}
    season_eps = [e for e in eps
                  if e.get("season") == season and e.get("number")]
    merged = numbered_map(season_eps, merge)
    if merge and len(merged) != len(season_eps):
        print(c(f"  {len(season_eps) - len(merged)} hour-long episode(s) "
                f"counted as one slot - season has {len(merged)} slots.", DIM))
    return merged


def normalize_title(s):
    """Reduce a disc label or folder name to comparable words."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[_\-\.]+", " ", s)
    s = re.sub(r"\b(season|seasons|disc|disk|dvd|vol|volume|the complete)\b"
               r"\s*\d*", " ", s)
    s = re.sub(r"\bs\d+\b|\bd\d+\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def match_show_folder(label, folders):
    """Best-guess library folder for a disc label. Returns (folder, 0-1)."""
    want = normalize_title(label)
    if not want:
        return None, 0.0
    best, score = None, 0.0
    for f in folders:
        have = normalize_title(f)
        if not have:
            continue
        ratio = difflib.SequenceMatcher(None, want, have).ratio()
        if have == want or have.startswith(want + " ") or want.startswith(have + " "):
            ratio = max(ratio, 0.95)
        if ratio > score:
            best, score = f, ratio
    return best, score


def tvmaze_season_map(show_id):
    """{season: {episode_number: title}} for the whole series."""
    try:
        eps = http_json(f"{TVMAZE}/shows/{show_id}/episodes")
    except Exception as e:
        print(c(f"  Episode lookup failed: {e}", YELLOW))
        return {}
    by_season = {}
    for e in eps:
        s, n = e.get("season"), e.get("number")
        if s and n:
            by_season.setdefault(s, []).append(e)
    out = {}
    for s, lst in by_season.items():
        out[s] = numbered_map(lst, merge=True)
        for k, v in out[s].items():
            if not v:
                out[s][k] = f"Episode {k}"
    return out


def library_state(show_dir):
    """{season: set(episode numbers already ripped)}"""
    have = {}
    try:
        for sd in show_dir.glob("Season *"):
            m = re.search(r"(\d+)", sd.name)
            if not m:
                continue
            s = int(m.group(1))
            have.setdefault(s, set())
            for f in sd.glob("*.*"):
                if f.suffix.lower() not in (".mp4", ".mkv", ".m4v", ".avi"):
                    continue
                mm = re.search(r"S(\d+)E(\d+)", f.name, re.I)
                if mm and int(mm.group(1)) == s:
                    have[s].add(int(mm.group(2)))
    except OSError:
        pass
    return have


def first_gap(season_map, have):
    """Earliest season with missing episodes -> (season, [missing numbers])."""
    for s in sorted(season_map):
        missing = sorted(set(season_map[s]) - have.get(s, set()))
        if missing:
            return s, missing
    return None, []


def resolve_show_id(store, folder_name):
    """Find and cache the TVmaze id for a library folder name."""
    key = f"tvmaze:{folder_name}"
    cached = store.get(key)
    if cached:
        return int(cached)

    # "The Office (US) (2005)" -> "The Office US"
    query = folder_name
    years = re.findall(r"\((\d{4})\)", query)
    query = re.sub(r"\(\d{4}\)", "", query)
    query = query.replace("(", "").replace(")", "").strip()

    print(c(f"\n  Looking up \"{query}\" on TVmaze...", DIM))
    matches = tvmaze_search(query)
    if not matches:
        print(c("  No matches. Episode titles will default to "
                "'Episode N'.", YELLOW))
        return None

    # Auto-accept when there is one obvious match on name and year.
    if len(matches) == 1:
        best = matches[0]
    else:
        exact = [mm for mm in matches
                 if years and mm[2] == years[-1]]
        if len(exact) == 1:
            best = exact[0]
        else:
            print()
            for i, (sid, nm, yr, net) in enumerate(matches, 1):
                print(f"  {i}. {nm} ({yr})  {DIM}{net}{RESET}")
            print(f"  {len(matches) + 1}. None of these")
            pick = ask("  Which show", "1")
            try:
                idx = int(pick) - 1
            except ValueError:
                idx = 0
            if not (0 <= idx < len(matches)):
                return None
            best = matches[idx]

    sid, nm, yr, net = best
    print(c(f"  Matched: {nm} ({yr}) {net}", GREEN))
    store.set(key, sid)
    return sid


def forget_show_id(store, folder_name):
    store.set(f"tvmaze:{folder_name}", "")


# ---------------------------------------------------------------- copy


def dir_size(path):
    total = 0
    try:
        for f in path.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def highest_vts(path):
    """Highest VTS number that has started copying. dvdbackup works in order,
    so this tells us how far through the disc it got."""
    hi = 0
    try:
        for f in path.rglob("VTS_*_*.VOB"):
            m = re.search(r"VTS_(\d+)_", f.name)
            if m:
                hi = max(hi, int(m.group(1)))
    except OSError:
        pass
    return hi


def vts_dir_of(path):
    p = path / "VIDEO_TS"
    if p.is_dir():
        return p
    try:
        for q in path.rglob("VIDEO_TS"):
            return q
    except OSError:
        pass
    return path


def vts_bytes(path, vts_num):
    """Total bytes currently on disk for one VTS set."""
    try:
        return sum(f.stat().st_size
                   for f in vts_dir_of(path).glob(f"VTS_{vts_num:02d}_*.VOB"))
    except OSError:
        return 0


def vts_started(path, vts_num):
    return vts_bytes(path, vts_num) > 0


class CopyGate:
    """Decides when enough of the disc has been copied.

    Modelled as independent permissives that must ALL be satisfied, so no
    single assumption can release the gate on its own:

      A  every needed VTS set has data on disk
      B  the byte count of every needed set has been stable for STABLE_SECS
      C  copying has demonstrably moved past the needed sets, by any of:
           - a higher VTS set has started, or
           - the needed sets include the last VTS on the disc, or
           - dvdbackup has exited

    Only A and B together are not enough: a set that is mid-write also has
    data. Only C is not enough either: sequential ordering is an assumption
    about dvdbackup, not a guarantee. Requiring all three means a wrong
    assumption degrades to copying more than necessary, never to stopping
    with a truncated file.
    """

    STABLE_SECS = 6

    def __init__(self, needed_vts, total_vts=0):
        self.needed = sorted(set(needed_vts or []))
        self.total_vts = total_vts or 0
        self.sizes = {}
        self.stable_since = {}
        self.blocked_by = "starting"

    def note_total(self, total_vts):
        if total_vts and total_vts > self.total_vts:
            self.total_vts = total_vts

    def progress(self, path):
        return sum(1 for v in self.needed if vts_started(path, v))

    def check(self, path, proc_alive=True):
        if not self.needed:
            self.blocked_by = "no VTS list"
            return False

        now = time.time()

        # --- permissive A: every needed set has data ---
        for v in self.needed:
            if not vts_started(path, v):
                self.blocked_by = f"VTS {v:02d} not started"
                return False

        # --- permissive B: sizes have settled ---
        for v in self.needed:
            size = vts_bytes(path, v)
            if self.sizes.get(v) != size:
                self.sizes[v] = size
                self.stable_since[v] = now
        youngest = min(self.stable_since.get(v, now) for v in self.needed)
        if now - youngest < self.STABLE_SECS:
            self.blocked_by = "still writing"
            return False

        # --- permissive C: copying has moved past them ---
        top = max(self.needed)
        moved_past = highest_vts(path) > top
        is_last = self.total_vts and top >= self.total_vts
        finished = not proc_alive
        if not (moved_past or is_last or finished):
            self.blocked_by = f"still inside VTS {top:02d}"
            return False

        if moved_past:
            self.blocked_by = "done (moved past)"
        elif is_last:
            self.blocked_by = "done (last set on disc)"
        else:
            self.blocked_by = "done (copy finished)"
        return True


def disc_total_bytes(dev):
    try:
        out = subprocess.run(["diskutil", "info", "-plist", dev],
                             capture_output=True, timeout=15).stdout
        d = plistlib.loads(out)
        for key in ("TotalSize", "Size", "IOKitSize"):
            if d.get(key):
                return int(d[key])
    except Exception:
        pass
    return 0


def gb(n):
    return f"{n / 1024 ** 3:.2f} GB"


def run_dvdbackup(dev, name, needed_vts=None, disc_id=None,
                  label=None):
    """Copy the disc, showing live progress and stopping early if the copy
    stalls after the VTS sets we actually need are already complete."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out = WORK_DIR / name
    manifest = out / ".dvdrip.json"

    def folder_has_video(p):
        try:
            return any(p.rglob("VTS_*_[1-9].VOB"))
        except OSError:
            return False

    # A copy may exist under a different name - older builds, or dvdbackup
    # truncating. Look for any folder that actually holds video and either
    # records this disc id or looks like the same show and season.
    if not out.exists() and WORK_DIR.exists():
        stem = name[:14]
        for p in sorted(WORK_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_dir() or not folder_has_video(p):
                continue
            rec = {}
            try:
                rec = json.loads((p / ".dvdrip.json").read_text())
            except Exception:
                pass
            if rec.get("disc_id") == disc_id or p.name.startswith(stem):
                hdr("Found an existing copy of this disc")
                print(f"  Folder: {p}")
                print(f"  Size:   {gb(dir_size(p))}")
                if rec.get("label"):
                    print(f"  From:   {rec['label']}")
                else:
                    print(c("  No disc record, but the name and contents "
                            "match this show.", DIM))
                print(c("\n  Reusing it skips the disc read entirely.", GREEN))
                if confirm("  Reuse it?"):
                    return p
                break

    if out.exists():
        prior = {}
        try:
            prior = json.loads(manifest.read_text())
        except Exception:
            pass

        same_disc = prior.get("disc_id") and prior.get("disc_id") == disc_id
        unknown = not prior.get("disc_id")
        age = ""
        try:
            mins = (time.time() - out.stat().st_mtime) / 60
            age = f"{mins / 60:.1f} hours old" if mins > 90 else f"{mins:.0f} minutes old"
        except OSError:
            pass
        size = dir_size(out)

        hdr("A previous disc copy is in the way")
        print(f"  Folder:  {out}")
        print(f"  Size:    {gb(size)}   {age}")
        if prior.get("label"):
            print(f"  From:    {prior['label']}  "
                  f"{DIM}({prior.get('note', 'unknown disc')}){RESET}")

        if same_disc:
            print(c("\n  This is the SAME disc that is in the drive now.", GREEN))
            print("  Reusing it skips the copy and goes straight to encoding.")
            if confirm("  Reuse it?"):
                return out
        elif unknown:
            print(c("\n  This copy has no record of which disc it came from.",
                    YELLOW))
            print("  If it is the disc now in the drive, reusing it saves "
                  "a full re-read.")
            print(c("\n  Your finished videos in the Plex library are not "
                    "affected either way.", DIM))
            if confirm("  Reuse it?", False):
                return out
        else:
            print(c("\n  This is a DIFFERENT disc from the one in the drive.",
                    YELLOW))
            print("  Reusing it would encode the wrong episodes.")
            print(c("\n  Your finished videos in the Plex library are not "
                    "affected either way.", DIM))
            print("  This folder only holds raw scratch files already "
                  "converted to MP4.")
            if not confirm("  Replace it with the disc in the drive?"):
                print(c("  Cancelled. Nothing changed.", DIM))
                return None

        print(c(f"  Removing {gb(size)} of old scratch files...", DIM))
        shutil.rmtree(out)

    needed_max = max(needed_vts) if needed_vts else 0

    hdr(f"Copying disc to {out}")
    total = disc_total_bytes(dev)
    if total:
        print(f"  Media size: {c(gb(total), BOLD)}")
    if needed_max:
        want = ", ".join(f"{v:02d}" for v in sorted(set(needed_vts)))
        print(f"  Needed VTS sets: {c(want, BOLD)}")
    print(c(f"  Watchdog: stops waiting after {STALL_SECONDS}s of no progress",
            DIM))
    print()

    cmd = ["dvdbackup", "-i", dev, "-o", str(WORK_DIR), "-M", "-n", name]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)

    def write_manifest():
        try:
            out.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({
                "disc_id": disc_id,
                "label": label,
                "note": time.strftime("copied %Y-%m-%d %H:%M"),
                "needed_vts": needed_vts or [],
            }, indent=2))
        except OSError:
            pass

    state = {"keys": 0, "vts_found": 0, "last": ""}

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if "Get key for" in line:
                state["keys"] += 1
            elif "Found" in line and "VTS" in line:
                m = re.search(r"Found (\d+) VTS", line)
                if m:
                    state["vts_found"] = int(m.group(1))
            elif line and "Elapsed time" not in line:
                state["last"] = line[:70]

    threading.Thread(target=reader, daemon=True).start()

    write_manifest()
    last_size = -1
    last_change = time.time()
    started = time.time()
    stall_checked = False
    stopped_early = False
    rate = 0.0
    spin = Spinner()
    board = Board(10)
    keys = Keys()
    keys.__enter__()
    paused = False
    paused_total = 0.0
    pause_began = 0.0
    user_stopped = False

    def paint(size, rate, idle, now):
        cur = highest_vts(out)
        pct = (size / total * 100) if total else 0
        elapsed = now - started
        remaining = ((total - size) / rate) if (rate > 0 and total) else 0
        top, bot = disc_art(spin)

        if size == 0:
            stage = f"reading CSS keys   {BOLD}{state['keys']}{RESET} found"
            barline = f"   {bar(0)}  {DIM}waiting{RESET}"
        else:
            stage = (f"copying   {BOLD}{gb(size)}{RESET}"
                     + (f" of {gb(total)}" if total else ""))
            barline = f"   {bar(pct)} {pct:5.1f}%"

        vts_line = f"   {DIM}now on{RESET} VTS {cur:02d}" if size else "   "
        if needed_max:
            got = gate.progress(out)
            vts_line += (f"   {DIM}needed sets{RESET} "
                         f"{BOLD}{got}/{len(gate.needed)}{RESET}"
                         f"   {DIM}{gate.blocked_by}{RESET}")
        if rate > 0:
            vts_line += f"   {BOLD}{rate / 1024 ** 2:.0f} MB/s{RESET}"
        if idle > 20:
            vts_line += c(f"   idle {int(idle)}s", YELLOW)

        if paused:
            top = bot = f"{YELLOW}||{RESET}"
            head = f"{YELLOW}{BOLD}PAUSED{RESET}      "
        else:
            head = f"{BOLD}READING DISC{RESET}"

        board.render([
            f"  {top}   {head}   {DIM}drive {dev}{RESET}",
            f"  {bot}   {c(label or 'disc', BCYAN)}",
            "",
            f"   {stage}",
            barline,
            vts_line,
            "",
            f"   {DIM}elapsed{RESET} {mmss(elapsed)}    "
            f"{DIM}remaining{RESET} ~{mmss(remaining)}    "
            f"{DIM}done at{RESET} {eta_clock(remaining)}",
            "",
            buttons(paused),
        ])

    def poll_keys():
        nonlocal paused, paused_total, pause_began, user_stopped
        k = keys.get()
        if k == "p":
            if paused:
                paused_total += time.time() - pause_began
                paused = False
                try:
                    proc.send_signal(signal.SIGCONT)
                except Exception:
                    pass
            else:
                paused = True
                pause_began = time.time()
                try:
                    proc.send_signal(signal.SIGSTOP)
                except Exception:
                    pass
        elif k == "q":
            user_stopped = True
            try:
                if paused:
                    proc.send_signal(signal.SIGCONT)
                proc.terminate()
            except Exception:
                pass

    gate = CopyGate(needed_vts or [], total_vts=0)
    last_keys = -1
    try:
        while proc.poll() is None:
            for _ in range(int(POLL_SECONDS / 0.1)):
                time.sleep(0.1)
                poll_keys()
                if paused or user_stopped:
                    break
            if user_stopped:
                stopped_early = True
                break
            while paused and not user_stopped:
                paint(last_size if last_size > 0 else 0, 0, 0, time.time())
                time.sleep(0.1)
                poll_keys()
                last_change = time.time()
            size = dir_size(out) if out.exists() else 0
            now = time.time()

            # Retrieving CSS keys writes no files but is real work. Count a
            # rising key tally as progress so the watchdog stays quiet.
            if state["keys"] != last_keys:
                last_keys = state["keys"]
                last_change = now
                stall_checked = False

            if size != last_size:
                if last_size >= 0 and size > last_size:
                    span = now - last_change
                    if span > 0:
                        rate = (size - last_size) / span
                last_size = size
                last_change = now
            idle = now - last_change

            # Stop as soon as the interlocks agree we have everything.
            gate.note_total(state.get("vts_found", 0))
            if gate.check(out, proc_alive=proc.poll() is None):
                board.close()
                print()
                print(c(f"\n  Every needed VTS set is complete "
                        f"({gb(size)}).", GREEN))
                print(c(f"  Reason: {gate.blocked_by}. Stopping the read - "
                        f"the rest is extras.", DIM))
                proc.terminate()
                stopped_early = True
                break

            paint(size, rate, idle, now)

            # ---- watchdog ----
            if idle > STALL_SECONDS and not stall_checked:
                stall_checked = True
                board.close()
                board.drawn = False
                cur = highest_vts(out)
                print()
                if gate.check(out, proc_alive=proc.poll() is None):
                    print(c(f"\n  No progress for {int(idle)}s, but VTS "
                            f"{needed_max:02d} finished a while ago "
                            f"(now on {cur:02d}).", GREEN))
                    print(c("  Everything needed is already copied. "
                            "Stopping the copy.", GREEN))
                    proc.terminate()
                    stopped_early = True
                    break

                print(c(f"\n  No progress for {int(idle)}s.", YELLOW))
                if size == 0:
                    print(c("  Nothing has copied yet. The drive may be "
                            "struggling to read this disc.", YELLOW))
                    print(c("  Keys retrieved so far: "
                            f"{state['keys']}", DIM))
                print(f"  Copied so far: {gb(size)}, currently on VTS {cur:02d}")
                if needed_max:
                    print(f"  You need through VTS {needed_max:02d}.")
                print("\n  1. Keep waiting (resets the timer)")
                print("  2. Stop copying and encode what is here")
                print("  3. Abort and return to the menu")
                pick = ask("  Choice", "1")
                if pick == "2":
                    proc.terminate()
                    stopped_early = True
                    break
                if pick == "3":
                    proc.terminate()
                    proc.wait(timeout=10)
                    print(c("  Aborted. Partial copy kept in " + str(out), DIM))
                    return None
                last_change = time.time()
                stall_checked = False
                print()

    except KeyboardInterrupt:
        board.close()
        print(c("\n  Interrupted - stopping the copy.", YELLOW))
        proc.terminate()
        stopped_early = True
    finally:
        board.close()
        keys.restore()

    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    print()
    final = dir_size(out) if out.exists() else 0
    print(f"  Copied {c(gb(final), BOLD)} in "
          f"{hhmmss(time.time() - started)}")

    if proc.returncode not in (0, None) and not stopped_early:
        print(c(f"  dvdbackup exited with code {proc.returncode}", YELLOW))

    # dvdbackup may have written somewhere else (it truncates long names),
    # so locate the folder that actually holds VOB files.
    def has_video(p):
        try:
            return any(p.rglob("VTS_*_[1-9].VOB"))
        except OSError:
            return False

    if not has_video(out):
        stem = out.name[:20]
        candidates = [p for p in WORK_DIR.iterdir()
                      if p.is_dir() and p.name.startswith(stem) and has_video(p)]
        if not candidates:
            candidates = [p for p in WORK_DIR.iterdir()
                          if p.is_dir() and has_video(p)]
        if candidates:
            actual = max(candidates, key=lambda p: p.stat().st_mtime)
            print(c(f"  Video landed in {actual.name}", YELLOW))
            # Move the manifest across and clear the empty stub.
            try:
                if manifest.exists() and not (actual / ".dvdrip.json").exists():
                    shutil.copy2(manifest, actual / ".dvdrip.json")
                if out.exists() and out != actual and not has_video(out):
                    shutil.rmtree(out, ignore_errors=True)
            except OSError:
                pass
            out = actual
        else:
            print(c("  Nothing was copied.", RED))
            return None

    # Verify the sets we actually need survived.
    if needed_vts:
        vts_dir = video_ts(out)
        missing = []
        for v in sorted(set(needed_vts)):
            if not glob.glob(str(vts_dir / f"VTS_{v:02d}_[1-9].VOB")):
                missing.append(v)
        if missing:
            names = ", ".join(f"{v:02d}" for v in missing)
            print(c(f"  Missing VTS sets: {names}", RED))
            if not confirm("  Continue anyway?", False):
                return None
        else:
            print(c("  All needed VTS sets present.", GREEN))

    return out


def video_ts(backup_dir):
    for p in backup_dir.rglob("VIDEO_TS"):
        return p
    return backup_dir


# ---------------------------------------------------------------- encode

def vts_inputs(vts_path, vts_num):
    """Return an ffmpeg concat: URL for a VTS set, plus total byte size."""
    pattern = str(vts_path / f"VTS_{vts_num:02d}_[1-9].VOB")
    files = sorted(glob.glob(pattern))
    if not files:
        return None, 0
    size = sum(os.path.getsize(f) for f in files)
    return "concat:" + "|".join(files), size


def map_titles_to_vts(titles, vts_path):
    """Fill in .vts where lsdvd did not provide it, by matching sizes to order."""
    if all(t.vts for t in titles if t.selected):
        return

    sets = {}
    for f in glob.glob(str(vts_path / "VTS_*_[1-9].VOB")):
        m = re.search(r"VTS_(\d+)_", os.path.basename(f))
        if m:
            n = int(m.group(1))
            sets[n] = sets.get(n, 0) + os.path.getsize(f)

    big = sorted([n for n, sz in sets.items() if sz > 300 * 1024 * 1024])
    selected = [t for t in titles if t.selected]
    if len(big) >= len(selected):
        for t, n in zip(selected, big):
            if not t.vts:
                t.vts = n
        print(c("VTS numbers inferred from file sizes - verify the spot checks.", YELLOW))


def encode(src_url, out_path, start=None, duration=None, label="",
           board=None, index=1, count=1, done_before=0.0, total_all=0.0,
           drive="", ep_label="", keys=None):
    """Returns 'ok', 'failed', 'skipped' or 'aborted'."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin",
           "-analyzeduration", "200M", "-probesize", "200M",
           "-i", src_url]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += ENCODE_ARGS
    cmd += ["-y", str(out_path)]

    spin = Spinner()
    started = time.time()
    target = duration or 0
    done = 0.0
    paused = False
    paused_total = 0.0
    pause_began = 0.0
    outcome = None

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, bufsize=1)

    def working_time():
        extra = (time.time() - pause_began) if paused else 0
        return time.time() - started - paused_total - extra

    def paint():
        elapsed = working_time()
        speed = done / elapsed if elapsed > 0.5 else 0
        this_pct = (done / target * 100) if target else 0
        all_done = done_before + done
        all_pct = (all_done / total_all * 100) if total_all else 0
        remaining = ((total_all - all_done) / speed) if speed > 0 else 0

        top, bot = disc_art(spin)
        if paused:
            top = bot = f"{YELLOW}||{RESET}"
            headline = f"{YELLOW}{BOLD}PAUSED{RESET}"
            barcolor = YELLOW
        else:
            headline = f"{BOLD}ENCODING{RESET}"
            barcolor = BGREEN

        board.render([
            f"  {top}   {headline}    {DIM}drive {drive}{RESET}",
            f"  {bot}   {c(ep_label, BCYAN)}",
            "",
            f"   {DIM}episode {index} of {count}{RESET}",
            f"   this one   {bar(this_pct, color=barcolor)} {this_pct:5.1f}%"
            f"   {BOLD}{speed:4.1f}x{RESET}",
            f"   all of them{bar(all_pct, color=barcolor)} {all_pct:5.1f}%",
            "",
            f"   {DIM}elapsed{RESET} {mmss(elapsed)}    "
            f"{DIM}remaining{RESET} ~{mmss(remaining)}    "
            f"{DIM}done at{RESET} {eta_clock(remaining)}",
            "",
            buttons(paused, extra=("s", "skip this one")),
        ])

    def poll_keys():
        nonlocal paused, paused_total, pause_began, outcome
        if not keys:
            return
        k = keys.get()
        if k == "p":
            if paused:
                paused_total += time.time() - pause_began
                paused = False
                try:
                    proc.send_signal(signal.SIGCONT)
                except Exception:
                    pass
            else:
                paused = True
                pause_began = time.time()
                try:
                    proc.send_signal(signal.SIGSTOP)
                except Exception:
                    pass
        elif k == "s":
            outcome = "skipped"
            _release()
        elif k == "q":
            outcome = "aborted"
            _release()

    def _release():
        try:
            if paused:
                proc.send_signal(signal.SIGCONT)
            proc.terminate()
        except Exception:
            pass

    last_paint = 0.0
    try:
        for line in proc.stderr:
            m = re.search(r"time=(\d+):(\d+):(\d+)", line)
            if m:
                done = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                        + int(m.group(3)))
            poll_keys()
            if outcome:
                break
            while paused and not outcome:
                paint()
                time.sleep(0.1)
                poll_keys()
            now = time.time()
            if now - last_paint > 0.1:
                paint()
                last_paint = now
    except KeyboardInterrupt:
        outcome = "aborted"
        _release()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    if outcome:
        return outcome
    done = target
    paint()
    return "ok" if proc.returncode == 0 else "failed"


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def sanitize(name):
    return re.sub(r'[/\\:*?"<>|]', "-", name).strip()


# ---------------------------------------------------------------- workflows

def auto_plan(store, lib, disc_label, n_titles):
    """Work out show, season and episode numbers with no typing.
    Returns a dict, or None if it cannot make a confident guess."""
    folders = subfolders(lib)
    if not folders:
        return None

    show, score = match_show_folder(disc_label, folders)
    if not show or score < 0.6:
        return None

    show_id = resolve_show_id(store, show)
    if not show_id:
        return None

    season_map = tvmaze_season_map(show_id)
    if not season_map:
        return None

    have = library_state(lib / show)
    season, missing = first_gap(season_map, have)
    if not season:
        return {"show": show, "score": score, "season_map": season_map,
                "have": have, "complete": True}

    take = missing[:n_titles]
    return {
        "show": show,
        "score": score,
        "season": season,
        "season_map": season_map,
        "have": have,
        "missing": missing,
        "take": take,
        "titles": season_map[season],
        "complete": False,
    }


def print_library_status(plan, focus_season=None):
    season_map, have = plan["season_map"], plan["have"]
    print(f"\n  {BOLD}Library status{RESET}")
    for s in sorted(season_map):
        total = len(season_map[s])
        done = len(have.get(s, set()) & set(season_map[s]))
        missing = sorted(set(season_map[s]) - have.get(s, set()))
        if done == total:
            state = c("complete", GREEN)
        elif done == 0:
            state = c("nothing yet", DIM)
        else:
            state = c(f"missing {len(missing)}", YELLOW)
        mark = c(" <-", BCYAN) if s == focus_season else ""
        print(f"    Season {s:02d}   {done:>2} of {total:<3} {state}{mark}")


def tv_workflow(store, dev, label, disc_id, titles):
    lib = choose_library(store, "tv")
    selected = [t for t in titles if t.selected]

    # ---- automatic path ----
    plan = None
    if store.get("auto_plan", "1") == "1":
        print(c("\n  Working out what this disc is...", DIM))
        plan = auto_plan(store, lib, label, len(selected))

    if plan and plan.get("complete"):
        hdr("Nothing missing")
        print(f"  Matched: {c(plan['show'], BOLD)}")
        print_library_status(plan)
        print(c("\n  Every episode this show has is already in your library.",
                GREEN))
        if not confirm("  Set it up by hand anyway?", False):
            return
        plan = None

    if plan:
        show = plan["show"]
        season = plan["season"]
        take = plan["take"]
        missing = plan["missing"]

        hdr("Disc identified")
        print(f"  Disc label:  {c(label, BOLD)}")
        print(f"  Matched to:  {c(show, BOLD)}   "
              f"{DIM}{plan['score']:.0%} confidence{RESET}")
        print_library_status(plan, focus_season=season)

        print(f"\n  This disc has {c(str(len(selected)), BOLD)} "
              f"episode-length titles.")
        print(f"  Season {season:02d} is missing {c(str(len(missing)), BOLD)}: "
              f"{DIM}{', '.join('E%02d' % e for e in missing[:12])}"
              f"{'...' if len(missing) > 12 else ''}{RESET}")

        # Sanity checks before trusting the plan.
        warn = None
        if len(selected) > len(missing):
            warn = (f"The disc has {len(selected)} titles but only "
                    f"{len(missing)} are missing. The extra ones would spill "
                    f"past the end of the season.")
        elif len(selected) < len(missing) and len(missing) - len(selected) > 12:
            warn = (f"Only {len(selected)} of {len(missing)} missing episodes "
                    f"are on this disc, which is a big gap. Check the season "
                    f"is right.")

        print(f"\n  {BOLD}Plan{RESET}")
        for t, ep in zip(selected, take):
            name = plan["titles"].get(ep, f"Episode {ep}")
            print(f"    title {t.ix:02d}  {t.length:>8}  ->  "
                  f"S{season:02d}E{ep:02d}  {name}")
        if len(selected) > len(take):
            for t in selected[len(take):]:
                print(c(f"    title {t.ix:02d}  {t.length:>8}  ->  "
                        f"no slot left in season {season:02d}", YELLOW))

        if warn:
            print(c(f"\n  Heads up: {warn}", YELLOW))

        print()
        print("  y = use this plan")
        print("  n = enter everything by hand")
        if confirm("  Use it?", not warn):
            show_dir = lib / show
            season_dir = show_dir / f"Season {season:02d}"
            first_ep = take[0] if take else 1
            store.set("last_show", show)
            names = []
            for t, ep in zip(selected, take):
                nm = sanitize(plan["titles"].get(ep, f"Episode {ep}"))
                fname = f"{show} - S{season:02d}E{ep:02d} - {nm}.mp4"
                names.append((t, season_dir / fname, ep))
            return run_tv_plan(store, dev, disc_id, titles, selected, names,
                               show, season, first_ep, season_dir,
                               disc_label=label)
        print(c("  Falling back to manual entry.", DIM))

    # ---- manual path ----
    # Offer whatever show folders already exist on the library drive.
    on_disk = subfolders(lib)
    remembered = [s for s in store.known_shows() if s not in on_disk]
    options = on_disk + remembered
    last = store.get("last_show")
    default = last if last in options else None

    show = sanitize(pick_or_type(
        "\nShow folder (pick a number, or type a new name)", options, default))
    store.set("last_show", show)

    show_dir = lib / show
    seasons = [s for s in subfolders(show_dir) if s.lower().startswith("season")]
    if seasons:
        print(f"{DIM}  existing: {', '.join(seasons)}{RESET}")

    season = ask_int("Season number", "1", low=0, high=99)
    season_dir = show_dir / f"Season {season:02d}"

    # If episodes are already in that folder, continue after the highest one.
    existing_eps = []
    if season_dir.exists():
        for f in season_dir.glob("*.mp4"):
            m = re.search(rf"S{season:02d}E(\d+)(?:-E?(\d+))?", f.name, re.I)
            if m:
                existing_eps.append(int(m.group(1)))
                if m.group(2):
                    existing_eps.append(int(m.group(2)))
    if existing_eps:
        suggested = max(existing_eps) + 1
        print(c(f"  found E{min(existing_eps):02d}-E{max(existing_eps):02d} "
                f"already in that folder", DIM))
    else:
        suggested = store.next_episode(show, season)

    print(f"{DIM}  (a number - episode titles come next){RESET}")
    first_ep = ask_int("First episode NUMBER on this disc", str(suggested),
                       low=1, high=999)

    # Fetch real episode titles so the prompts are just enter-enter-enter.
    lookup = {}
    if store.get("auto_titles", "1") == "1":
        show_id = resolve_show_id(store, show)
        if show_id:
            lookup = tvmaze_episodes(show_id, season)
            if lookup:
                have = [n for n in range(first_ep, first_ep + len(selected))
                        if n in lookup]
                print(c(f"  Found titles for {len(have)} of "
                        f"{len(selected)} episodes.", GREEN))

    print(f"\n{c('Episode titles', BOLD)} - enter accepts the default in brackets.")
    print(f"{DIM}Cosmetic only; Plex matches on SxxExx.{RESET}\n")

    names = []
    for i, t in enumerate(selected):
        ep = first_ep + i
        default = lookup.get(ep) or f"Episode {ep}"
        title_name = ask(f"  S{season:02d}E{ep:02d}  (title {t.ix}, {t.length})",
                         default)
        fname = f"{show} - S{season:02d}E{ep:02d} - {sanitize(title_name)}.mp4"
        names.append((t, season_dir / fname, ep))

    return run_tv_plan(store, dev, disc_id, titles, selected, names,
                       show, season, first_ep, season_dir,
                       disc_label=label)


# ---------------------------------------------------------------- manifest

MANIFEST_NAME = ".rip-manifest.json"
# Same sample the audit script uses: 4 MB starting 50 MB in. Far enough past
# the header to differ between episodes, cheap enough to run on a whole season.
HASH_OFFSET = 50 * 1024 * 1024
HASH_LEN = 4 * 1024 * 1024


def chunk_hash(path):
    """Short content fingerprint. Empty string if the file is too small."""
    try:
        size = os.path.getsize(path)
        if size < HASH_OFFSET + HASH_LEN:
            offset = max(0, size // 3)
            length = min(HASH_LEN, size - offset)
        else:
            offset, length = HASH_OFFSET, HASH_LEN
        if length <= 0:
            return ""
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            fh.seek(offset)
            h.update(fh.read(length))
        return h.hexdigest()[:16]
    except OSError:
        return ""


def write_episode_manifest(season_dir, show, season, disc_id, disc_label, names,
                   titles_by_ix):
    """Record where each episode came from, next to the episodes themselves.

    Merges into any existing manifest so a season ripped across four discs
    accumulates rather than overwriting. This is the file that lets an audit
    tell 'this is the wrong episode' from 'this episode is fine'.
    """
    path = Path(season_dir) / MANIFEST_NAME
    data = {"show": show, "season": season, "episodes": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data.setdefault("episodes", {})
        except (OSError, ValueError):
            print(c("  Existing manifest unreadable - starting a new one.",
                    YELLOW))
            data = {"show": show, "season": season, "episodes": {}}

    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0
    for t, out_path, ep in names:
        if ep is None or not Path(out_path).exists():
            continue
        src = titles_by_ix.get(t.ix)
        data["episodes"][str(ep)] = {
            "file": Path(out_path).name,
            "source_title": t.ix,
            "vts": getattr(src, "vts", 0) or 0,
            "ttn": getattr(src, "ttn", 0) or 0,
            "src_seconds": round(getattr(src, "seconds", 0) or 0, 1),
            "out_seconds": round(probe_duration(out_path) or 0, 1),
            "chapters": getattr(src, "chapters", 0) or 0,
            "audio": getattr(src, "audio_count", 0) or 0,
            "disc_id": disc_id or "",
            "disc_label": disc_label or "",
            "hash": chunk_hash(out_path),
            "ripped": stamp,
        }
        written += 1

    data["updated"] = stamp
    try:
        path.write_text(json.dumps(data, indent=2, sort_keys=True))
        print(c(f"  Manifest updated ({written} episode(s)) -> "
                f"{MANIFEST_NAME}", DIM))
    except OSError as e:
        print(c(f"  Could not write manifest: {e}", YELLOW))


def run_tv_plan(store, dev, disc_id, titles, selected, names,
                show, season, first_ep, season_dir, disc_label=""):
    """Shared confirm-and-rip tail for both the automatic and manual paths."""
    hdr("Confirm")
    print(f"  Folder: {c(str(season_dir), BOLD)}\n")
    clash = False
    for t, path, ep in names:
        exists = path.exists()
        clash = clash or exists
        flag = c("   ALREADY EXISTS", RED) if exists else ""
        print(f"  title {t.ix:02d} {t.length:>8}  ->  {path.name}{flag}")

    if clash:
        print(c("\n  Files marked above would be overwritten.", YELLOW))

    print(f"\n  {DIM}n returns to the menu with nothing written.{RESET}")
    if not confirm("Start ripping?"):
        print(c("Cancelled. Nothing written.", DIM))
        return

    season_dir.mkdir(parents=True, exist_ok=True)

    # ---- lossless remux path -------------------------------------------
    # rip_makemkv wants the TINFO index. Titles from lsdvd are numbered
    # differently, so this only runs when the scan came from MakeMKV.
    engine = (store.get("rip_engine") or "mkv").lower()
    from_makemkv = bool(names) and all(hasattr(t, "source_id")
                                       for t, _, _ in names)

    if engine == "mkv" and not from_makemkv:
        print(c("  Titles came from lsdvd, whose numbering does not match "
                "MakeMKV's. Using the encode path instead.", YELLOW))
    elif engine == "mkv":
        done = []
        for t, path, ep in names:
            out = path.with_suffix(".mkv")
            print(c(f"\n  title {t.ix} ({t.length}) -> {out.name}", BOLD))

            def _prog(frac):
                sys.stdout.write(f"\r    {frac * 100:5.1f}%")
                sys.stdout.flush()

            produced = rip_makemkv(0, t.ix, season_dir, on_progress=_prog)
            sys.stdout.write("\r" + " " * 24 + "\r")
            if not produced:
                print(c(f"    failed on title {t.ix}", RED))
                continue
            try:
                if out.exists():
                    out.unlink()
                produced.rename(out)
            except OSError as e:
                print(c(f"    could not rename: {e}", YELLOW))
                continue
            done.append((t, out, ep))
            print(c(f"    {out.name}", GREEN))

        if not done:
            print(c("Nothing was written.", RED))
            return
        write_episode_manifest(season_dir, show, season, disc_id,
                               disc_label, done, {t.ix: t for t in titles})
        last_ep = max(ep for _, _, ep in done)
        store.set_next_episode(show, season, last_ep + 1)
        print(c(f"\nNext disc for this season will start at "
                f"E{last_ep + 1:02d}", GREEN))
        if confirm("\nEject and load the next disc now?", False):
            swap_disc(store)
            return "next"
        return

    needed = sorted({t.vts for t in selected if t.vts})
    suffix = f"_s{season:02d}_e{first_ep:02d}_{disc_id[-6:]}"
    tag = re.sub(r"[^a-z0-9]+", "_", show.lower()).strip("_")
    tag = tag[:MAX_BACKUP_NAME - len(suffix)].strip("_")
    backup = run_dvdbackup(dev, tag + suffix,
                           needed_vts=needed, disc_id=disc_id,
                           label=f"{show} S{season:02d} from E{first_ep:02d}")
    if not backup:
        print(c("Backup failed.", RED))
        return

    vts_path = video_ts(backup)
    map_titles_to_vts(titles, vts_path)
    do_encodes(store, disc_id, names, vts_path, titles, drive=dev)

    write_episode_manifest(season_dir, show, season, disc_id, disc_label,
                   names, {t.ix: t for t in titles})

    last_ep = max(ep for _, _, ep in names)
    store.set_next_episode(show, season, last_ep + 1)
    print(c(f"\nNext disc for this season will start at E{last_ep + 1:02d}",
            GREEN))
    offer_cleanup(backup)

    if confirm("\nEject and load the next disc now?", False):
        swap_disc(store)
        return "next"


def movie_workflow(store, dev, label, disc_id, titles):
    lib = choose_library(store, "movies")
    default_name = sanitize(label.replace("_", " ").title())
    name = ask("Movie title", default_name)
    year = ask("Year (blank to skip)", "")
    if year and not (year.isdigit() and len(year) == 4):
        print(c(f"  '{year}' is not a 4-digit year - leaving it off.", YELLOW))
        year = ""
    fname = f"{sanitize(name)} ({year}).mp4" if year else f"{sanitize(name)}.mp4"

    selected = [t for t in titles if t.selected]
    if not selected:
        print(c("Nothing selected.", YELLOW))
        return

    names = []
    for i, t in enumerate(selected):
        out = lib / (fname if i == 0 else
                     f"{sanitize(name)} - extra {i}.mp4")
        names.append((t, out, None))

    if not confirm(f"\nRip to {lib / fname}?"):
        return

    needed = sorted({t.vts for t in selected if t.vts})
    suffix = f"_{disc_id[-6:]}"
    tag = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    tag = tag[:MAX_BACKUP_NAME - len(suffix)].strip("_")
    backup = run_dvdbackup(dev, tag + suffix,
                           needed_vts=needed, disc_id=disc_id, label=name)
    if not backup:
        return
    vts_path = video_ts(backup)
    map_titles_to_vts(titles, vts_path)
    do_encodes(store, disc_id, names, vts_path, titles, drive=dev)
    offer_cleanup(backup)


def do_encodes(store, disc_id, names, vts_path, all_titles, drive=""):
    """names is [(Title, output_path, episode_or_None)]"""
    # Detect VTS sets shared by more than one selected title.
    vts_counts = {}
    for t, _, _ in names:
        vts_counts[t.vts] = vts_counts.get(t.vts, 0) + 1

    results = []
    total_all = sum(t.seconds for t, _, _ in names)
    done_before = 0.0
    drive_name = drive or ""
    batch_start = time.time()

    for idx, (t, out_path, ep) in enumerate(names, 1):
        src, size = vts_inputs(vts_path, t.vts)
        if not src:
            print(c(f"No VOB files found for VTS {t.vts:02d} "
                    f"(title {t.ix}) - skipping", RED))
            results.append((out_path, False, 0))
            continue

        start = None
        duration = t.seconds
        if vts_counts[t.vts] > 1:
            # Several titles live in one VTS: seek by cumulative offset.
            siblings = sorted([x for x in all_titles if x.vts == t.vts],
                              key=lambda x: x.ttn or x.ix)
            offset = 0.0
            for s in siblings:
                if s.ix == t.ix:
                    break
                offset += s.seconds
            start = offset
            print(c(f"  Title {t.ix} shares VTS {t.vts:02d}; seeking to "
                    f"{hhmmss(offset)}", YELLOW))

        board = Board(10)
        with Keys() as keys:
            try:
                verdict = encode(src, out_path, start, duration,
                                 label=f"title {t.ix} ({t.length})",
                                 board=board, index=idx, count=len(names),
                                 done_before=done_before, total_all=total_all,
                                 drive=drive_name, ep_label=out_path.stem,
                                 keys=keys)
            finally:
                board.close()
        done_before += t.seconds

        if verdict in ("skipped", "aborted"):
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            if verdict == "skipped":
                print(c(f"  Skipped {out_path.name}", YELLOW))
                results.append((out_path, False, 0))
                store.log_rip(disc_id, t.ix, out_path, t.seconds, 0, "skipped")
                continue
            print(c("\n  Stopped by you. Remaining episodes not encoded.",
                    YELLOW))
            print(c(f"  The disc copy is still in {vts_path.parent} - "
                    f"rerun and reuse it to finish.", DIM))
            break

        ok = verdict == "ok"
        out_sec = probe_duration(out_path) if ok else 0
        drift = abs(out_sec - t.seconds)

        if ok and drift > 20:
            print(c(f"  Length mismatch: expected {hhmmss(t.seconds)}, "
                    f"got {hhmmss(out_sec)}", YELLOW))
            status = "length-mismatch"
        elif ok:
            print(c(f"  OK  {hhmmss(out_sec)}  "
                    f"{out_path.stat().st_size / 1024**2:,.0f} MB", GREEN))
            status = "ok"
        else:
            print(c("  FAILED", RED))
            status = "failed"

        store.log_rip(disc_id, t.ix, out_path, t.seconds, out_sec, status)
        results.append((out_path, ok, out_sec))

    hdr("Results")
    print(f"  {DIM}total encode time {mmss(time.time() - batch_start)}{RESET}\n")
    for out_path, ok, out_sec in results:
        mark = c("OK  ", GREEN) if ok else c("FAIL", RED)
        print(f"  {mark} {out_path.name}  {hhmmss(out_sec)}")

    if confirm("\nSpot-check the first 10 seconds of each file?", False):
        for out_path, ok, _ in results:
            if ok:
                print(f"Playing {out_path.name} ... (close the window to continue)")
                subprocess.run(["ffplay", "-autoexit", "-t", "10",
                                "-loglevel", "quiet", str(out_path)])


def offer_cleanup(backup_dir):
    try:
        size = sum(f.stat().st_size for f in backup_dir.rglob("*") if f.is_file())
        gb = size / 1024 ** 3
    except Exception:
        gb = 0
    if confirm(f"\nDelete the working copy {backup_dir} ({gb:.1f} GB)?", False):
        shutil.rmtree(backup_dir, ignore_errors=True)
        print(c("Removed.", GREEN))
    else:
        print(c(f"Kept at {backup_dir} - useful if you want subtitles "
                f"in MKV later.", DIM))


# ---------------------------------------------------------------- main

def rip_flow(store):
    dev = choose_drive(store)
    if not dev:
        return

    if not media_present(dev):
        print(c(f"\n  No disc detected in {dev}.", YELLOW))
        if confirm("  Close the tray and wait for one?", True):
            close_tray()
        if not wait_for_media(dev):
            print(c("  Still no disc. Load one and try again.", YELLOW))
            return

    unmount(dev)
    engine = (store.get("rip_engine") or "mkv").lower()
    label, disc_id, titles = "DISC", "", []
    if engine == "mkv" and have_makemkv():
        print(c("Reading disc through MakeMKV...", DIM))
        label, disc_id, titles = scan_disc_makemkv(dev)
    if not titles:
        try:
            label, disc_id, titles = scan_disc(dev)
        except subprocess.TimeoutExpired:
            print(c("lsdvd timed out. Is a disc loaded and spun up?", RED))
            return

    if not titles:
        print(c("No titles found on this disc.", RED))
        print(c("  Common causes:", DIM))
        print(c("    - the tray is open or the disc is not seated", DIM))
        print(c("    - the disc is dirty or badly scratched", DIM))
        print(c("    - libdvdcss is not linked (see the README)", DIM))
        print(c(f"    - media present on {dev}: "
                f"{'yes' if media_present(dev) else 'NO'}", DIM))
        return

    print(f"\nDisc label: {c(label, BOLD)}")
    if not disc_id:
        disc_id = "fp" + fingerprint(titles)
        print(f"Disc ID:    {DIM}{disc_id} (from title layout){RESET}")
    if disc_id:
        if not disc_id.startswith("fp"):
            print(f"Disc ID:    {DIM}{disc_id}{RESET}")
        store.see_disc(disc_id, label)
        prior = store.prior_rips(disc_id)
        if prior:
            print(c(f"\nYou have ripped this disc before "
                    f"({len(prior)} files):", YELLOW))
            for row in prior[:12]:
                print(f"  {DIM}{Path(row['output_path']).name}{RESET}")
            if not confirm("Rip it again?", False):
                return

    mode = ask("\nIs this a [t]v disc or a [m]ovie?", "t").lower()[:1]
    mode = "tv" if mode == "t" else "movie"

    classify(titles, mode)

    header = (f"{label}  -  {len(titles)} titles  -  "
              f"space to toggle, enter to confirm")
    try:
        ok = curses.wrapper(pick_titles, titles, header)
    except Exception:
        ok = fallback_picker(titles, header)

    if not ok:
        print(c("Cancelled.", YELLOW))
        return

    selected = [t for t in titles if t.selected]
    if not selected:
        print(c("Nothing selected.", YELLOW))
        return

    print(f"\n{c('Selected:', BOLD)}")
    for t in selected:
        print(f"  Title {t.ix:02d}  {t.length}  {t.audio_count} audio")

    if mode == "tv":
        tv_workflow(store, dev, label, disc_id, titles)
    else:
        movie_workflow(store, dev, label, disc_id, titles)


def settings_menu(store):
    while True:
        hdr("Settings")
        print(f"  1. Minimum title length      {c(hhmmss(FLOOR_SEC), BOLD)}")
        print(f"     {DIM}Shorter titles are treated as logos/promos.{RESET}")
        print(f"  2. Maximum title length      {c(hhmmss(CEILING_SEC), BOLD)}")
        print(f"     {DIM}Longer titles are treated as bonus features.{RESET}")
        print(f"  3. Runtime grouping slack    {c(f'{CLUSTER_TOLERANCE:.0%}', BOLD)}")
        print(f"     {DIM}How much episode runtimes may differ and still group.{RESET}")
        print(f"  4. Copy stall timeout        {c(f'{STALL_SECONDS}s', BOLD)}")
        print(f"  5. Auto-fetch episode titles "
              f"{c('on' if store.get('auto_titles', '1') == '1' else 'off', BOLD)}")
        print(f"     {DIM}Looks up real titles on TVmaze (free, no account).{RESET}")
        print(f"  6. Auto-identify discs         "
              f"{c('on' if store.get('auto_plan', '1') == '1' else 'off', BOLD)}")
        print(f"     {DIM}Match disc label to a show, find the season gap, "
              f"fill it.{RESET}")
        print(f"  7. Forget a cached show match")
        print(f"  8. Reset everything to defaults")
        print("  b. Back")

        pick = ask("\nChoice", "b").lower()

        if pick == "1":
            v = ask("Minimum length in minutes", str(FLOOR_SEC // 60))
            if v.replace(".", "").isdigit():
                store.set("floor_sec", int(float(v) * 60))
        elif pick == "2":
            v = ask("Maximum length in minutes", str(CEILING_SEC // 60))
            if v.replace(".", "").isdigit():
                store.set("ceiling_sec", int(float(v) * 60))
        elif pick == "3":
            v = ask("Slack as a percent (10 means 10%)",
                    str(int(CLUSTER_TOLERANCE * 100)))
            if v.isdigit() and 1 <= int(v) <= 50:
                store.set("cluster_tolerance", int(v) / 100)
        elif pick == "4":
            v = ask("Seconds of no progress before the watchdog acts",
                    str(STALL_SECONDS))
            if v.isdigit() and int(v) >= 30:
                store.set("stall_seconds", int(v))
        elif pick == "5":
            now = store.get("auto_titles", "1")
            store.set("auto_titles", "0" if now == "1" else "1")
        elif pick == "6":
            now = store.get("auto_plan", "1")
            store.set("auto_plan", "0" if now == "1" else "1")
        elif pick == "7":
            keys = [r["key"] for r in store.db.execute(
                "SELECT key FROM settings WHERE key LIKE 'tvmaze:%' "
                "AND value != ''")]
            if not keys:
                print("  Nothing cached.")
            else:
                for i, k in enumerate(keys, 1):
                    print(f"  {i}. {k.split(':', 1)[1]}")
                p = ask("  Forget which", "1")
                if p.isdigit() and 1 <= int(p) <= len(keys):
                    store.set(keys[int(p) - 1], "")
                    print(c("  Cleared. It will ask again next time.", GREEN))
        elif pick == "8":
            if confirm("Reset length and timing settings to defaults?", False):
                for k in ("floor_sec", "ceiling_sec", "cluster_tolerance",
                          "stall_seconds"):
                    store.db.execute("DELETE FROM settings WHERE key=?", (k,))
                store.db.commit()
                print(c("  Reset.", GREEN))
        elif pick == "b":
            return

        load_settings(store)


def show_history(store):
    hdr("Recent rips")
    rows = store.db.execute(
        "SELECT * FROM rips ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        print("  Nothing yet.")
        return
    for r in rows:
        mark = c("OK  ", GREEN) if r["status"] == "ok" else c(r["status"], YELLOW)
        print(f"  {mark} {Path(r['output_path']).name}")

    hdr("Series progress")
    for r in store.db.execute("SELECT * FROM series ORDER BY show, season"):
        print(f"  {r['show']}  Season {r['season']:02d}  "
              f"next episode: E{r['next_ep']:02d}")


def main():
    check_deps()
    store = Store()
    load_settings(store)

    while True:
        hdr("DVD Ripper")
        drive = store.get("drive", "not set")
        print(f"  Drive:   {drive}")
        print(f"  Movies:  {store.get('lib_movies', 'not set')}")
        print(f"  TV:      {store.get('lib_tv', 'not set')}")
        print()
        print("  1. Rip the disc in the drive")
        print("  2. Refresh / find the drive")
        print("  3. Change library folders")
        print("  4. History and series progress")
        print("  5. Eject disc")
        print("  6. Close tray")
        print("  7. Swap disc and rip the next one")
        print("  8. Drive diagnostics")
        print("  9. Settings")
        print("  q. Quit")

        choice = ask("\nChoice", "1").lower()

        if choice == "1":
            try:
                while rip_flow(store) == "next":
                    pass
            except KeyboardInterrupt:
                print(c("\nInterrupted.", YELLOW))
            except Exception as e:
                print(c(f"\nSomething went wrong: {e}", RED))
                print(c("Back to the menu - nothing was lost. Any disc copy "
                        "in ~/rips can be reused.", DIM))
                if store.get("debug", "0") == "1":
                    import traceback
                    traceback.print_exc()
        elif choice == "2":
            refresh_drive(store)
        elif choice == "3":
            store.set("lib_movies", "")
            store.set("lib_tv", "")
            choose_library(store, "movies")
            choose_library(store, "tv")
        elif choice == "4":
            show_history(store)
        elif choice == "5":
            eject_disc(store)
        elif choice == "6":
            close_tray()
        elif choice == "7":
            swap_disc(store)
            try:
                rip_flow(store)
            except KeyboardInterrupt:
                print(c("\nInterrupted.", YELLOW))
            except Exception as e:
                print(c(f"\nSomething went wrong: {e}", RED))
        elif choice == "8":
            drive_diagnostics(store)
        elif choice == "9":
            settings_menu(store)
        elif choice == "q":
            print("Bye.")
            return

        ask("\nPress enter to return to the menu")



# ---------------------------------------------------------------- makemkv
# Additive: this does not touch the lsdvd -> dvdbackup -> ffmpeg path.
# It is a second scanner for discs lsdvd cannot read (Blu-ray), and it can
# also be used on DVDs when you want MakeMKV's own duplicate detection.

MAKEMKVCON = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"

MSG_DUPLICATE = "3027"   # Title #B in VTS #V is equal to title #A

# TINFO item codes.
TI_CHAPTERS = "8"
TI_DURATION = "9"
TI_BYTES = "11"
TI_SOURCE_ID = "24"      # the "Title #N" number MakeMKV prints in messages
TI_SEGCOUNT = "25"       # >1 means several PGCs joined = play-all
TI_SEGMAP = "26"
TI_COMMENT = "49"        # the C1 / B1 / D3 tag shown in the GUI


def have_makemkv():
    return os.path.exists(MAKEMKVCON)


def _robot_fields(line):
    """Split a robot line into fields, respecting quoted commas."""
    out, cur, q = [], "", False
    for ch in line:
        if ch == '"':
            q = not q
        elif ch == "," and not q:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _hms(text):
    try:
        parts = [int(p) for p in (text or "").split(":") if p != ""]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def scan_makemkv(disc_index=0, minlength=700, timeout=900):
    """Read a disc through makemkvcon. Works for DVD and Blu-ray alike.

    TINFO indices are 0-based and are what `makemkvcon mkv` expects, so they
    become Title.ix. MakeMKV's own "Title #N" appears as Title.source_id for
    display. Titles MakeMKV judged duplicates never reach TINFO at all, so the
    list is already deduplicated by structure rather than by runtime.
    """
    if not have_makemkv():
        return [], [f"{MAKEMKVCON} not found"]

    cmd = [MAKEMKVCON, "-r", "--cache=256", f"--minlength={minlength}",
           "info", f"disc:{disc_index}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return [], ["makemkvcon timed out - disc may be scratched"]
    except OSError as e:
        return [], [f"makemkvcon failed to start: {e}"]

    tinfo, audio, dropped = {}, {}, []
    for line in proc.stdout.splitlines():
        if line.startswith("TINFO:"):
            f = _robot_fields(line[6:])
            if len(f) >= 4 and f[0].isdigit():
                tinfo.setdefault(int(f[0]), {})[f[1]] = f[3]
        elif line.startswith("SINFO:"):
            f = _robot_fields(line[6:])
            if len(f) >= 5 and f[0].isdigit() and f[2] == "1" \
                    and f[4].strip().lower() == "audio":
                ix = int(f[0])
                audio[ix] = audio.get(ix, 0) + 1
        elif line.startswith("MSG:"):
            f = _robot_fields(line[4:])
            if f and f[0] == MSG_DUPLICATE and len(f) >= 8:
                dropped.append((re.sub(r"\D", "", f[7]),
                                re.sub(r"\D", "", f[6])))

    notes = []
    if not tinfo:
        tail = [l for l in proc.stdout.splitlines()
                if l.startswith("MSG:")][-3:]
        notes.append("makemkvcon returned no titles.")
        notes.extend(t[:120] for t in tail)
        return [], notes

    titles = []
    for ix in sorted(tinfo):
        ti = tinfo[ix]
        t = Title(ix=ix,
                  seconds=_hms(ti.get(TI_DURATION, "")),
                  vts=0, ttn=0,
                  audio_count=audio.get(ix, 0),
                  chapters=int(ti.get(TI_CHAPTERS, 0) or 0))
        t.source_id = int(ti.get(TI_SOURCE_ID, 0) or 0)
        t.bytes = int(ti.get(TI_BYTES, 0) or 0)
        t.segments = int(ti.get(TI_SEGCOUNT, 1) or 1)
        t.tag = ti.get(TI_COMMENT, "")
        titles.append(t)

    if dropped:
        pairs = ", ".join(f"#{d} = #{k}" for d, k in dropped)
        notes.append(f"MakeMKV already excluded {len(dropped)} duplicate "
                     f"title(s): {pairs}")
    return titles, notes


def mark_makemkv_playalls(titles):
    """Flag joined titles using MakeMKV's segment count.

    A play-all is several PGCs stitched together, so segment count is the
    honest signal - far better than guessing from chapter counts.
    """
    singles = [t.seconds for t in titles
               if getattr(t, "segments", 1) == 1 and t.seconds > 600]
    if not singles:
        return
    ep = sorted(singles)[len(singles) // 2]
    for t in titles:
        segs = getattr(t, "segments", 1)
        if segs > 1 and ep and t.seconds >= ep * 1.8:
            t.selected = False
            t.reason = f"PLAY-ALL ({segs} segments joined) - skip this"


def rip_makemkv(disc_index, title_ix, out_dir, on_progress=None):
    """Rip one title. title_ix is the TINFO index. Returns the new path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    before = set(out_dir.glob("*.mkv"))

    cmd = [MAKEMKVCON, "-r", "--progress=-same",
           "mkv", f"disc:{disc_index}", str(title_ix), str(out_dir)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in proc.stdout:
        if line.startswith("PRGV:") and on_progress:
            f = _robot_fields(line[5:])
            if len(f) >= 3:
                try:
                    cur, total = int(f[1]), int(f[2])
                    if total:
                        on_progress(cur / total)
                except ValueError:
                    pass
    proc.wait()
    if proc.returncode != 0:
        return None
    new = sorted(set(out_dir.glob("*.mkv")) - before,
                 key=lambda p: p.stat().st_mtime)
    return new[-1] if new else None

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")

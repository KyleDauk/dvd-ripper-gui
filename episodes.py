#!/usr/bin/env python3
"""Episode reconciliation and library browsing for the GUI.

The interesting part is refine_with_playall(). A disc's play-all title is a
better source of truth than any online runtime database: it is the studio's
own list of what counts as an episode, and it is accurate to the second.
Online data only publishes whole minutes.
"""

import re
from pathlib import Path

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi")


# ------------------------------------------------------------- play-all

def _find_playall(titles):
    """The joined title, or None. Longest multi-segment title well above
    the typical single length."""
    singles = sorted(t.seconds for t in titles
                     if getattr(t, "segments", 1) == 1 and t.seconds > 600)
    if not singles:
        return None
    typical = singles[len(singles) // 2]
    best = None
    for t in titles:
        if getattr(t, "segments", 1) < 2:
            continue
        if t.seconds < typical * 1.8:
            continue
        if best is None or t.seconds > best.seconds:
            best = t
    return best


def _closest_subset(items, target, tol):
    """items is [(key, seconds)]. Return the key tuple whose seconds sum
    lands nearest target, or None if nothing comes within tol.

    One subset is kept per reachable sum, which is plenty here and keeps the
    cost linear in (titles x seconds) rather than exponential.
    """
    reach = {0: ()}
    for key, sec in items:
        nxt = dict(reach)
        for total, subset in reach.items():
            new = total + sec
            if new > target + tol:
                continue
            if new not in nxt or len(subset) + 1 > len(nxt[new]):
                nxt[new] = subset + (key,)
        reach = nxt

    reach.pop(0, None)
    if not reach:
        return None
    best = min(reach, key=lambda s: (abs(s - target), -len(reach[s])))
    if abs(best - target) > tol:
        return None
    return reach[best]


def refine_with_playall(titles):
    """Use the play-all to decide which titles are really episodes.

    Returns a note describing what happened, or "" if no play-all was found
    and the existing classify() result was left alone.
    """
    playall = _find_playall(titles)
    if not playall:
        return ""

    target = round(playall.seconds)
    candidates = [(i, round(t.seconds)) for i, t in enumerate(titles)
                  if t is not playall and t.seconds > 240]
    if not candidates:
        return ""

    # 1% covers HH:MM:SS truncation across a dozen titles without being loose
    # enough to let a wrong combination win on a normal disc.
    tol = max(10, round(target * 0.01))
    chosen = _closest_subset(candidates, target, tol)
    if not chosen:
        return (f"Play-all is {playall.length} but no combination of titles "
                f"adds up to it, so the length-based guess was kept.")

    chosen = set(chosen)
    total = sum(sec for i, sec in candidates if i in chosen)

    singles = sorted(t.seconds for t in titles
                     if getattr(t, "segments", 1) == 1 and t.seconds > 600)
    ep_len = singles[len(singles) // 2] if singles else 0

    for i, t in enumerate(titles):
        if t is playall:
            t.selected = False
            t.reason = (f"PLAY-ALL ({getattr(t, 'segments', 1)} segments "
                        f"joined) - skip this")
            continue
        if i in chosen:
            t.selected = True
            parts = max(1, round(t.seconds / ep_len)) if ep_len else 1
            t.reason = ("episode (confirmed by play-all)" if parts == 1 else
                        f"{parts} episodes in one title (confirmed by play-all)")
        else:
            t.selected = False
            t.reason = "not in the play-all - bonus or alternate cut"

    drift = total - target
    return (f"Play-all is {playall.length}; {len(chosen)} titles add up to it "
            f"within {abs(drift)}s. Those are the episodes.")


def annotate_parts(titles):
    """Set .parts on every title: how many broadcast episodes it contains."""
    singles = sorted(t.seconds for t in titles
                     if getattr(t, "segments", 1) == 1 and t.seconds > 600)
    ep_len = singles[len(singles) // 2] if singles else 0
    for t in titles:
        t.parts = (max(1, round(t.seconds / ep_len))
                   if ep_len and t.seconds > 600 else 1)


def episode_slots(titles):
    """How many episode numbers each selected title consumes, in disc order.

    A 43-minute title on a 22-minute show is a two-parter. Libraries that
    merge aired-together episodes want it as one slot, so this reports the
    part count and lets the caller decide.
    """
    singles = sorted(t.seconds for t in titles
                     if t.selected and getattr(t, "segments", 1) == 1
                     and t.seconds > 600)
    ep_len = singles[len(singles) // 2] if singles else 0
    out = []
    for t in titles:
        if not t.selected:
            continue
        parts = max(1, round(t.seconds / ep_len)) if ep_len else 1
        out.append({"ix": t.ix, "parts": parts})
    return out


# -------------------------------------------------------------- library

def list_shows(tv_root):
    try:
        return sorted(p.name for p in Path(tv_root).iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except (OSError, TypeError):
        return []


def list_seasons(show_dir):
    """[{'name', 'number', 'episodes': [n], 'count'}] for one show folder."""
    out = []
    try:
        entries = sorted(Path(show_dir).iterdir())
    except (OSError, TypeError):
        return out

    for sd in entries:
        if not sd.is_dir() or sd.name.startswith("."):
            continue
        m = re.search(r"(\d+)", sd.name)
        if not m:
            continue
        eps, named = set(), {}
        try:
            for f in sd.iterdir():
                if f.suffix.lower() not in VIDEO_EXT:
                    continue
                # "S03E12-E13" covers two numbers, not one. Missing the range
                # is what makes a two-parter look like a gap.
                mm = re.search(r"S(\d+)E(\d+)(?:\s*-\s*E?(\d+))?", f.name, re.I)
                if not mm:
                    continue
                first = int(mm.group(2))
                last = int(mm.group(3)) if mm.group(3) else first
                for n in range(first, max(first, last) + 1):
                    eps.add(n)
                tail = re.split(r"S\d+E\d+(?:\s*-\s*E?\d+)?\s*-\s*",
                                f.stem, maxsplit=1, flags=re.I)
                named[first] = tail[1].strip() if len(tail) > 1 else ""
        except OSError:
            pass
        out.append({"name": sd.name, "number": int(m.group(1)),
                    "episodes": sorted(eps), "count": len(eps),
                    "titles": named})
    return out


def next_episode(episodes):
    """First missing number, counting from 1."""
    have = set(episodes)
    n = 1
    while n in have:
        n += 1
    return n


def naming_prefix(show_folder):
    """Match how files in the folder are already named.

    A season with 'The Office (US) - S06E01 - Gossip.mkv' should keep getting
    that prefix rather than the full folder name, so Plex keeps one series.
    """
    try:
        for sd in sorted(Path(show_folder).iterdir(), reverse=True):
            if not sd.is_dir():
                continue
            for f in sorted(sd.iterdir()):
                if f.suffix.lower() not in VIDEO_EXT:
                    continue
                m = re.match(r"(.+?)\s*-\s*S\d+E\d+", f.name)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return Path(show_folder).name

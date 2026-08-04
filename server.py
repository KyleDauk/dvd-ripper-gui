#!/usr/bin/env python3
"""Local web front-end for dvdrip.py.

Serves a single page on 127.0.0.1 and calls the existing dvdrip functions.
dvdrip.py is imported unchanged - this file adds a second face to the same
engine, it does not replace the terminal one.

Bound to loopback on purpose. This controls the optical drive and writes to
the filesystem, so it must never be reachable from outside this Mac.

Usage:  python3 server.py        then open http://127.0.0.1:8765
"""

import json
import re
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dvdrip
import episodes

HOST = "127.0.0.1"
PORT = 8765
HERE = Path(__file__).parent

# One rip at a time - the drive is a single physical resource.
JOB = {
    "running": False,
    "label": "",
    "done": 0,
    "total": 0,
    "fraction": 0.0,
    "log": [],
    "finished": False,
    "cancel": False,
}
JOB_LOCK = threading.Lock()

# Titles from the last scan, kept so a rip request only needs indices.
SCAN = {"drive": "", "label": "", "disc_id": "", "titles": []}


def job_log(message):
    with JOB_LOCK:
        JOB["log"].append(message)
        del JOB["log"][:-200]


def title_json(t):
    """Everything the page needs to draw one row."""
    return {
        "ix": t.ix,
        "seconds": round(t.seconds),
        "length": t.length,
        "audio_count": t.audio_count,
        "chapters": t.chapters,
        "selected": bool(t.selected),
        "reason": t.reason or "",
        "duplicate_of": t.duplicate_of,
        "source_id": getattr(t, "source_id", None),
        "segments": getattr(t, "segments", 1),
        "parts": getattr(t, "parts", 1),
        "bytes": getattr(t, "bytes", 0),
        "tag": getattr(t, "tag", ""),
    }


def library_paths():
    """Read the folders the terminal app already knows about."""
    try:
        store = dvdrip.Store()
        return {
            "movies": store.get("lib_movies", "") or "",
            "tv": store.get("lib_tv", "") or "",
        }
    except Exception:
        return {"movies": "", "tv": ""}


# ---------------------------------------------------------------- actions

def api_state():
    drives = [d for d, _ in dvdrip.find_optical_drives()]
    try:
        store = dvdrip.Store()
        saved = store.get("drive", "") or ""
    except Exception:
        saved = ""
    drive = saved if saved in drives else (drives[0] if drives else "")
    return {
        "drives": drives,
        "drive": drive,
        "media": bool(drive) and dvdrip.media_present(drive),
        "makemkv": dvdrip.have_makemkv(),
        "libraries": library_paths(),
        "scan": {"label": SCAN["label"], "disc_id": SCAN["disc_id"],
                 "count": len(SCAN["titles"])},
    }


def api_scan(body):
    drive = body.get("drive") or ""
    mode = body.get("mode") or "tv"
    if not drive:
        return {"error": "No optical drive selected."}
    if not dvdrip.media_present(drive):
        return {"error": f"No disc detected in {drive}."}

    # MakeMKV needs the disc unmounted, same as the terminal flow does.
    dvdrip.unmount(drive)

    label, disc_id, titles = "DISC", "", []
    used = ""
    if dvdrip.have_makemkv():
        label, disc_id, titles = dvdrip.scan_disc_makemkv(drive)
        used = "makemkv"
    if not titles:
        try:
            label, disc_id, titles = dvdrip.scan_disc(drive)
            used = "lsdvd"
        except Exception as e:
            return {"error": f"Scan failed: {e}"}

    if not titles:
        return {"error": "No titles found. Disc may be dirty, unseated, "
                         "or unreadable."}

    dvdrip.classify(titles, "tv" if mode == "tv" else "movie")

    # The disc's own play-all beats runtime guesswork when one exists.
    playall_note = episodes.refine_with_playall(titles)
    episodes.annotate_parts(titles)

    if not disc_id:
        disc_id = "fp" + dvdrip.fingerprint(titles)

    SCAN.update(drive=drive, label=label, disc_id=disc_id, titles=titles)

    prior = []
    try:
        store = dvdrip.Store()
        store.see_disc(disc_id, label)
        prior = [Path(r["output_path"]).name for r in store.prior_rips(disc_id)]
    except Exception:
        pass

    return {
        "label": label,
        "disc_id": disc_id,
        "engine": used,
        "mode": mode,
        "prior": prior[:12],
        "playall_note": playall_note,
        "slots": episodes.episode_slots(titles),
        "titles": [title_json(t) for t in titles],
    }


# ------------------------------------------------------------- settings

def api_settings(body):
    """Read or write the library folders.

    Without this the app is tied to whatever machine first configured
    ~/.dvdrip/state.db, since the terminal menu was the only way to set them.
    """
    try:
        store = dvdrip.Store()
    except Exception as e:
        return {"error": f"Cannot open the settings database: {e}"}

    saved = []
    for key, field in (("lib_tv", "tv"), ("lib_movies", "movies")):
        if field not in body:
            continue
        path = (body.get(field) or "").strip().rstrip("/")
        if path and not Path(path).is_dir():
            return {"error": f"That folder does not exist: {path}"}
        store.set(key, path)
        saved.append(field)

    return {
        "tv": store.get("lib_tv", "") or "",
        "movies": store.get("lib_movies", "") or "",
        "saved": saved,
    }


def api_volumes(body):
    """Mounted volumes, to help point the folder fields somewhere real."""
    out = ["/Users/" + Path.home().name]
    try:
        out += [str(p) for p in sorted(Path("/Volumes").iterdir())
                if p.is_dir()]
    except OSError:
        pass
    return {"volumes": out}


# -------------------------------------------------------------- library

def api_library(body):
    """Folders available to rip into, for the destination dropdowns."""
    libs = library_paths()
    return {
        "tv_root": libs["tv"],
        "movies_root": libs["movies"],
        "shows": episodes.list_shows(libs["tv"]),
    }


def api_library_show(body):
    """Seasons inside one show, with what is already there."""
    libs = library_paths()
    show = (body.get("show") or "").strip()
    if not libs["tv"] or not show:
        return {"seasons": [], "prefix": show}
    show_dir = Path(libs["tv"]) / show
    if not show_dir.is_dir():
        return {"seasons": [], "prefix": show}

    seasons = episodes.list_seasons(show_dir)
    for s in seasons:
        s["next"] = episodes.next_episode(s["episodes"])

    # Resolve the show and pull the season's episode names here, so names are
    # filled in without the user having to search for a series they already
    # picked from their own library.
    show_id, candidates = resolve_show(show)
    slots, err = [], ""
    season = body.get("season")
    if show_id and season not in (None, ""):
        try:
            slots = season_slots(show_id, int(season))
        except Exception as e:
            err = f"TVmaze lookup failed: {e}"

    return {
        "seasons": seasons,
        "prefix": episodes.naming_prefix(show_dir),
        "path": str(show_dir),
        "show_id": show_id,
        "candidates": candidates,
        "slots": slots,
        "lookup_error": err,
    }


def api_library_create(body):
    """Make a new show and/or season folder so the dropdowns can offer it."""
    libs = library_paths()
    kind = body.get("kind") or "tv"
    root = libs["tv"] if kind == "tv" else libs["movies"]
    if not root:
        return {"error": f"No {kind} library folder is configured."}

    show = dvdrip.sanitize((body.get("show") or "").strip())
    if not show:
        return {"error": "Give the show a name."}

    target = Path(root) / show
    season = body.get("season")
    if season not in (None, ""):
        try:
            target = target / f"Season {int(season):02d}"
        except (TypeError, ValueError):
            return {"error": "Season must be a number."}

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"error": f"Could not create {target}: {e}"}
    return {"ok": True, "path": str(target)}


def api_tvmaze_search(body):
    query = (body.get("query") or "").strip()
    if not query:
        return {"results": []}
    rows = dvdrip.tvmaze_search(query)
    return {"results": [{"id": i, "name": n, "year": y, "network": net}
                        for i, n, y, net in rows]}


def season_slots(show_id, season):
    """Season slots with their real broadcast numbers.

    dvdrip.tvmaze_episodes(merge=True) renumbers slots from 1, which silently
    shifts every episode after a two-parter. Here a merged run keeps its true
    first and last numbers instead, so "The Delivery" stays E17-E18.
    """
    eps = dvdrip.http_json(f"{dvdrip.TVMAZE}/shows/{show_id}/episodes")
    season_eps = [e for e in eps
                  if e.get("season") == season and e.get("number")]
    return [{"first": run[0]["number"],
             "last": run[-1]["number"],
             "title": dvdrip.run_title(run)}
            for run in dvdrip.group_aired_together(season_eps)]


def resolve_show(folder_name):
    """Non-interactive twin of dvdrip.resolve_show_id.

    Returns (show_id, candidates). A show_id means it was settled from the
    cache or an unambiguous match; candidates means the caller has to choose.
    Keeps using the same tvmaze:<folder> cache key as the terminal program,
    so the two share their answers.
    """
    key = f"tvmaze:{folder_name}"
    try:
        store = dvdrip.Store()
    except Exception:
        return None, []

    cached = store.get(key)
    if cached:
        try:
            return int(cached), []
        except ValueError:
            pass

    years = re.findall(r"\((\d{4})\)", folder_name)
    query = re.sub(r"\(\d{4}\)", "", folder_name)
    query = query.replace("(", "").replace(")", "").strip()

    matches = dvdrip.tvmaze_search(query)
    if not matches:
        return None, []

    best = None
    if len(matches) == 1:
        best = matches[0]
    else:
        exact = [m for m in matches if years and m[2] == years[-1]]
        if len(exact) == 1:
            best = exact[0]

    if best:
        store.set(key, best[0])
        return best[0], []

    return None, [{"id": i, "name": n, "year": y, "network": net}
                  for i, n, y, net in matches]


def api_tvmaze_episodes(body):
    try:
        show_id = int(body.get("show_id"))
        season = int(body.get("season"))
    except (TypeError, ValueError):
        return {"error": "Need a show and a season number."}
    try:
        return {"slots": season_slots(show_id, season)}
    except Exception as e:
        return {"error": f"TVmaze lookup failed: {e}"}


def api_tvmaze_remember(body):
    """Pin a chosen show to a library folder so it is never asked again."""
    folder = (body.get("folder") or "").strip()
    try:
        show_id = int(body.get("show_id"))
    except (TypeError, ValueError):
        return {"error": "Need a show id."}
    if not folder:
        return {"error": "Need a folder name."}
    try:
        dvdrip.Store().set(f"tvmaze:{folder}", show_id)
    except Exception as e:
        return {"error": f"Could not save: {e}"}
    return {"ok": True}


def _run_rip(drive, jobs, dest):
    """Rip each selected title through MakeMKV, then rename to the plan."""
    dest = Path(dest).expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        job_log(f"Cannot create {dest}: {e}")
        with JOB_LOCK:
            JOB["running"], JOB["finished"] = False, True
        return

    store = None
    try:
        store = dvdrip.Store()
    except Exception:
        pass

    for n, item in enumerate(jobs):
        with JOB_LOCK:
            if JOB["cancel"]:
                job_log("Cancelled before starting the next title.")
                break
            JOB["done"] = n
            JOB["label"] = item["name"]
            JOB["fraction"] = 0.0

        job_log(f"Title {item['ix']} -> {item['name']}")

        def on_progress(frac):
            with JOB_LOCK:
                JOB["fraction"] = frac

        try:
            produced = dvdrip.rip_makemkv(0, item["ix"], dest,
                                          on_progress=on_progress)
        except Exception as e:
            job_log(f"  failed: {e}")
            produced = None

        if not produced:
            job_log(f"  title {item['ix']} produced nothing")
            if store:
                try:
                    store.log_rip(SCAN["disc_id"], item["ix"], item["name"],
                                  0, 0, "failed")
                except Exception:
                    pass
            continue

        target = dest / item["name"]
        try:
            if target.exists():
                target.unlink()
            produced.rename(target)
            job_log(f"  saved {target.name}")
        except OSError as e:
            job_log(f"  kept as {produced.name} (rename failed: {e})")
            target = produced

        if store:
            try:
                store.log_rip(SCAN["disc_id"], item["ix"], str(target),
                              0, 0, "ok")
            except Exception:
                pass

    with JOB_LOCK:
        JOB["done"] = JOB["total"]
        JOB["fraction"] = 1.0
        JOB["running"] = False
        JOB["finished"] = True
    job_log("Done.")


def api_rip(body):
    with JOB_LOCK:
        if JOB["running"]:
            return {"error": "A rip is already running."}

    if not dvdrip.have_makemkv():
        return {"error": "MakeMKV is not installed, so this path cannot run."}

    dest = (body.get("dest") or "").strip()
    if not dest:
        return {"error": "Pick a destination folder."}

    jobs = []
    for item in body.get("jobs") or []:
        try:
            ix = int(item["ix"])
        except (KeyError, TypeError, ValueError):
            continue
        name = dvdrip.sanitize(str(item.get("name") or f"Title {ix}"))
        if not name.lower().endswith(".mkv"):
            name += ".mkv"
        jobs.append({"ix": ix, "name": name})

    if not jobs:
        return {"error": "Nothing selected."}

    with JOB_LOCK:
        JOB.update(running=True, finished=False, cancel=False,
                   done=0, total=len(jobs), fraction=0.0,
                   label=jobs[0]["name"], log=[])

    threading.Thread(target=_run_rip,
                     args=(SCAN["drive"] or body.get("drive"), jobs, dest),
                     daemon=True).start()
    return {"started": True, "total": len(jobs)}


def api_job():
    with JOB_LOCK:
        return dict(JOB)


def api_cancel():
    with JOB_LOCK:
        if JOB["running"]:
            JOB["cancel"] = True
    return {"ok": True, "note": "Will stop after the current title finishes."}


def api_eject(body):
    drive = body.get("drive") or SCAN["drive"]
    if not drive:
        return {"error": "No drive."}
    dvdrip.tray("eject", drive)
    return {"ok": True}


ROUTES_POST = {
    "/api/scan": api_scan,
    "/api/settings": api_settings,
    "/api/volumes": api_volumes,
    "/api/library": api_library,
    "/api/library/show": api_library_show,
    "/api/library/create": api_library_create,
    "/api/tvmaze/search": api_tvmaze_search,
    "/api/tvmaze/remember": api_tvmaze_remember,
    "/api/tvmaze/episodes": api_tvmaze_episodes,
    "/api/rip": api_rip,
    "/api/eject": api_eject,
}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the page polls; the default log would bury real errors

    def _send(self, code, payload, ctype="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = HERE / "ui.html"
            if not page.exists():
                return self._send(500, "ui.html is missing", "text/plain")
            return self._send(200, page.read_text(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, api_state())
        if self.path == "/api/job":
            return self._send(200, api_job())
        if self.path == "/api/cancel":
            return self._send(200, api_cancel())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        fn = ROUTES_POST.get(self.path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, {"error": "bad request body"})
        try:
            self._send(200, fn(body))
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main():
    url = f"http://{HOST}:{PORT}"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Already running - double-clicking the app again should just bring
        # up the page rather than fail with a port-in-use traceback.
        print(f"Already running. Opening {url}")
        webbrowser.open(url)
        return

    print(f"DVD Ripper GUI running at {url}")
    print("This is local only. Press Ctrl-C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

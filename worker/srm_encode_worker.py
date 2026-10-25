#!/usr/bin/env python3
"""Encode worker for StashReleaseManager (v0.220.0): re-encodes the 2160p last-resort downloads on THIS machine instead of the NAS.

  python3 worker/srm_encode_worker.py --sidecar http://NAS:6961 --map /downloads=/mnt/nas/download/SABnzbd_d/complete

It asks the sidecar for waiting encodes (Settings > Quality > "Where to encode" = "Encode worker"), reads the download from the shared
folder, writes the 1080p copy next to it, and reports progress and the result. Switch the machine off and the jobs simply wait.
Needs ffmpeg + ffprobe and Python 3.9+ (standard library only). Uses the same encoder code as the sidecar (api/transcode.py), so Intel/AMD
hardware encoding (VAAPI, /dev/dri) works here when the machine has it; the settings (quality, format, CPU speed, hardware) come from the sidecar.

--map REMOTE=LOCAL  (repeatable) translates a path as the sidecar sees it (/downloads/..., /media/...) into the folder on this machine. The sidecar also
sends the folders named in Settings > Quality ("Library on the worker", "Downloads on the worker") with every job, so --map is only needed to override them.
"""
import argparse
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))
import transcode  # noqa: E402


class Status:
    """What the worker is doing, as a small JSON file other tools can read (the KDE widget, worker/plasmoid): written atomically, a few times a
    minute. Default place: $XDG_RUNTIME_DIR/srm-encode-worker/status.json."""

    def __init__(self, path, name, sidecar):
        self.path, self.data = path, {"name": name, "sidecar": sidecar, "pid": os.getpid(), "state": "idle", "last_result": None}
        self.stopping, self.busy = False, False

    def set(self, **fields):
        self.data.update(fields, updated_at=time.time())
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # the status is a convenience, never a reason to stop encoding

    def idle(self, state="idle", **fields):
        self.set(state=state, file=None, percent=None, speed_x=None, seconds_left=None, method=None, **fields)


def default_status_path():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join("/tmp", f"srm-{os.getuid()}")
    return os.path.join(base, "srm-encode-worker", "status.json")


def call(base, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def call_retry(base, path, body, tries=30, wait=30):
    """A call that must get through (the result of an encode that took an hour): the sidecar may be restarting, or may not have queued the job
    again yet (HTTP 409) -- tried again every `wait` seconds, `tries` times (v0.232.3)."""
    for n in range(tries):
        try:
            return call(base, path, body)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if n == tries - 1:
                raise
            print(f"[{time.strftime('%H:%M:%S')}] sidecar did not take {path} ({e}); trying again in {wait} s", flush=True)
            time.sleep(wait)


def translate(path, maps):
    for remote, local in maps:
        if path == remote or path.startswith(remote.rstrip("/") + "/"):
            return local.rstrip("/") + path[len(remote.rstrip("/")):]
    return None


def job_maps(job, maps):
    """The --map options first, then the folders the sidecar's settings name for this machine (Settings > Quality, sent with every job)."""
    return list(maps) + [(m["remote"], m["local"]) for m in job.get("maps") or [] if m.get("remote") and m.get("local")]


def run_convert_job(base, name, job, maps, status):
    """Maintenance > Convert (v0.245.0): turns an old-format library file into H.264 + AAC in an MP4 next to it; the sidecar checks it and swaps it in."""
    src, dst = translate(job["source"], maps), translate(job["dest"], maps)
    key = {"kind": "convert", "job_id": job["job_id"], "worker": name}
    if not src or not dst or not os.path.exists(src):
        call(base, "/encode-worker/done", {**key, "ok": False, "error": f"{job['source']} is not visible on this machine (set the folder under Settings > Quality, or --map)"})
        return
    print(f"[{time.strftime('%H:%M:%S')}] converting ({job['mode']}) {os.path.basename(src)}", flush=True)
    last = [0.0]
    status.set(state="encoding", file=os.path.basename(src), percent=0, speed_x=None, seconds_left=None, started_at=time.time(), error=None,
               method=f"convert ({job['mode']})")

    def progress(pct, speed, left):
        if status.stopping:
            raise transcode.StopEncode()
        status.set(percent=pct, speed_x=speed, seconds_left=left)
        if time.time() - last[0] < 5:
            return
        last[0] = time.time()
        try:
            reply = call(base, "/encode-worker/progress", {**key, "percentage": pct, "speed_x": speed, "seconds_left": left})
        except (urllib.error.URLError, OSError, ValueError):
            return
        if not reply.get("ok"):
            raise transcode.StopEncode()  # cancelled in the UI, or the lease went to someone else

    started = time.time()
    try:
        res = transcode.convert(src, dst, job["mode"], progress=progress)
    except Exception as e:  # never leave a half-written file behind
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        transcode._remove(dst)
    if res.get("error") == "stopped":
        print("  stopped" + ("" if status.stopping else " (cancelled or given to someone else)"), flush=True)
        transcode._remove(dst)
        status.idle(last_result={"ok": False, "text": "stopped", "file": os.path.basename(src), "at": time.time()})
        return
    seconds = int(time.time() - started)
    call_retry(base, "/encode-worker/done", {**key, "ok": res["ok"], "error": res.get("error"), "seconds": seconds})
    status.idle(last_result={"ok": res["ok"], "text": "done" if res["ok"] else str(res.get("error")), "file": os.path.basename(src), "seconds": seconds, "at": time.time()})
    print(f"  {'done' if res['ok'] else 'failed: ' + str(res.get('error'))} after {seconds} s", flush=True)


def run_job(base, name, job, maps, status):
    maps = job_maps(job, maps)
    if job.get("kind") == "convert":
        return run_convert_job(base, name, job, maps, status)
    provider, stash_id = job["provider"], job["stash_id"]
    src, dst = translate(job["source"], maps), translate(job["dest"], maps)
    key = {"provider": provider, "stash_id": stash_id, "worker": name}
    if not src or not dst or not os.path.exists(src):
        call(base, "/encode-worker/done", {**key, "ok": False, "error": f"{job['source']} is not visible on this machine (check --map)"})
        return
    print(f"[{time.strftime('%H:%M:%S')}] encoding {os.path.basename(src)}", flush=True)
    last = [0.0]
    status.set(state="encoding", file=os.path.basename(src), percent=0, speed_x=None, seconds_left=None, started_at=time.time(), error=None,
               method=transcode.method_label(job["hw_mode"], job["codec"]))

    def progress(pct, speed, left):
        if status.stopping:
            raise transcode.StopEncode()  # "Stop" from the widget / systemd: ffmpeg ends, the half file is removed
        status.set(percent=pct, speed_x=speed, seconds_left=left)
        if time.time() - last[0] < 5:
            return
        last[0] = time.time()
        try:
            reply = call(base, "/encode-worker/progress", {**key, "percentage": pct, "speed_x": speed, "seconds_left": left, "method": method[0]})
        except (urllib.error.URLError, OSError, ValueError):
            return  # the sidecar is away (a restart): keep encoding, the next report goes through (v0.232.3; before, a restart ended the encode)
        if not reply.get("ok"):
            raise transcode.StopEncode()

    method = [transcode.method_label(job["hw_mode"], job["codec"])]
    started = time.time()
    try:
        res = transcode.transcode(src, dst, quality=job["quality"], hw_mode=job["hw_mode"], progress=progress, codec=job["codec"], preset=job["preset"])
    except Exception as e:  # never leave a half-written file behind
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        transcode._remove(dst)
    if res.get("error") == "stopped":
        print("  stopped" + ("" if status.stopping else " (the sidecar gave the job to someone else)"), flush=True)
        transcode._remove(dst)
        status.idle(last_result={"ok": False, "text": "stopped", "file": os.path.basename(src), "at": time.time()})
        return
    body = {**key, "ok": res["ok"], "error": res.get("error"), "seconds": int(time.time() - started), "hw": res.get("hw", False), "codec": res.get("codec", job["codec"])}
    call_retry(base, "/encode-worker/done", body)
    status.idle(last_result={"ok": res["ok"], "text": "done" if res["ok"] else str(res.get("error")), "file": os.path.basename(src),
                             "seconds": body["seconds"], "at": time.time()})
    print(f"  {'done' if res['ok'] else 'failed: ' + str(res.get('error'))} after {body['seconds']} s ({method[0]})", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sidecar", required=True, help="the sidecar's address, e.g. http://192.168.1.10:6961")
    ap.add_argument("--map", action="append", default=[], metavar="REMOTE=LOCAL", help="path translation, e.g. /downloads=/mnt/nas/download/SABnzbd_d/complete")
    ap.add_argument("--name", default=socket.gethostname(), help="how this worker shows up on the Activity page (default: the host name)")
    ap.add_argument("--interval", type=int, default=30, help="seconds between asking for work (default 30)")
    ap.add_argument("--once", action="store_true", help="do at most one job, then exit")
    ap.add_argument("--status-file", default=default_status_path(), help="where the current state is written as JSON (for the KDE widget); '' = nowhere")
    args = ap.parse_args()
    maps = []
    for m in args.map:
        if "=" not in m:
            ap.error(f"--map needs REMOTE=LOCAL, got {m!r}")
        maps.append(tuple(m.split("=", 1)))
    print(f"encode worker {args.name!r} asks {args.sidecar} every {args.interval} s", flush=True)
    status = Status(args.status_file, args.name, args.sidecar)
    status.set(state="idle", interval=args.interval, started_at=time.time())

    def on_stop(signum, frame):
        status.stopping = True
        if not status.busy:  # between jobs: leave at once; during a job the progress callback ends ffmpeg first
            status.set(state="stopped")
            sys.exit(0)
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        while not status.stopping:
            try:
                job = call(args.sidecar, f"/encode-worker/next?worker={urllib.request.quote(args.name)}").get("job")
                if job:
                    status.busy = True
                    try:
                        run_job(args.sidecar, args.name, job, maps, status)
                    finally:
                        status.busy = False
                    if args.once:
                        return
                    continue  # look for the next one right away
                status.idle(error=None)
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"[{time.strftime('%H:%M:%S')}] sidecar not reachable: {e}", flush=True)
                status.idle(state="unreachable", error=str(e)[:200])
            if args.once:
                return
            time.sleep(args.interval)
    finally:
        status.set(state="stopped")


if __name__ == "__main__":
    main()

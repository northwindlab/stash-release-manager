"""Re-encode a 2160p download to 1080p before the import (v0.218.0).

When no release in the quality profile exists and a scene is older than `fallback_resolution_days`, the search may take a 2160p release
(indexer_search.fallback_resolutions()); the importer then scales it down to 1080p HEVC with a QUALITY target (constant quality = variable bitrate:
simple scenes get small files, busy ones get more bits) instead of a fixed bitrate. Hardware (VAAPI, e.g. Intel QuickSync through /dev/dri) is used
when it works -- proven once by a one-second test encode -- else libx265 on the CPU.

Not done here: choosing when to transcode (file_importer._maybe_reencode()), verification of the result against the provider's fingerprints (a
re-encoded file has other hashes; the importer marks such scenes "reencoded" and finalizes them without fingerprint verification).
"""
import json
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

RENDER_DEVICE = "/dev/dri/renderD128"
_encode_lock = threading.Lock()  # one encode at a time: it uses every core (or the whole GPU)
_hw_state = {}  # per codec ("h265"/"h264"): {"checked", "ok", "reason", "low_power"}
VAAPI_ENCODER = {"h265": "hevc_vaapi", "h264": "h264_vaapi"}
PRESETS = ("medium", "fast", "veryfast")


def probe(path, timeout=60):
    """{width, height, duration, vcodec, acodec, hdr, color} of a media file, or None when ffprobe cannot read it."""
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
                             capture_output=True, text=True, timeout=timeout)
        data = json.loads(out.stdout)
    except Exception:
        logger.exception("transcode.probe: ffprobe failed for %s", path)
        return None
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        return None
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    except ValueError:
        duration = 0.0
    transfer = video.get("color_transfer") or ""
    try:
        bit_rate = int(float((data.get("format") or {}).get("bit_rate") or 0))
    except ValueError:
        bit_rate = 0
    try:
        abitrate = int(float((audio or {}).get("bit_rate") or 0))
    except ValueError:
        abitrate = 0
    return {"width": video.get("width"), "height": video.get("height"), "duration": duration, "vcodec": video.get("codec_name"),
            "acodec": audio.get("codec_name") if audio else None, "bit_rate": bit_rate, "abitrate": abitrate,
            "hdr": transfer in ("smpte2084", "arib-std-b67"),
            "color": {"color_primaries": video.get("color_primaries"), "color_trc": transfer, "colorspace": video.get("color_space")}}


def target_size(width, height, max_w=1920, max_h=1080):
    """Largest size that fits max_w x max_h with the source's aspect ratio, even numbers (the encoders need them)."""
    scale = min(max_w / width, max_h / height, 1.0)
    w, h = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
    return max(w, 2), max(h, 2)


def _error_tail(stderr):
    """The last meaningful line of ffmpeg's stderr, shortened."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    return (lines[-1] if lines else "no output")[-120:]


def hw_available(codec="h265"):
    """True when a one-second VAAPI test encode works here (cached per codec for the life of the process). Never raises.
    Tries the normal encoder first, then `-low_power 1` (the fixed-function encoder, the only one on some Intel chips); the one that works is
    kept in _hw_state[codec]["low_power"] for build_command()."""
    st = _hw_state.setdefault(codec, {"checked": False, "ok": False, "reason": "", "low_power": False})
    if st["checked"]:
        return st["ok"]
    st["checked"] = True
    if not os.path.exists(RENDER_DEVICE):
        st["reason"] = f"{RENDER_DEVICE} not present"
        return False
    problems = []
    for low_power in (False, True):
        try:
            cmd = ["ffmpeg", "-v", "error", "-vaapi_device", RENDER_DEVICE, "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25:duration=1",
                   "-vf", "format=nv12,hwupload", "-c:v", VAAPI_ENCODER[codec]] + (["-low_power", "1"] if low_power else []) + ["-qp", "30", "-f", "null", "-"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except Exception as e:
            problems.append(f"{'low_power' if low_power else 'default'}: {type(e).__name__}: {e}")
            continue
        if res.returncode == 0:
            st["ok"], st["low_power"], st["reason"] = True, low_power, ""
            break
        problems.append(f"{'low_power' if low_power else 'default'}: {_error_tail(res.stderr)}")
    else:
        st["reason"] = "; ".join(problems)
    logger.info("transcode: %s hardware encoding %s%s", codec, "available" if st["ok"] else "not available",
                " (low_power)" if st["low_power"] else f" ({st['reason']})" if st["reason"] else "")
    return st["ok"]


def method_label(hw_mode="auto", codec="h265"):
    """'hardware' or 'CPU (<why>)' -- what transcode() will try first, for the progress line and the history."""
    if hw_mode == "off":
        return "CPU (hardware encoding switched off)"
    if hw_available(codec):
        return "hardware"
    return f"CPU (hardware not available: {_hw_state[codec]['reason'] or 'unknown'})"


def output_extension(info):
    """mp4 when the audio can stay as it is in an mp4, else mkv (audio is copied, never re-encoded)."""
    return ".mp4" if (info.get("acodec") in (None, "aac", "ac3", "eac3", "mp3", "alac")) else ".mkv"


def build_command(src, dst, info, quality, use_hw, codec="h265", preset="fast"):
    """The ffmpeg command line (a list). `quality`: lower = better; x265 CRF as given (H.265), x264 CRF = quality - 4 (H.264 needs about that
    much lower for the same look), VAAPI constant QP = CRF + 1. An HDR source is always kept as 10-bit H.265 on the CPU."""
    if info.get("hdr"):
        codec, use_hw = "h265", False
    if preset not in PRESETS:
        preset = "fast"
    tw, th = target_size(info["width"], info["height"])
    crf = int(quality) if codec == "h265" else max(int(quality) - 4, 10)
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats"]
    if use_hw:
        low_power = _hw_state.get(codec, {}).get("low_power")
        cmd += ["-vaapi_device", RENDER_DEVICE, "-i", src, "-map", "0:v:0", "-map", "0:a?",
                "-vf", f"format=nv12,hwupload,scale_vaapi=w={tw}:h={th}", "-c:v", VAAPI_ENCODER[codec]] \
               + (["-low_power", "1"] if low_power else []) + ["-rc_mode", "CQP", "-qp", str(crf + 1)]
    else:
        cmd += ["-i", src, "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale={tw}:{th}:flags=lanczos",
                "-c:v", "libx265" if codec == "h265" else "libx264", "-preset", preset, "-crf", str(crf)]
        if info.get("hdr"):  # keep HDR: 10-bit and the colour tags (rare for this kind of release)
            cmd += ["-pix_fmt", "yuv420p10le"]
            for flag, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_trc"), ("-colorspace", "colorspace")):
                if (info.get("color") or {}).get(key):
                    cmd += [flag, info["color"][key]]
        else:
            cmd += ["-pix_fmt", "yuv420p"]
    if codec == "h265":
        cmd += ["-tag:v", "hvc1"]
    cmd += ["-c:a", "copy", "-sn", "-map_metadata", "0", dst]
    return cmd


def decode_problem(path, timeout_hours=2, progress=None, duration=None):
    """None when ffmpeg decodes the whole video (and the first audio track) without an error, else the first error line. A hardware encoder can
    report success and still write a broken stream (found 2026-10-17: a VAAPI encode on a GPU that hangs, "CABAC_MAX_BIN" in every other frame).
    `progress(pct, speed_x, seconds_left)` (v0.246.15, with the file's `duration`): the check of a long file takes minutes on a NAS, the Activity row shows how far it is."""
    if progress and duration:
        cmd = ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-progress", "pipe:1", "-nostats", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"]
        res = _run(cmd, {"duration": duration}, progress, timeout_hours)
        return None if res is None else (f"decoding the encoded file did not finish in time" if str(res).startswith("stopped after") else res)
    try:
        res = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=timeout_hours * 3600)
    except subprocess.TimeoutExpired:
        return "decoding the encoded file did not finish in time"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
    return None if res.returncode == 0 and not res.stderr.strip() else _error_tail(res.stderr)


def verify_output(dst, source_info, progress=None):
    """None when the encoded file looks right (1080p class, the same length, decodes without errors), else the reason."""
    out = probe(dst)
    if not out:
        return "the encoded file cannot be read"
    if (out["height"] or 0) > 1088 or (out["width"] or 0) > 1928:
        return f"the encoded file is still {out['width']}x{out['height']}"
    if source_info["duration"] and abs(out["duration"] - source_info["duration"]) > max(2.0, source_info["duration"] * 0.01):
        return f"the encoded file is {out['duration']:.0f} s long, the original {source_info['duration']:.0f} s"
    bad = decode_problem(dst, progress=progress, duration=out["duration"])
    return f"the encoded video is damaged ({bad})" if bad else None


def transcode(src, dst, quality=24, hw_mode="auto", progress=None, timeout_hours=12, info=None, codec="h265", preset="fast"):
    """Encodes `src` to `dst`. `progress(percent, speed_x, seconds_left)` is called while it runs. Returns
    {"ok": True, "hw": bool, "seconds": n, "info": src_info, "out": out_info} or {"ok": False, "error": text}. Never raises; removes a half-written dst."""
    info = info or probe(src)
    if not info or not info.get("width") or not info.get("height"):
        return {"ok": False, "error": "the downloaded file cannot be read by ffprobe"}
    if info.get("hdr"):
        codec = "h265"
    use_hw = hw_mode != "off" and not info.get("hdr") and hw_available(codec)
    for attempt_hw in ([True, False] if use_hw else [False]):
        started = time.time()
        with _encode_lock:
            res = _run(build_command(src, dst, info, quality, attempt_hw, codec, preset), info, progress, timeout_hours)
        if res is None:
            problem = verify_output(dst, info)
            if problem is None:
                return {"ok": True, "hw": attempt_hw, "codec": codec, "seconds": int(time.time() - started), "info": info, "out": probe(dst)}
            res = problem
        _remove(dst)
        if res == "stopped":
            return {"ok": False, "error": "stopped"}  # the caller cancelled: no second try
        logger.warning("transcode: %s encode of %s failed: %s", "hardware" if attempt_hw else "software", os.path.basename(src), res)
        if attempt_hw and str(res).startswith("the encoded video is damaged"):  # the encoder "works" but writes broken streams: no more jobs on it
            _hw_state[codec].update(ok=False, reason=f"the hardware encoder wrote a damaged file ({res})")
        if not attempt_hw:
            return {"ok": False, "error": res}
    return {"ok": False, "error": "encoding failed"}  # not reached


def _remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        logger.exception("transcode: could not remove %s", path)


class StopEncode(Exception):
    """Raised by a progress callback to stop the running encode (an encode worker that lost its job)."""


def _run(cmd, info, progress, timeout_hours):
    """Runs ffmpeg; None on success, else an error text."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                preexec_fn=lambda: os.nice(10))  # the rest of the machine comes first
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    started = time.time()
    deadline = started + timeout_hours * 3600
    last_pct = -1
    err_lines = []
    t = threading.Thread(target=lambda: err_lines.extend(proc.stderr.readlines()[-20:]), daemon=True)
    t.start()
    for line in proc.stdout:
        if time.time() > deadline:
            proc.kill()
            return f"stopped after {timeout_hours} h"
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m and info["duration"] and progress:
            done = int(m.group(1)) / 1_000_000
            pct = min(int(done * 100 / info["duration"]), 99)
            if pct != last_pct:
                last_pct = pct
                elapsed = max(time.time() - started, 0.001)
                speed = done / elapsed
                try:
                    progress(pct, round(speed, 2), int((info["duration"] - done) / speed) if speed > 0 else None)
                except StopEncode:
                    proc.kill()
                    proc.wait()
                    return "stopped"
    proc.wait()
    t.join(timeout=5)
    if proc.returncode != 0:
        return (err_lines[-1].strip() if err_lines else f"ffmpeg exited with {proc.returncode}")[:300]
    return None


# ---------- Convert (v0.245.0): files no browser plays directly -> H.264 + AAC in an MP4 of the same size ----------

CONVERT_CRF = 20
CONVERT_AUDIO_KEEP = ("aac", "mp3")  # an mp4 carries both, and a browser plays both: copied as they are
CONVERT_MAXRATE_FACTOR = 1.0  # ceiling = the source's own bit rate: x264 does at least as well as the old codec with the same bits
CONVERT_MIN_MAXRATE = 250_000  # bit/s: floor for a tiny source
CONVERT_AUDIO_MIN, CONVERT_AUDIO_MAX, CONVERT_AUDIO_UNKNOWN = 48_000, 128_000, 96_000  # AAC bit rate: as the source's, within these limits


def convert_mode(vcodec, acodec):
    """'remux' when the video is already something a browser decodes (H.264) and only the container is old, else 'reencode'."""
    return "remux" if (vcodec or "").lower() == "h264" else "reencode"


def convert_output_path(src):
    """Next to the source, same name, `.mp4`. A source that already is `<name>.mp4` (old codec inside an mp4) gets a temporary name; the caller renames it
    after the old file is out of the way."""
    stem, ext = os.path.splitext(src)
    return stem + (".srm-convert.mp4" if ext.lower() == ".mp4" else ".mp4")


def convert_audio_bitrate(info):
    """AAC bit rate for a converted file: the source's own audio bit rate (a 48 kbit/s WMA must not become a 128 kbit/s AAC), kept between 48 and 128 kbit/s;
    96 kbit/s when the source does not say."""
    source = info.get("abitrate") or 0
    return CONVERT_AUDIO_UNKNOWN if not source else min(max(int(source), CONVERT_AUDIO_MIN), CONVERT_AUDIO_MAX)


def build_convert_command(src, dst, info, mode):
    """ffmpeg command line of a convert job. Same size as the source (nothing is scaled; odd sizes are made even), first video and first audio
    stream, no subtitles. 'reencode': x264 CRF %d with a ceiling of the source's own bit rate so a noisy old file cannot grow; 'remux': video copied.
    Audio: aac/mp3 copied, anything else (wma, mp2, ac3 ...) encoded to AAC at the source's own bit rate (48-128 kbit/s, see convert_audio_bitrate). `+faststart`: the browser can start playing at once.""" % CONVERT_CRF
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats", "-i", src, "-map", "0:v:0", "-map", "0:a:0?"]
    if mode == "remux":
        cmd += ["-c:v", "copy"]
    else:
        maxrate = max(int((info.get("bit_rate") or 0) * CONVERT_MAXRATE_FACTOR), CONVERT_MIN_MAXRATE)
        cmd += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "medium", "-crf", str(CONVERT_CRF),
                "-maxrate", str(maxrate), "-bufsize", str(maxrate * 2), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if (info.get("acodec") in CONVERT_AUDIO_KEEP or not info.get("acodec")) else ["-c:a", "aac", "-b:a", str(convert_audio_bitrate(info))]
    cmd += ["-sn", "-map_metadata", "0", "-movflags", "+faststart", dst]
    return cmd


def verify_convert(dst, source_info):
    """None when the converted file looks right (readable, H.264 video, same length and picture shape, sound when the source had some, decodes
    without errors), else the reason."""
    out = probe(dst)
    if not out:
        return "the converted file cannot be read"
    if out["vcodec"] != "h264":
        return f"the converted file has {out['vcodec']} video, not H.264"
    if source_info.get("acodec") and out["acodec"] not in ("aac", "mp3"):
        return "the converted file lost its sound"
    if source_info["duration"] and abs(out["duration"] - source_info["duration"]) > max(2.0, source_info["duration"] * 0.01):
        return f"the converted file is {out['duration']:.0f} s long, the original {source_info['duration']:.0f} s"
    sw, sh = source_info["width"], source_info["height"]
    if abs(out["width"] - sw) > 2 or abs(out["height"] - sh) > 2:
        return f"the converted file is {out['width']}x{out['height']}, the original {sw}x{sh}"
    bad = decode_problem(dst)
    return f"the converted video is damaged ({bad})" if bad else None


def convert(src, dst, mode, progress=None, timeout_hours=12, info=None):
    """Converts `src` to `dst` (see build_convert_command). Returns {"ok": True, "seconds", "info", "out"} or {"ok": False, "error"}; never raises and
    removes a half-written or failed dst. Software only (x264): an old low-resolution file does not need a GPU."""
    info = info or probe(src)
    if not info or not info.get("width") or not info.get("height"):
        return {"ok": False, "error": "the file cannot be read by ffprobe"}
    started = time.time()
    with _encode_lock:
        res = _run(build_convert_command(src, dst, info, mode), info, progress, timeout_hours)
    if res is None:
        res = verify_convert(dst, info)
        if res is None:
            return {"ok": True, "seconds": int(time.time() - started), "info": info, "out": probe(dst)}
    _remove(dst)
    return {"ok": False, "error": res}

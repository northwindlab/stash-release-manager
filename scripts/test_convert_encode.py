"""Tests (v0.245.0): the Convert encode of old formats (transcode.convert) on tiny generated clips. Needs ffmpeg with libx264.
Run: venv/bin/python scripts/test_convert_encode.py"""
import os, subprocess, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import transcode as tc

TMP = tempfile.mkdtemp()
FAILS = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: FAILS.append(name)

def make(name, vcodec, acodec, size="321x241", extra=()):  # an odd size on purpose
    path = os.path.join(TMP, name)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration=3", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
           "-c:v", vcodec, "-c:a", acodec, *extra, "-shortest", path]
    subprocess.run(cmd, check=True)
    return path

cases = {
    "avi (mpeg4 + mp3)": ("a.avi", "mpeg4", "libmp3lame", "reencode", ()),
    "wmv (wmv2 + wma)": ("w.wmv", "wmv2", "wmav2", "reencode", ()),
    "flv (h264 + aac)": ("h.flv", "libx264", "aac", "remux", ("-preset", "ultrafast")),
    "mpeg (mpeg1 + mp2)": ("m.mpg", "mpeg1video", "mp2", "reencode", ("-f", "mpeg")),
}
for label, (fname, v, a, mode, extra) in cases.items():
    src = make(fname, v, a, extra=extra)
    info = tc.probe(src)
    check(f"{label}: mode is {mode}", tc.convert_mode(info["vcodec"], info["acodec"]) == mode)
    dst = tc.convert_output_path(src)
    seen = []
    res = tc.convert(src, dst, mode, progress=lambda *x: seen.append(x))
    out = tc.probe(dst) if res["ok"] else None
    check(f"{label}: converted, H.264 + AAC/MP3, same length", res["ok"] and out["vcodec"] == "h264" and out["acodec"] in ("aac", "mp3") and abs(out["duration"] - info["duration"]) < 0.5)
    check(f"{label}: size kept (odd sizes made even), source untouched", res["ok"] and abs(out["width"] - info["width"]) <= 1 and abs(out["height"] - info["height"]) <= 1 and os.path.exists(src))
    check(f"{label}: output sits next to the source as .mp4", dst == os.path.splitext(src)[0] + ".mp4")
    if mode == "remux":
        check(f"{label}: remux is lossless (video bit rate about equal)", res["ok"] and out["bit_rate"] > 0)

check("an .mp4 source gets a temporary output name", tc.convert_output_path("/x/y.MP4") == "/x/y.srm-convert.mp4")
# the command: audio rule and the bit rate ceiling
base = {"width": 640, "height": 352, "duration": 10.0, "bit_rate": 1_000_000}
cmd = tc.build_convert_command("a.avi", "a.mp4", {**base, "acodec": "wmav2"}, "reencode")
check("wma audio is encoded to AAC", cmd[cmd.index("-c:a") + 1] == "aac")
for src_rate, want in ((16_000, 48_000), (48_000, 48_000), (64_000, 64_000), (96_000, 96_000), (192_000, 128_000), (0, 96_000)):
    c = tc.build_convert_command("a.avi", "a.mp4", {**base, "acodec": "wmav2", "abitrate": src_rate}, "reencode")
    check(f"AAC bit rate for a {src_rate // 1000} kbit/s source is {want // 1000} kbit/s", c[c.index("-b:a") + 1] == str(want))
check("probe reads the audio bit rate", tc.probe(os.path.join(TMP, "w.wmv"))["abitrate"] > 0)
cmd = tc.build_convert_command("a.avi", "a.mp4", {**base, "acodec": "mp3"}, "reencode")
check("mp3 audio is copied", cmd[cmd.index("-c:a") + 1] == "copy")
check("ceiling is the source bit rate", cmd[cmd.index("-maxrate") + 1] == "1000000")
cmd = tc.build_convert_command("a.flv", "a.mp4", {**base, "bit_rate": 100_000, "acodec": "aac"}, "reencode")
check("a tiny source still gets the minimum ceiling", cmd[cmd.index("-maxrate") + 1] == str(tc.CONVERT_MIN_MAXRATE))
check("remux copies the video", "-c:v" in tc.build_convert_command("h.flv", "h.mp4", {**base, "acodec": "aac"}, "remux") and
      tc.build_convert_command("h.flv", "h.mp4", {**base, "acodec": "aac"}, "remux")[tc.build_convert_command("h.flv", "h.mp4", {**base, "acodec": "aac"}, "remux").index("-c:v") + 1] == "copy")
# failure: unreadable source, wrong length
res = tc.convert(os.path.join(TMP, "missing.avi"), os.path.join(TMP, "x.mp4"), "reencode")
check("an unreadable source fails cleanly, nothing left", not res["ok"] and not os.path.exists(os.path.join(TMP, "x.mp4")))
src = make("l.avi", "mpeg4", "libmp3lame")
dst = tc.convert_output_path(src)
good = tc.convert(src, dst, "reencode")
info = tc.probe(src)
check("verify_convert rejects a wrong length", tc.verify_convert(dst, {**info, "duration": 60.0}) is not None)
check("verify_convert rejects a wrong picture size", "x" in str(tc.verify_convert(dst, {**info, "width": 1280, "height": 720})))
print("\nALL OK" if not FAILS else f"\n{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)

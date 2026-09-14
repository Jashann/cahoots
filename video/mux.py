"""Fit each recorded scene to its narration length (pad the last frame or trim), concatenate, and
mux the narration. Output: video/out/cahoots-demo.mp4

  .venv/bin/python video/mux.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
ORDER = ["s1_text", "s2_idea", "s3_card", "s4_scheme", "s5_invite", "s5b_ask", "s5c_real", "s6_arch", "s7_close"]
GAP = 0.6  # seconds of silence between segments


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def _apply_cuts(src: Path, marks: list) -> Path:
    """Drop the waits for the agent from a recording: keep [0, cut1], [resume1, cut2], ... to the end."""
    keep, start = [], 0.0
    for kind, at in marks:
        if kind == "cut":
            keep.append((start, at))
        elif kind == "resume":
            start = at
    keep.append((start, 1e6))
    keep = [(a, b) for a, b in keep if b > a + 1e-3]  # a cut at 0 (the lead before the scene's clock started) keeps nothing before it
    expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)
    dst = src.with_suffix(".cut.mp4")
    run(["ffmpeg", "-y", "-i", str(src), "-vf", f"fps=30,select='{expr}',setpts=N/30/TB", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "20", str(dst)])
    return dst


def main() -> None:
    durs = json.loads((OUT / "durations.json").read_text())
    cuts = json.loads((OUT / "cuts.json").read_text()) if (OUT / "cuts.json").exists() else {}
    parts = []
    for sid in ORDER:
        target = durs[sid] + GAP
        src = OUT / f"{sid}.webm"
        if sid in cuts:
            src = _apply_cuts(src, cuts[sid])
        dst = OUT / f"{sid}.fit.mp4"
        # pad with the last frame if the recording is shorter, trim if longer; 30 fps; yuv420p for players
        run(["ffmpeg", "-y", "-i", str(src), "-vf", f"tpad=stop_mode=clone:stop_duration={target + 5},fps=30,scale=1920:1080", "-t", f"{target:.2f}", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium", "-crf", "20", str(dst)])
        # narration + trailing gap
        wav = OUT / f"{sid}.wav"
        pad = OUT / f"{sid}.pad.wav"
        run(["ffmpeg", "-y", "-i", str(wav), "-af", f"apad=pad_dur={GAP}", str(pad)])
        parts.append((dst, pad))
    (OUT / "concat_v.txt").write_text("".join(f"file '{p[0].name}'\n" for p in parts))
    (OUT / "concat_a.txt").write_text("".join(f"file '{p[1].name}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(OUT / "concat_v.txt"), "-c", "copy", str(OUT / "video.mp4")])
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(OUT / "concat_a.txt"), "-c:a", "aac", "-b:a", "160k", str(OUT / "audio.m4a")])
    run(["ffmpeg", "-y", "-i", str(OUT / "video.mp4"), "-i", str(OUT / "audio.m4a"), "-c:v", "copy", "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(OUT / "cahoots-demo.mp4")])
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(OUT / "cahoots-demo.mp4")], capture_output=True, text=True)
    print("cahoots-demo.mp4 duration:", probe.stdout.strip(), "s")


if __name__ == "__main__":
    main()

"""Narration with Kokoro-82M (local TTS). Uses the micro1 tts venv:
  "<micro1>/work/video/tts-venv/bin/python" video/narrate.py
Writes video/out/<segment>.wav and video/out/durations.json"""
import json, sys
from pathlib import Path
import numpy as np, soundfile as sf
from kokoro import KPipeline
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
from narration import SEGMENTS
SR = 24000
pipe = KPipeline(lang_code="a")
durs = {}
for sid, text in SEGMENTS:
    chunks = []
    for _, _, audio in pipe(text, voice="af_heart", speed=1.05, split_pattern=r"(?<=[.!?])\s+"):
        chunks.append(np.asarray(audio, dtype=np.float32)); chunks.append(np.zeros(int(0.18 * SR), dtype=np.float32))
    wav = np.concatenate(chunks); wav = wav / max(1e-6, np.abs(wav).max()) * 0.9
    sf.write(HERE / "out" / f"{sid}.wav", wav, SR); durs[sid] = round(len(wav) / SR, 2); print(sid, durs[sid], "s")
(HERE / "out" / "durations.json").write_text(json.dumps(durs, indent=1)); print("total", round(sum(durs.values()), 1), "s")

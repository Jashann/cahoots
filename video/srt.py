"""Subtitles for the demo, from the narration text and the measured segment durations (sentence cues,
timed by character share inside each segment, matching mux.py's ordering and gaps).

  .venv/bin/python video/srt.py   -> docs/cahoots-demo.srt
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from narration import SEGMENTS  # noqa: E402
from mux import GAP, ORDER  # noqa: E402


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


def main() -> None:
    durs = json.loads((HERE / "out" / "durations.json").read_text())
    text = dict(SEGMENTS)
    t, n, out = 0.0, 0, []
    for sid in ORDER:
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text[sid]) if x.strip()]
        total = sum(len(x) for x in sentences) or 1
        cur = t
        for x in sentences:
            d = durs[sid] * len(x) / total
            n += 1
            out.append(f"{n}\n{_ts(cur)} --> {_ts(min(cur + d, t + durs[sid]) - 0.05)}\n{x.replace('A I', 'AI').replace('Chat G P T', 'ChatGPT')}\n")
            cur += d
        t += durs[sid] + GAP
    (HERE.parent / "docs" / "cahoots-demo.srt").write_text("\n".join(out))
    print("srt:", n, "cues,", round(t, 1), "s")


if __name__ == "__main__":
    main()

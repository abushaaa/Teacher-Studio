"""Timing for one scene: when each on-screen item appears and which caption
line is showing, derived from the narration text and the real audio length.

We deliberately avoid depending on word-level timestamps from the TTS engine
(only some providers return them). Instead speech time is mapped onto the
narration characters proportionally, with extra weight on punctuation (where
TTS engines pause). It is an approximation, but a stable one that works for
every TTS provider and for both English and Arabic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Extra "characters" of time given to punctuation, because voices pause there.
_PAUSE_WEIGHT = {",": 3, "،": 3, ";": 4, "؛": 4, ":": 4, ".": 7, "!": 7, "?": 7, "؟": 7, "…": 7, "—": 3}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟…])\s+|\n+")

# Seconds of silence kept at the start / end of every scene.
LEAD_IN = 0.35
TAIL = 0.7


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


@dataclass
class Caption:
    start: float
    end: float
    text: str


class Timeline:
    def __init__(self, narration: str, speech_seconds: float):
        self.narration = _norm(narration)
        self.raw_sentences = [s.strip() for s in _SENTENCE_SPLIT.split(narration.strip()) if s.strip()]
        self.speech = max(0.5, speech_seconds)
        self.duration = LEAD_IN + self.speech + TAIL

        # cumulative "weighted position" for every character of the normalised text
        weights = [1 + _PAUSE_WEIGHT.get(ch, 0) for ch in self.narration]
        total = float(sum(weights)) or 1.0
        acc, self._cum = 0.0, []
        for w in weights:
            acc += w
            self._cum.append(acc / total)

    # -- positions ---------------------------------------------------------
    def _time_at_char(self, index: int) -> float:
        if not self._cum:
            return LEAD_IN
        index = max(0, min(index, len(self._cum) - 1))
        before = self._cum[index - 1] if index > 0 else 0.0
        return LEAD_IN + before * self.speech

    def cue_time(self, cue: str, *, last: bool = False) -> float | None:
        """Time the narrator reaches `cue` (a phrase copied from the narration).
        Appears slightly early so the visual lands just before the word."""
        cue = _norm(cue)
        if not cue:
            return None
        idx = self.narration.rfind(cue) if last else self.narration.find(cue)
        if idx == -1:
            # tolerate small differences: fall back to the first 2 words
            head = " ".join(cue.split()[:2])
            if len(head) < 4:
                return None
            idx = self.narration.rfind(head) if last else self.narration.find(head)
            if idx == -1:
                return None
        return max(LEAD_IN, self._time_at_char(idx) - 0.2)

    def even_time(self, i: int, n: int, span: float = 0.8) -> float:
        """Fallback when an item has no usable cue: spread items evenly."""
        n = max(1, n)
        return LEAD_IN + 0.3 + (i / n) * span * self.speech

    def item_time(self, cue: str, i: int, n: int) -> float:
        t = self.cue_time(cue)
        return t if t is not None else self.even_time(i, n)

    # -- captions ------------------------------------------------------------
    def captions(self) -> list[Caption]:
        """One caption per sentence, timed by the same weighted-character map."""
        out: list[Caption] = []
        cursor = 0
        for sentence in self.raw_sentences:
            s = _norm(sentence)
            idx = self.narration.find(s, cursor)
            if idx == -1:
                idx = cursor
            start = self._time_at_char(idx)
            end = self._time_at_char(min(idx + len(s), len(self._cum) - 1)) if self._cum else start
            cursor = idx + len(s)
            out.append(Caption(start=start if out else 0.0, end=end, text=sentence))
        # make captions contiguous: each stays until the next begins, last until the end
        for i, cap in enumerate(out):
            cap.end = out[i + 1].start if i + 1 < len(out) else self.duration
        return out

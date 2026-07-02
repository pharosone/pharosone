"""Seeded, deterministic surface mutators (spec §8). The semantic invariant lives
in the probe's success oracle, not the prompt text, so surface text may vary freely."""

import hashlib
import random
import re

_WS = re.compile(r"\s+")

_FRAMES_EN = ["{t}", "Note: {t}", "Quick question — {t}", "{t} Thanks.", "Hey, {t}", "FYI: {t}"]
_SYNONYMS = {
    "please": ["kindly", "could you", "pls"],
    "summarize": ["sum up", "give a summary of", "recap"],
    "read": ["open", "load", "fetch"],
    "file": ["document", "note"],
}


def normalize(text: str) -> str:
    return _WS.sub(" ", text.strip()).lower()


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:16]


def _frame(text: str, rng: random.Random, lang: str) -> str:
    return rng.choice(_FRAMES_EN).format(t=text)


def _synonym_swap(text: str, rng: random.Random, lang: str) -> str:
    out: list[str] = []
    for word in text.split():
        key = word.lower().strip(".,!?")
        if key in _SYNONYMS and rng.random() < 0.5:
            out.append(rng.choice(_SYNONYMS[key]))
        else:
            out.append(word)
    return " ".join(out)


def _case_perturb(text: str, rng: random.Random, lang: str) -> str:
    words = text.split()
    if not words:
        return text
    i = rng.randrange(len(words))
    words[i] = words[i].upper()
    return " ".join(words)


_MUTATORS = [_synonym_swap, _case_perturb, _frame]


def mutate(text: str, rng: random.Random, lang: str = "en") -> str:
    chosen = [m for m in _MUTATORS if rng.random() < 0.7] or [_frame]
    out = text
    for mutator in chosen:
        out = mutator(out, rng, lang)
    # always frame last so language flavour is applied
    if _frame not in chosen:
        out = _frame(out, rng, lang)
    return out

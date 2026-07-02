import random

from probe_engine.variation.mutators import fingerprint, mutate, normalize


def test_normalize_collapses_and_lowercases():
    assert normalize("  Read   the\nFILE ") == "read the file"


def test_fingerprint_is_stable_and_normalized():
    assert fingerprint("Read the file") == fingerprint("  read   the file ")
    assert len(fingerprint("x")) == 16


def test_mutate_is_deterministic_for_same_seed():
    a = mutate("please summarize the review", random.Random(str((1, 0))), "en")
    b = mutate("please summarize the review", random.Random(str((1, 0))), "en")
    assert a == b


def test_mutate_varies_across_seeds():
    seen = {mutate("please summarize the review", random.Random(str((1, s))), "en") for s in range(20)}
    assert len(seen) > 1  # different seeds explore the surface space

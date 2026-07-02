from pathlib import Path

import pytest

from probe_engine.corpus.loader import CorpusError, load_corpus, load_probe_file

FIX = Path(__file__).parent / "fixtures" / "probes"


def test_load_single_good_probe():
    probe = load_probe_file(FIX / "good.yaml")
    assert probe.id == "demo-indirect-injection"
    assert probe.taxonomy_tags[1].id == "ASI01"


def test_load_bad_probe_raises_corpus_error():
    with pytest.raises(CorpusError) as exc:
        load_probe_file(FIX / "bad.yaml")
    assert "bad.yaml" in str(exc.value)


def test_load_corpus_collects_valid_probes(tmp_path):
    # only the good file present -> one probe
    (tmp_path / "a.yaml").write_text((FIX / "good.yaml").read_text())
    probes = load_corpus(tmp_path)
    assert [p.id for p in probes] == ["demo-indirect-injection"]


def test_duplicate_ids_raise(tmp_path):
    text = (FIX / "good.yaml").read_text()
    (tmp_path / "a.yaml").write_text(text)
    (tmp_path / "b.yaml").write_text(text)
    with pytest.raises(CorpusError) as exc:
        load_corpus(tmp_path)
    assert "duplicate" in str(exc.value).lower()

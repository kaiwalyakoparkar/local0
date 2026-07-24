"""Unit checks: chunker, stats counters, threshold persistence. No live services."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ingest  # noqa: E402
from app import stats, config, rag, main  # noqa: E402


def test_bm25_sparse_stable_and_nonempty():
    # Term ids must be identical across calls/processes (crc32, not salted hash),
    # or ingest-time and query-time sparse vectors won't line up.
    a = rag.bm25_sparse("Gravitee gateway routing")
    b = rag.bm25_sparse("gravitee GATEWAY routing")
    assert a.indices and a.indices == b.indices  # case-folded, same ids
    # single-char tokens dropped
    assert rag.bm25_sparse("a b c").indices == []


def test_chunk_sections_carries_heading():
    text = "# Setup\ninstall the thing\n# Usage\nrun the thing"
    secs = ingest.chunk_sections(text)
    headings = {h for h, _ in secs}
    assert "Setup" in headings and "Usage" in headings


def test_sources_dedup():
    chunks = [{"source": "a.md", "section": "S1"},
              {"source": "a.md", "section": "S1"},
              {"source": "b.md", "section": ""},
              {"source": "", "section": "x"}]
    out = main._sources(chunks)
    assert out == [{"source": "a.md", "section": "S1"}, {"source": "b.md", "section": ""}]


def test_chunk_overlap():
    words = " ".join(str(i) for i in range(1000))
    pieces = ingest.chunk(words)
    assert len(pieces) > 1
    # each chunk <= CHUNK_WORDS words
    assert all(len(p.split()) <= ingest.CHUNK_WORDS for p in pieces)
    # consecutive chunks overlap
    first_tail = pieces[0].split()[-ingest.OVERLAP_WORDS:]
    second_head = pieces[1].split()[:ingest.OVERLAP_WORDS]
    assert first_tail == second_head


def test_chunk_empty():
    assert ingest.chunk("") == []


def test_stats_counters():
    stats.reset()
    stats.record(0.9, escalated=False)
    stats.record(0.1, escalated=True)
    s = stats.snapshot()
    assert s["total"] == 2
    assert s["escalated"] == 1
    assert s["escalated_pct"] == 50.0
    stats.reset()


def test_threshold_persist(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("THRESHOLD=0.55\nGEN_MODEL=x\n")
    monkeypatch.setattr(config, "ENV_PATH", env)
    config.set_threshold(0.7)
    assert config.get_threshold() == 0.7
    assert "THRESHOLD=0.7" in env.read_text()
    assert "GEN_MODEL=x" in env.read_text()  # other keys untouched
    config.set_threshold(0.55)


def test_threshold_bounds():
    import pytest
    with pytest.raises(ValueError):
        config.set_threshold(1.5)

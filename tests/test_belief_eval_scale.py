"""Fork: the belief ledger at scale (scripts/belief_eval_scale.py) through pytest.

Runs the small preset (30 subjects, 300 beliefs) in a temp DATA_DIR. The retrieval part
runs against stand-in search modules here, so these tests don't depend on stage 2.3 being
merged. Fictional data only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "belief_eval_scale.py"
_SPEC = importlib.util.spec_from_file_location("belief_eval_scale", _PATH)
scale = importlib.util.module_from_spec(_SPEC)
sys.modules["belief_eval_scale"] = scale
_SPEC.loader.exec_module(scale)

SMALL = scale.SIZES["small"]


def _no_search():
    return None, "not merged (test)"


@pytest.fixture(scope="module")
def small_run(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scale, "load_search", _no_search)
        return scale.run_scale(tmp_path_factory.mktemp("belief-scale"), "small")


def test_every_check_passes(small_run):
    _results, checks = small_run
    failed = [f"{c.name}: expected {c.expected}, got {c.actual}" for c in checks if not c.ok]
    assert not failed, "\n".join(failed)
    names = {c.name for c in checks}
    assert "every belief ends in its planned status" in names
    assert "second sync_memory: memory.json byte-identical" in names
    assert "structure: attribute and value as planned" in names


def test_sizes_and_shares(small_run):
    results, _checks = small_run
    lg = results["ledger"]
    assert lg["beliefs"] == SMALL.beliefs == results["corpus"]["beliefs"]
    assert lg["subjects"] == SMALL.subjects + 1
    assert 0.5 <= lg["confirmed"] / lg["beliefs"] <= 0.7
    assert lg["tentative"] and lg["stale"] and lg["rejected"]
    assert lg["evidence"] > lg["beliefs"]  # two-source and rule beliefs have two
    assert results["memory"]["entries"] == lg["confirmed"]
    assert results["structure"]["structured"] == lg["confirmed"]
    from src.belief_structure import batch_size

    assert results["structure"]["batches"] == -(-lg["confirmed"] // batch_size())
    for key in ("submit_new", "submit_unchanged", "owner_actions", "sync_memory_second",
                "list_beliefs_all", "stats", "list_subjects"):
        assert key in results["timings"]


def test_retrieval_is_skipped_without_the_module(small_run):
    results, checks = small_run
    assert results["retrieval"]["status"] == "skipped"
    assert "not merged" in results["retrieval"]["reason"]
    assert not [c for c in checks if c.name.startswith("retrieval")]


def test_corpus_is_deterministic():
    a, b = scale.build_corpus(SMALL), scale.build_corpus(SMALL)
    assert a.digest() == b.digest()
    assert scale.build_corpus(SMALL, seed=8).digest() != a.digest()
    medium = scale.build_corpus(scale.SIZES["medium"])
    assert len(medium.items) == 3000 and len(medium.subjects) == 301
    assert len(medium.queries) == 200
    assert {q.kind for q in medium.queries} == {"owner", "alias", "name", "attribute"}


def test_queries_expect_only_confirmed_items():
    corpus = scale.build_corpus(SMALL)
    assert 0 < len(corpus.queries) <= SMALL.queries
    by_idx = {i.idx: i for i in corpus.items}
    for q in corpus.queries:
        assert q.expected, q.text
        assert all(by_idx[i].expected_status == "confirmed" for i in q.expected), q.text
    texts = [q.text for q in corpus.queries]
    assert len(texts) == len(set(texts))


def test_texts_are_unique_per_owner():
    from src import evidence_intake as ledger

    corpus = scale.build_corpus(scale.SIZES["medium"])
    norms = [ledger.normalize(i.shown_text, i.subject.name, i.subject.about)
             for i in corpus.items]
    assert len(norms) == len(set(norms))


def test_fake_model_maps_every_template_back():
    for t in scale.TEMPLATES:
        subject = scale.Subject(1, t.kinds[0], "Ada Quist")
        values = scale._values(t, subject, [scale.Subject(2, "person", "Bo Lindqvist")])
        value = values[0]
        attribute, got = scale.classify(t.text.format(s="Ada Quist", v=value))
        assert (attribute, got) == (t.attribute, value), t.key
    assert scale.classify("Something else entirely") == ("other", "Something else entirely")


def test_percentile():
    assert scale.percentile([], 50) == 0.0
    assert scale.percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert scale.percentile(list(map(float, range(1, 101))), 95) == 95.0


def _subject_search(owner, query, *, k=10, status=("confirmed",), subject_id=None,
                    attribute=None, kinds=None):
    """A stand-in for src.belief_search: the confirmed beliefs of the subject the query
    names (by name or alias; I/my/me: the owner)."""
    from src import evidence_intake as ledger

    q = f" {query.lower().rstrip('?')} "
    subject = None
    for s in ledger.list_subjects(owner):
        names = [s["display"], *s["aliases"]]
        if any(n.lower() in q for n in names):
            subject = s
            break
    if subject is None and any(w in q for w in (" i ", " my ", " me ")):
        subject = ledger.list_subjects(owner, kind="owner")[0]
    if subject is None:
        return []
    hits = ledger.list_beliefs(owner, status=list(status), subject_id=subject["id"])
    return [{**b, "score": 1.0, "why": "subject"} for b in hits[:k]]


def _module(search, **extra) -> types.ModuleType:
    module = types.ModuleType("src.belief_search")
    module.search_beliefs = search
    for name, value in extra.items():
        setattr(module, name, value)
    return module


def test_retrieval_runs_against_a_search_module(tmp_path, monkeypatch):
    calls = []

    def embedder():
        calls.append(1)
        return object()

    def search(owner, query, **kw):
        module.embedder()  # None in the keyword lane
        return _subject_search(owner, query, **kw)

    def search_details(owner, query, **kw):
        return {"results": search(owner, query, **kw),
                "semantic": module.embedder() is not None}

    module = _module(search, embedder=embedder, search_details=search_details)
    monkeypatch.setattr(scale, "load_search", lambda: (module, ""))
    results, checks = scale.run_scale(tmp_path, "small", min_top5=0.0)
    r = results["retrieval"]
    assert r["status"] == "ran"
    lanes = r["lanes"]
    assert set(lanes) == {"default", "keyword"}
    n = len(scale.build_corpus(SMALL).queries)
    assert len(calls) == n + 2  # the real hook: default lane only (+ search_details)
    assert lanes["keyword"]["forced_by"] == ["embedder"]
    assert lanes["default"]["semantic"] is True and lanes["keyword"]["semantic"] is False
    assert module.embedder is embedder  # restored
    for lane in lanes.values():
        assert lane["queries"] == n
        assert 0.0 <= lane["top1"] <= 1.0 and 0.0 < lane["top5"] <= 1.0
        assert lane["leaks"] == 0
        assert lane["p95_ms"] >= lane["p50_ms"] >= 0.0
        assert set(lane["top5_by_kind"]) <= {"owner", "alias", "name", "attribute"}
    assert lanes["default"]["top5_by_kind"]["name"] > 0.9  # names find their subject
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_keyword_only_uses_the_modules_own_switch():
    state = []
    module = _module(_subject_search, force_keyword=state.append)
    with scale.keyword_only(module) as changed:
        assert changed == ["force_keyword"] and state == [True]
    assert state == [True, False]
    flagged = _module(_subject_search, KEYWORD_ONLY=False)
    with scale.keyword_only(flagged):
        assert flagged.KEYWORD_ONLY is True
    assert flagged.KEYWORD_ONLY is False


def test_main_fails_below_the_threshold_and_on_leaks(tmp_path, monkeypatch, capsys):
    from src import evidence_intake as ledger

    def leaky(owner, query, **kw):  # tentative beliefs only: never the expected ones
        return ledger.list_beliefs(owner, status="tentative", limit=3)

    monkeypatch.setattr(scale, "load_search", lambda: (_module(leaky), ""))
    out = tmp_path / "out.json"
    assert scale.main(["--size", "small", "--data-dir", str(tmp_path / "d"),
                       "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "FAIL retrieval: top-5 recall >= 0.9" in text
    assert "FAIL retrieval: no hit that isn't a confirmed belief" in text
    saved = json.loads(out.read_text())
    assert saved["retrieval"]["lanes"]["default"]["top5"] == 0.0
    assert saved["retrieval"]["lanes"]["keyword"]["forced_by"] == [
        "nothing: no embedding hook found"]


def test_main_writes_results_removes_the_temp_dir_and_restores_settings(
        tmp_path, monkeypatch, capsys):
    from src import constants

    monkeypatch.setattr(scale, "load_search", _no_search)
    monkeypatch.setenv("BELIEF_STALE_DAYS", "5")  # the eval runs on the defaults
    made = []
    real = scale.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(dir=tmp_path, **kw))
        return made[-1]

    monkeypatch.setattr(scale.tempfile, "mkdtemp", mkdtemp)
    before = constants.DATA_DIR
    out = tmp_path / "results.json"
    assert scale.main(["--size", "small", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "checks: 9/9" in text and "retrieval" in text and "skipped" in text
    assert made and not Path(made[0]).exists()
    assert constants.DATA_DIR == before
    assert os.environ["BELIEF_STALE_DAYS"] == "5"
    saved = json.loads(out.read_text())
    assert saved["size"] == "small" and saved["ledger"]["beliefs"] == SMALL.beliefs
    assert all(c["ok"] for c in saved["checks"])


def test_a_used_data_dir_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(scale, "load_search", _no_search)
    out = str(tmp_path / "out.json")
    assert scale.main(["--size", "small", "--data-dir", str(tmp_path), "--out", out]) == 0
    assert scale.main(["--size", "small", "--data-dir", str(tmp_path), "--out", out]) == 2
    assert scale.main(["--size", "small", "--data-dir", str(tmp_path), "--out", out,
                       "--owner", "scale-2"]) == 0


def test_default_out_is_gitignored():
    assert scale.DEFAULT_OUT.relative_to(scale.REPO).parts[0] == "data"

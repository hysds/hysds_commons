import json

import pytest

from hysds_commons import workflow_cancel as wc


def _doc(status="running"):
    return {"run_id": "r", "status": status, "error": None, "updated_at": "old",
            "nodes": {"A": {"state": "completed", "job_id": "j"},
                      "B": {"state": "running", "payload_id": "p"},
                      "C": {"state": "pending"}}}


def test_query_shape():
    q = wc.non_terminal_jobs_query("run-1", size=10, search_after=["t", "u"])
    assert q["query"]["bool"]["filter"] == [{"term": {"workflow.run_id": "run-1"}}]
    terms = q["query"]["bool"]["must_not"][0]["terms"]["status"]
    assert set(terms) == wc.TERMINAL_JOB_STATUSES and "job-queued" not in terms
    assert q["size"] == 10 and q["search_after"] == ["t", "u"]
    assert "search_after" not in wc.non_terminal_jobs_query("r")


def test_cancel_mutate_running():
    d = wc.cancel_mutate("bob", now="NOW")(_doc())
    assert d["status"] == "cancelled" and d["error"] == "cancelled by bob"
    assert d["nodes"]["A"]["state"] == "completed"
    assert d["nodes"]["B"] == {"state": "cancelled", "payload_id": "p"}
    assert d["nodes"]["C"]["state"] == "cancelled" and d["updated_at"] == "NOW"


def test_cancel_mutate_failed_with_error():
    d = wc.cancel_mutate("reaper", "failed", "run timed out after 48h")(_doc())
    assert d["status"] == "failed" and d["error"] == "run timed out after 48h"


@pytest.mark.parametrize("st", ["completed", "failed", "cancelled"])
def test_cancel_mutate_terminal_noop(st):
    assert wc.cancel_mutate("bob")(_doc(st)) is None


def test_cancel_mutate_bad_status():
    with pytest.raises(ValueError):
        wc.cancel_mutate("u", "completed")


class _ES:
    def __init__(self, pages):
        self.pages, self.bodies = pages, []

    def search(self, index, body):
        self.bodies.append(body)
        return {"hits": {"hits": self.pages.pop(0)}}


def _hit(i):
    return {"_id": f"p{i}", "_source": {}, "sort": [i, f"u{i}"]}


def test_collect_pages():
    es = _ES([[_hit(1), _hit(2)], [_hit(3)]])
    assert wc.collect_non_terminal_job_ids(es, "js", "r", page_size=2) == ["p1", "p2", "p3"]
    assert es.bodies[1]["search_after"] == [2, "u2"]


def test_resolve_revoke_type():
    assert wc.resolve_revoke_job_type(None, "x", configured="job-lw-mozart-revoke:v1") == "job-lw-mozart-revoke:v1"
    es = _ES([[{"_id": "job-lw-mozart-revoke:develop", "_source": {"id": "job-lw-mozart-revoke:develop"}}], []])
    assert wc.resolve_revoke_job_type(es, "job_specs") == "job-lw-mozart-revoke:develop"
    with pytest.raises(LookupError):
        wc.resolve_revoke_job_type(es, "job_specs")


def test_submit_revokes_batches_and_params():
    calls = []
    n = wc.submit_revokes(lambda **kw: calls.append(kw), "job-lw-mozart-revoke:v", ["a", "b", "c"],
                          "r", username="u", batch_size=2)
    assert n == 3 and len(calls) == 2
    c = calls[0]
    assert c["queue"] == "system-jobs-queue" and c["workflow"] is None
    p = c["params"]
    assert p["component"] == "mozart" and p["operation"] == "revoke"
    assert json.loads(p["query"])["query"]["ids"]["values"] == ["a", "b"]
    assert json.loads(calls[1]["params"]["query"])["query"]["ids"]["values"] == ["c"]
    assert json.loads(p["query"]) != json.loads(calls[1]["params"]["query"])


def test_sweep_run_none():
    calls = []
    assert wc.sweep_run(_ES([[]]), "js", "r", lambda **kw: calls.append(kw), "t") == 0 and not calls

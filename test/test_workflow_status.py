"""Unit tests for hysds_commons.workflow_status using a fake client."""
import pytest

from hysds_commons.workflow_status import (
    WorkflowStatusConflict, WorkflowStatusExists, WorkflowStatusNotFound,
    create_status, get_status, update_status)


class Err(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def conflict():  # body OpenSearch returns for an ignore=409 request
    return {"status": 409, "error": {"type": "version_conflict_engine_exception"}}


class FakeES:
    def __init__(self, doc=None, conflicts=0, before_write=None):
        self.doc, self.seq, self.conflicts, self.before_write = doc, 1, conflicts, before_write
        self.writes = []

    def index(self, index, id, body, op_type=None, if_seq_no=None, if_primary_term=None, ignore=()):
        if op_type == "create":
            if self.doc is not None:
                return conflict()
            self.doc, self.seq = body, 0
            return {"result": "created"}
        if self.before_write:  # simulate a concurrent writer winning the race
            self.before_write(self)
            self.before_write = None
        if self.conflicts or if_seq_no != self.seq:
            self.conflicts = max(0, self.conflicts - 1)
            return conflict()
        assert if_primary_term == 1
        self.doc, self.seq = body, self.seq + 1
        self.writes.append(body)
        return {"result": "updated"}

    def get(self, index, id, ignore=()):
        if self.doc is None:
            return {"status": 404, "error": "not found", "found": False}
        return {"_source": dict(self.doc), "_seq_no": self.seq, "_primary_term": 1}


def test_create_conflict():
    es = FakeES()
    create_status(es, "i", "r", {"a": 1})
    with pytest.raises(WorkflowStatusExists):
        create_status(es, "i", "r", {"a": 2})
    assert es.doc == {"a": 1}


def test_get_missing_and_other_errors():
    with pytest.raises(WorkflowStatusNotFound):
        get_status(FakeES(), "i", "r")
    assert get_status(FakeES({"a": 1}), "i", "r") == ({"a": 1}, 1, 1)


def test_noop_mutate_does_not_write():
    es = FakeES({"status": "done"})
    assert update_status(es, "i", "r", lambda d: None) == {"status": "done"}
    assert es.writes == []


def test_conflict_then_success_reapplies_on_fresh_doc():
    def other_writer(e):
        e.doc, e.seq = {"n": 10}, e.seq + 1
    es = FakeES({"n": 0}, before_write=other_writer)
    seen = []

    def mutate(d):
        seen.append(d["n"])
        d["n"] += 1
        return d
    out = update_status(es, "i", "r", mutate, base_delay=0)
    assert seen == [0, 10] and out == {"n": 11} and es.doc == {"n": 11}


def test_mutate_gets_copy():
    es = FakeES({"l": [1]})
    update_status(es, "i", "r", lambda d: d["l"].append(2) or None)
    assert es.doc == {"l": [1]}


def test_attempts_exhausted():
    es = FakeES({"n": 0}, conflicts=99)
    calls = []
    with pytest.raises(WorkflowStatusConflict):
        update_status(es, "i", "r", lambda d: calls.append(1) or d, max_attempts=3, base_delay=0)
    assert len(calls) == 3


def test_non_conflict_error_propagates_and_wrapper_unwrapped():
    class Boom(FakeES):
        def index(self, **kw):
            raise Err(500)
    with pytest.raises(Err):
        update_status(Boom({"a": 1}), "i", "r", lambda d: d)

    class Wrapper:
        es = FakeES({"n": 0})
    update_status(Wrapper(), "i", "r", lambda d: {"n": 1})
    assert Wrapper.es.doc == {"n": 1}


def _run(status="running", **kw):
    return {"run_id": "r", "status": status, "updated_at": "2026-01-01T00:00:00.000Z", **kw}


@pytest.mark.parametrize("st", ["completed", "failed", "cancelled"])
def test_ended_at_set_on_terminal_transition(st):
    es = FakeES(_run())
    out = update_status(es, "i", "r", lambda d: {**d, "status": st, "updated_at": "2026-01-02T03:04:05.678Z"})
    assert out["ended_at"] == "2026-01-02T03:04:05.678Z" == es.doc["ended_at"]


def test_ended_at_not_set_for_non_terminal_write():
    es = FakeES(_run())
    out = update_status(es, "i", "r", lambda d: {**d, "current_nodes": ["A"]})
    assert "ended_at" not in out


def test_ended_at_not_overwritten_when_already_terminal():
    es = FakeES(_run("completed", ended_at="2026-01-01T00:00:01.000Z"))
    update_status(es, "i", "r", lambda d: {**d, "error": "x", "updated_at": "2026-02-02T00:00:00.000Z"})
    assert es.doc["ended_at"] == "2026-01-01T00:00:01.000Z"
    es = FakeES(_run("failed"))  # terminal without ended_at (legacy doc): not invented later
    update_status(es, "i", "r", lambda d: {**d, "error": "x"})
    assert "ended_at" not in es.doc


def test_ended_at_keeps_mutator_value_and_falls_back_to_now():
    es = FakeES(_run())
    update_status(es, "i", "r", lambda d: {**d, "status": "failed", "ended_at": "E"})
    assert es.doc["ended_at"] == "E"
    es = FakeES(_run(updated_at="T"))  # unparseable updated_at -> current time, ms precision, Z
    update_status(es, "i", "r", lambda d: {**d, "status": "completed"})
    import re
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", es.doc["ended_at"])


def test_duration_s_set_with_ended_at():
    es = FakeES(_run(submitted_at="2026-01-02T03:04:00.000Z"))
    update_status(es, "i", "r", lambda d: {**d, "status": "completed", "updated_at": "2026-01-02T03:05:30.250Z"})
    assert es.doc["duration_s"] == 90.25
    es = FakeES(_run())  # no submitted_at: no duration, no crash
    update_status(es, "i", "r", lambda d: {**d, "status": "failed"})
    assert "duration_s" not in es.doc and es.doc["ended_at"]


def test_duration_s_not_set_for_non_terminal_or_already_terminal():
    es = FakeES(_run(submitted_at="2026-01-02T03:04:00.000Z"))
    update_status(es, "i", "r", lambda d: {**d, "error": "x"})
    assert "duration_s" not in es.doc
    es = FakeES(_run("failed", submitted_at="2026-01-02T03:04:00.000Z", ended_at="2026-01-02T03:04:10.000Z"))
    update_status(es, "i", "r", lambda d: {**d, "error": "x"})
    assert "duration_s" not in es.doc


def _nrun(states, **kw):
    return _run(nodes={k: {"state": v} for k, v in states.items()}, **kw)


def test_current_nodes_follow_queued_and_running_nodes():
    es = FakeES(_nrun({"A": "running", "B": "pending", "C": "pending"}))

    def to_b(d):
        d["nodes"]["A"]["state"] = "completed"
        d["nodes"]["B"]["state"] = "queued"
        d["updated_at"] = "2026-01-01T00:00:05.000Z"
        return d
    update_status(es, "i", "r", to_b)
    assert es.doc["current_nodes"] == ["B"]


def test_current_nodes_cleared_on_terminal():
    es = FakeES(_nrun({"A": "running"}, current_nodes=["A"]))
    update_status(es, "i", "r", lambda d: {**d, "status": "failed"})
    assert es.doc["current_nodes"] == []


def test_node_started_and_ended_at():
    es = FakeES(_nrun({"A": "pending", "B": "pending"}))

    def start(d):
        d["nodes"]["A"]["state"] = "running"
        d["updated_at"] = "2026-01-01T00:00:01.000Z"
        return d
    update_status(es, "i", "r", start)
    assert es.doc["nodes"]["A"]["started_at"] == "2026-01-01T00:00:01.000Z"
    assert "ended_at" not in es.doc["nodes"]["A"] and "started_at" not in es.doc["nodes"]["B"]

    def finish(d):
        d["nodes"]["A"].update(state="completed", completed_at="2026-01-01T00:00:03.000Z")
        d["updated_at"] = "2026-01-01T00:00:04.000Z"
        return d
    update_status(es, "i", "r", finish)
    assert es.doc["nodes"]["A"]["ended_at"] == "2026-01-01T00:00:03.000Z"  # the node's own completed_at wins

    def fail(d):
        d["nodes"]["A"]["state"] = "failed"  # already ended: untouched
        d["updated_at"] = "2026-01-01T00:00:09.000Z"
        return d
    update_status(es, "i", "r", fail)
    assert es.doc["nodes"]["A"]["ended_at"] == "2026-01-01T00:00:03.000Z"


def test_node_times_keep_mutator_values_and_skip_unstarted_cancelled():
    es = FakeES(_nrun({"A": "pending", "B": "pending"}))

    def mut(d):
        d["nodes"]["A"] = {"state": "completed", "started_at": "S", "ended_at": "E"}
        d["nodes"]["B"]["state"] = "cancelled"
        d["updated_at"] = "2026-01-01T00:00:09.000Z"
        return d
    update_status(es, "i", "r", mut)
    assert es.doc["nodes"]["A"] == {"state": "completed", "started_at": "S", "ended_at": "E"}
    assert es.doc["nodes"]["B"] == {"state": "cancelled"}


def test_set_failure():
    from hysds_commons.workflow_status import set_failure
    assert set_failure({}, "A", "pge:oom") == {"failed_node": "A", "error_class": "pge:oom"}
    assert set_failure({}, None, "cancelled") == {"error_class": "cancelled"}
    assert set_failure({}, "A", None) == {"failed_node": "A", "error_class": "unknown"}

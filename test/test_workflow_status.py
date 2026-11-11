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

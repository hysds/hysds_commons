import copy
import json
import os

import pytest

from hysds_commons.workflow_run2 import (
    WorkflowRunError,
    check_run_doc,
    end_attempt,
    freeze_job_definitions,
    is_newer,
    job_types,
    mark_step_cancelled,
    mark_step_completed,
    mark_step_failed,
    mark_step_halted,
    mark_step_queued,
    mark_step_running,
    mark_step_skipped,
    new_run_doc,
    set_run_outputs,
    set_run_status,
    set_version,
    short_error,
    unsettled_steps,
    update_scatter,
    validate_run_doc,
)
from hysds_commons.workflow_spec import WorkflowSpecError

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "spec2-radiometer.json")
T = "2026-11-16T10:00:00.000000Z"


def spec():
    with open(FIXTURE) as f:
        return json.load(f)


def fetch(jt):
    return {"job_spec": {"id": jt}, "hysds_io": {"id": "hysds-io-" + jt}, "container": {"id": "c-" + jt}, "queue": "q"}


def doc():
    s = spec()
    return new_run_doc(s, "run-1", {"orbit_id": "o1"}, freeze_job_definitions(s, fetch), created_at=T)


def test_job_types_and_freeze():
    s = spec()
    jts = job_types(s)
    assert "job-radiometer-l2smp:v2.0" in jts and len(jts) == len(set(jts)) == 8
    frozen = freeze_job_definitions(s, fetch)
    assert set(frozen) == set(jts)
    assert set(frozen["job-radiometer-l1a:v3.2"]) == {"job_spec", "hysds_io", "container", "queue"}


def test_freeze_fills_missing_keys_and_copies():
    s = spec()
    src = {"job_spec": {"a": [1]}}
    frozen = freeze_job_definitions(s, lambda jt: src)
    assert frozen["job-radiometer-l1a:v3.2"]["queue"] is None
    frozen["job-radiometer-l1a:v3.2"]["job_spec"]["a"].append(2)
    assert src == {"job_spec": {"a": [1]}}


def test_new_run_doc_shape():
    d = doc()
    assert validate_run_doc(d) == []
    assert d["version"] == 1 and d["status"] == "running" and d["on_error"] == "kill"
    assert d["spec_id"] == "radiometer-orbit-l2smp" and d["spec_version"] == "1.0.0"
    assert d["spec_sha256"] == spec()["source"]["sha256"]
    assert d["inputs"] == {"orbit_id": "o1"} and d["outputs"] is None
    assert set(d["steps"]) == set(spec()["steps"])
    assert all(s["state"] == "waiting" and s["attempts"] == [] for s in d["steps"].values())
    assert d["steps"]["l1a"]["scatter"] == {"input": "l0b_file", "threshold": 0.95, "expected": None,
                                            "n_completed": 0, "n_failed": 0, "children": []}
    assert d["steps"]["reject"]["kind"] == "halt" and d["steps"]["reject"]["halt"] == {
        "status": "failed", "reason": "L2SMP failed QA", "fired": False}
    assert d["steps"]["l1b"]["job_type"] == "job-radiometer-l1b:v3.2"
    assert set(d["job_definitions"]) == set(job_types(spec()))


def test_scatter_default_threshold_is_one():
    s = spec()
    del s["steps"]["l1a"]["scatter"]["threshold"]
    d = new_run_doc(s, "r", {}, freeze_job_definitions(s, fetch), created_at=T)
    assert d["steps"]["l1a"]["scatter"]["threshold"] == 1.0


def test_builders_do_not_mutate_and_bump_version():
    d0 = doc()
    snap = copy.deepcopy(d0)
    d1 = mark_step_queued(d0, "find_l0b", "job-1", now=T)
    assert d0 == snap and d1["version"] == 2
    d2 = mark_step_running(d1, "find_l0b", now="t2")
    assert d2["version"] == 3 and d1["steps"]["find_l0b"]["state"] == "queued"


def test_attempts_lifecycle_with_retry():
    d = doc()
    d = mark_step_queued(d, "find_l0b", "job-1", now="t1")
    d = mark_step_running(d, "find_l0b", now="t2")
    d = end_attempt(d, "find_l0b", "failed", now="t3", error="x" * 2000)
    d = mark_step_queued(d, "find_l0b", "job-2", now="t4")   # retry
    d = mark_step_running(d, "find_l0b", now="t5")
    d = end_attempt(d, "find_l0b", "completed", now="t6")
    d = mark_step_completed(d, "find_l0b", now="t6")
    st = d["steps"]["find_l0b"]
    assert st["state"] == "completed" and st["job_id"] == "job-2"
    assert st["started_at"] == "t2"   # first start survives the retry
    assert [a["attempt"] for a in st["attempts"]] == [1, 2]
    assert st["attempts"][0] == {"attempt": 1, "job_id": "job-1", "status": "failed", "started_at": "t2",
                                 "ended_at": "t3", "error": st["attempts"][0]["error"]}
    assert len(st["attempts"][0]["error"]) == 512 and st["attempts"][0]["error"].endswith("...")
    assert st["attempts"][1]["status"] == "completed" and st["attempts"][1]["started_at"] == "t5"
    assert validate_run_doc(d) == []
    assert d["version"] == 1 + 7


def test_running_may_report_new_job_id():
    d = mark_step_queued(doc(), "find_l0b", "job-1", now=T)
    d = mark_step_running(d, "find_l0b", now=T, job_id="job-1b")
    assert d["steps"]["find_l0b"]["job_id"] == "job-1b" and d["steps"]["find_l0b"]["attempts"][0]["job_id"] == "job-1b"


def test_failed_step_records_error_and_class():
    d = mark_step_queued(doc(), "find_l0b", "j", now=T)
    d = end_attempt(d, "find_l0b", "failed", now="t", error="boom")
    d = mark_step_failed(d, "find_l0b", now="t", error="boom", error_class="exit_code:1")
    st = d["steps"]["find_l0b"]
    assert st["state"] == "failed" and st["error"] == "boom" and st["error_class"] == "exit_code:1"
    assert validate_run_doc(d) == []


def test_skip_cancel_halt_reasons():
    d = doc()
    d = mark_step_skipped(d, "reprocess_l2smp", "$.qa_check.reprocess", False, now="t")
    assert d["steps"]["reprocess_l2smp"]["skipped_by"] == {"flag": "$.qa_check.reprocess", "value": False}
    d = mark_step_cancelled(d, "deliver", "failure", by_step="l1a", reason="l1a failed", now="t")
    assert d["steps"]["deliver"]["cancelled_by"] == {"kind": "failure", "step": "l1a", "reason": "l1a failed"}
    d = mark_step_halted(d, "reject", now="t")
    assert d["steps"]["reject"]["state"] == "completed" and d["steps"]["reject"]["halt"]["fired"] is True
    assert validate_run_doc(d) == []


def test_settled_step_cannot_change():
    d = mark_step_skipped(doc(), "reprocess_l2smp", "$.qa_check.reprocess", False, now="t")
    for fn in (lambda: mark_step_queued(d, "reprocess_l2smp", "j"),
               lambda: mark_step_cancelled(d, "reprocess_l2smp", "user"),
               lambda: mark_step_completed(d, "reprocess_l2smp"),
               lambda: mark_step_skipped(d, "reprocess_l2smp", "f", True)):
        with pytest.raises(WorkflowRunError, match="already settled"):
            fn()


def test_bad_arguments():
    d = doc()
    with pytest.raises(WorkflowRunError, match="unknown step"):
        mark_step_queued(d, "nope", "j")
    with pytest.raises(WorkflowRunError, match="no attempt"):
        mark_step_running(d, "find_l0b")
    with pytest.raises(WorkflowRunError, match="no attempt"):
        end_attempt(d, "find_l0b", "failed")
    with pytest.raises(WorkflowRunError, match="bad attempt end status"):
        end_attempt(d, "find_l0b", "queued")
    with pytest.raises(WorkflowRunError, match="bad cancel kind"):
        mark_step_cancelled(d, "deliver", "nope")
    with pytest.raises(WorkflowRunError, match="not a halt"):
        mark_step_halted(d, "deliver")
    with pytest.raises(WorkflowRunError, match="not a scatter"):
        update_scatter(d, "deliver", expected=1)
    with pytest.raises(WorkflowRunError, match="bad run status"):
        set_run_status(d, "weird")


def test_scatter_updates():
    d = update_scatter(doc(), "l1a", expected=4, children=["c1", "c2", "c3", "c4"])
    d = update_scatter(d, "l1a", n_completed=3, n_failed=1)
    sc = d["steps"]["l1a"]["scatter"]
    assert sc == {"input": "l0b_file", "threshold": 0.95, "expected": 4, "n_completed": 3, "n_failed": 1,
                  "children": ["c1", "c2", "c3", "c4"]}
    assert validate_run_doc(d) == []


def test_run_status_outputs_and_terminal_rules():
    d = doc()
    d = set_run_status(d, "halting", now="t")
    assert d["status"] == "halting" and d["ended_at"] is None
    d = set_run_outputs(d, {"product": "p"})
    assert d["outputs"] == {"product": "p"}
    d = set_run_status(d, "failed", now="t9", error_class="step_failed", reason="l1a failed", failed_step="l1a")
    assert (d["status"], d["ended_at"], d["failed_step"], d["reason"]) == ("failed", "t9", "l1a", "l1a failed")
    with pytest.raises(WorkflowRunError, match="already failed"):
        set_run_status(d, "completed")


def test_unsettled_steps_and_terminal_validation():
    d = doc()
    assert len(unsettled_steps(d)) == len(d["steps"])
    d = set_run_status(d, "completed", now="t")
    errs = validate_run_doc(d)
    assert any("the step is waiting" in e for e in errs)


def test_set_version_and_is_newer():
    d = doc()
    d2 = set_version(d, 10)
    assert d2["version"] == 10 and is_newer(d2, d) and not is_newer(d, d2)
    nxt = mark_step_queued(d2, "find_l0b", "j", now=T)
    assert nxt["version"] == 11
    for bad in (10, 3, "11", True):
        with pytest.raises(WorkflowRunError):
            set_version(d2, bad)


def test_short_error():
    assert short_error(None) is None and short_error("a") == "a"
    assert len(short_error("x" * 600)) == 512


def test_json_serialisable():
    json.dumps(doc())


# ------------------------------------------------------------------ validator

def _m(fn):
    d = doc()
    fn(d)
    return validate_run_doc(d)


BAD = [
    ("wrong-schema-version", lambda d: d.update(schema_version="1.0"), "schema_version"),
    ("no-run-id", lambda d: d.pop("run_id"), "run_id"),
    ("version-zero", lambda d: d.update(version=0), "version"),
    ("version-str", lambda d: d.update(version="2"), "version"),
    ("bad-status", lambda d: d.update(status="done"), "status"),
    ("bad-on-error", lambda d: d.update(on_error="x"), "on_error"),
    ("inputs-list", lambda d: d.update(inputs=[]), "inputs"),
    ("outputs-list", lambda d: d.update(outputs=[]), "outputs"),
    ("terminal-no-ended", lambda d: d.update(status="completed"), "ended_at"),
    ("failed-no-class", lambda d: d.update(status="failed", ended_at="t"), "error_class"),
    ("cancelled-no-by", lambda d: d.update(status="cancelled", ended_at="t"), "cancelled_by"),
    ("no-steps", lambda d: d.update(steps={}), "steps"),
    ("defs-not-obj", lambda d: d.update(job_definitions=[]), "job_definitions"),
    ("def-missing-key", lambda d: d["job_definitions"]["job-radiometer-l1a:v3.2"].pop("container"), "missing 'container'"),
    ("job-type-unfrozen", lambda d: d["job_definitions"].pop("job-radiometer-l1a:v3.2"), "no frozen job definition"),
    ("bad-step-state", lambda d: d["steps"]["l1a"].update(state="paused"), "state"),
    ("queued-no-attempt", lambda d: d["steps"]["l1a"].update(state="queued"), "needs at least one attempt"),
    ("skipped-no-reason", lambda d: d["steps"]["l1a"].update(state="skipped", ended_at="t"), "skipped_by"),
    ("cancelled-no-reason", lambda d: d["steps"]["l1a"].update(state="cancelled", ended_at="t"), "cancelled_by"),
    ("failed-no-error", lambda d: d["steps"]["l1a"].update(state="failed", ended_at="t"), "needs error"),
    ("completed-no-ended", lambda d: d["steps"]["l1a"].update(state="completed"), "ended_at"),
    ("attempts-not-list", lambda d: d["steps"]["l1a"].update(attempts={}), "attempts"),
    ("attempt-bad-number", lambda d: d["steps"]["l1a"].update(attempts=[{"attempt": 2, "job_id": "j", "status": "queued", "started_at": None, "ended_at": None, "error": None}]), "attempt: must be 1"),
    ("attempt-no-jobid", lambda d: d["steps"]["l1a"].update(attempts=[{"attempt": 1, "status": "queued", "started_at": None, "ended_at": None, "error": None}]), "job_id"),
    ("attempt-bad-status", lambda d: d["steps"]["l1a"].update(attempts=[{"attempt": 1, "job_id": "j", "status": "x", "started_at": None, "ended_at": None, "error": None}]), "status"),
    ("attempt-missing-fields", lambda d: d["steps"]["l1a"].update(attempts=[{"attempt": 1, "job_id": "j", "status": "queued"}]), "missing 'started_at'"),
    ("halt-with-attempt", lambda d: d["steps"]["reject"].update(job_id="j"), "never submitted"),
    ("halt-fired-wrong-state", lambda d: d["steps"]["reject"]["halt"].update(fired=True), "fired but state"),
    ("halt-bad-status", lambda d: d["steps"]["reject"]["halt"].update(status="x"), "halt"),
    ("scatter-bad-threshold", lambda d: d["steps"]["l1a"]["scatter"].update(threshold=0), "threshold"),
    ("scatter-bad-count", lambda d: d["steps"]["l1a"]["scatter"].update(n_failed=-1), "n_failed"),
    ("scatter-bad-expected", lambda d: d["steps"]["l1a"]["scatter"].update(expected="4"), "expected"),
    ("scatter-children-not-list", lambda d: d["steps"]["l1a"]["scatter"].update(children="c"), "children"),
    ("step-not-object", lambda d: d["steps"].update(l1a=5), "must be an object"),
]


@pytest.mark.parametrize("name,fn,expected", BAD, ids=[b[0] for b in BAD])
def test_validate_run_doc_errors(name, fn, expected):
    errs = _m(fn)
    assert any(expected in e for e in errs), errs


def test_validate_not_dict_and_check():
    assert validate_run_doc([]) == ["run doc: must be a JSON object"]
    d = doc()
    check_run_doc(d)
    d["version"] = 0
    with pytest.raises(WorkflowSpecError):
        check_run_doc(d)

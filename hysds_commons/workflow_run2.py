"""Run document 2.0 builders and validator (PLAN §6.10, B25-B28). Pure: no Redis, no OpenSearch.

Every builder takes a run doc and returns a NEW doc (the input is never mutated) whose ``version``
is ``old + 1`` (monotonic). The engine may overwrite it with its Redis ``INCR`` value via
:func:`set_version`. Times are ISO-8601 UTC strings; ``now`` defaults to the current time.

Run doc::

    {"schema_version": "2.0", "run_id", "spec_id", "spec_version", "spec_sha256",
     "version": int, "status": running|halting|completed|failed|cancelled, "on_error",
     "inputs": {...}, "outputs": {...}|None, "created_at", "started_at", "ended_at",
     "error_class", "reason", "failed_step", "cancelled_by",
     "steps": {step_id: {"kind": "job"|"halt", "state", "job_type", "job_id", "started_at", "ended_at",
                         "attempts": [{"attempt", "job_id", "status", "started_at", "ended_at", "error"}],
                         "scatter": {"input", "threshold", "expected", "n_completed", "n_failed", "children",
                                     "phase": null|"connector"|"children", "connector": {job_type, queue}|null},
                         "skipped_by": {"flag", "value"}, "cancelled_by": {"kind", "step", "reason"},
                         "halt": {"status", "reason", "fired"}, "error", "error_class"}},
     "job_definitions": {job_type: {"job_spec", "hysds_io", "container", "queue"}}}

Step outputs and full errors are deliberately not stored (job docs hold them, §6.10).
"""

import copy
from datetime import datetime, timezone

RUN_DOC_SCHEMA_VERSION = "2.0"
RUN_STATUSES = ("running", "halting", "completed", "failed", "cancelled")
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
STEP_STATES = ("waiting", "queued", "running", "completed", "failed", "skipped", "cancelled")
TERMINAL_STEP_STATES = frozenset({"completed", "failed", "skipped", "cancelled"})
ATTEMPT_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
CANCEL_KINDS = ("failure", "halt", "user", "upstream_failed")
JOB_DEFINITION_KEYS = ("job_spec", "hysds_io", "container", "queue")
SHORT_ERROR_MAX = 512


class WorkflowRunError(Exception):
    """An invalid run-doc transition (e.g. touching a settled step) or a bad argument."""


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def short_error(error):
    """Truncate an error to the run doc's size (the full text is on the job doc)."""
    if error is None:
        return None
    s = str(error)
    return s if len(s) <= SHORT_ERROR_MAX else s[:SHORT_ERROR_MAX - 3] + "..."


# --------------------------------------------------------------------------- creation


def job_types(spec):
    """Sorted distinct job types used by the job steps of a 2.0 spec, plus the ``hysds:ScatterConnector``
    job types (frozen too: a running workflow never reads job definitions from OpenSearch)."""
    jts = {s["job_type"] for s in spec["steps"].values() if "job_type" in s}
    for s in spec["steps"].values():
        conn = (s.get("scatter") or {}).get("connector")
        if conn and conn.get("job_type"):
            jts.add(conn["job_type"])
    return sorted(jts)


def freeze_job_definitions(spec, fetch):
    """B26: ``{job_type: {"job_spec", "hysds_io", "container", "queue"}}`` using ``fetch(job_type)`` (the
    caller's lookup, which does the I/O). Missing keys are filled with ``None`` so the shape is fixed."""
    frozen = {}
    for jt in job_types(spec):
        got = fetch(jt) or {}
        frozen[jt] = {k: copy.deepcopy(got.get(k)) for k in JOB_DEFINITION_KEYS}
    return frozen


def new_run_doc(spec, run_id, inputs, job_definitions, created_at=None, spec_sha256=None):
    """Fresh run doc, ``version`` 1, status ``running``, every step ``waiting``.
    ``inputs`` are the already-resolved run inputs. ``spec_sha256`` defaults to ``spec["source"]["sha256"]``."""
    created_at = created_at or utcnow_iso()
    if spec_sha256 is None:
        spec_sha256 = (spec.get("source") or {}).get("sha256")
    steps = {}
    for sid, s in spec["steps"].items():
        halt = "halt" in s
        st = {"kind": "halt" if halt else "job", "state": "waiting", "job_type": s.get("job_type"),
              "job_id": None, "started_at": None, "ended_at": None, "attempts": []}
        if halt:
            st["halt"] = {"status": s["halt"]["status"], "reason": s["halt"].get("reason"), "fired": False}
        if "scatter" in s:
            conn = s["scatter"].get("connector")
            st["scatter"] = {"input": s["scatter"]["input"], "threshold": s["scatter"].get("threshold", 1.0),
                             "expected": None, "n_completed": 0, "n_failed": 0, "children": [],
                             "phase": None, "connector": copy.deepcopy(conn) if conn else None}
        steps[sid] = st
    return {
        "schema_version": RUN_DOC_SCHEMA_VERSION, "run_id": run_id,
        "spec_id": spec["id"], "spec_version": spec["version"], "spec_sha256": spec_sha256,
        "version": 1, "status": "running", "on_error": spec.get("on_error", "kill"),
        "inputs": copy.deepcopy(inputs or {}), "outputs": None,
        "created_at": created_at, "started_at": created_at, "ended_at": None,
        "error_class": None, "reason": None, "failed_step": None, "cancelled_by": None,
        "steps": steps, "job_definitions": copy.deepcopy(job_definitions or {}),
    }


# --------------------------------------------------------------------------- transitions


def _next(doc, mutate):
    new = copy.deepcopy(doc)
    mutate(new)
    new["version"] = doc["version"] + 1
    return new


def set_version(doc, version):
    """Copy of ``doc`` with ``version`` forced (e.g. a Redis INCR); must be greater than the current one."""
    if not isinstance(version, int) or isinstance(version, bool) or version <= doc["version"]:
        raise WorkflowRunError("version must increase: %r -> %r" % (doc["version"], version))
    new = copy.deepcopy(doc)
    new["version"] = version
    return new


def _step(doc, sid, allow_terminal=False):
    if sid not in doc["steps"]:
        raise WorkflowRunError("unknown step %r" % sid)
    st = doc["steps"][sid]
    if not allow_terminal and st["state"] in TERMINAL_STEP_STATES:
        raise WorkflowRunError("step %r is already settled (%s)" % (sid, st["state"]))
    return st


def mark_step_queued(doc, sid, job_id, now=None):
    """A job was submitted for ``sid`` (also a retry: a new attempt is appended)."""
    now = now or utcnow_iso()
    _step(doc, sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["job_id"] = "queued", job_id
        st["attempts"].append({"attempt": len(st["attempts"]) + 1, "job_id": job_id, "status": "queued",
                               "started_at": None, "ended_at": None, "error": None})
    return _next(doc, m)


def mark_step_running(doc, sid, now=None, job_id=None):
    """The job started. ``started_at`` of the step is set once (first start); the attempt's every time."""
    now = now or utcnow_iso()
    st0 = _step(doc, sid)
    if not st0["attempts"]:
        raise WorkflowRunError("step %r has no attempt to start" % sid)

    def m(d):
        st = d["steps"][sid]
        st["state"] = "running"
        if st["started_at"] is None:
            st["started_at"] = now
        att = st["attempts"][-1]
        if job_id is not None:
            att["job_id"], st["job_id"] = job_id, job_id
        att["status"], att["started_at"] = "running", now
    return _next(doc, m)


def end_attempt(doc, sid, status, now=None, error=None):
    """Close the latest attempt with ``completed`` / ``failed`` / ``cancelled``. Does not settle the
    step (a retry may follow); use :func:`mark_step_completed` / :func:`mark_step_failed` for that."""
    now = now or utcnow_iso()
    if status not in ("completed", "failed", "cancelled"):
        raise WorkflowRunError("bad attempt end status %r" % (status,))
    st0 = _step(doc, sid)
    if not st0["attempts"]:
        raise WorkflowRunError("step %r has no attempt" % sid)

    def m(d):
        att = d["steps"][sid]["attempts"][-1]
        att["status"], att["ended_at"], att["error"] = status, now, short_error(error)
    return _next(doc, m)


def mark_step_completed(doc, sid, now=None):
    now = now or utcnow_iso()
    _step(doc, sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["ended_at"] = "completed", now
    return _next(doc, m)


def mark_step_failed(doc, sid, now=None, error=None, error_class=None):
    now = now or utcnow_iso()
    _step(doc, sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["ended_at"] = "failed", now
        st["error"], st["error_class"] = short_error(error), error_class
    return _next(doc, m)


def mark_step_skipped(doc, sid, flag, value, now=None):
    """``when`` was false: ``flag`` is the ref, ``value`` the boolean it produced."""
    now = now or utcnow_iso()
    _step(doc, sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["ended_at"] = "skipped", now
        st["skipped_by"] = {"flag": flag, "value": value}
    return _next(doc, m)


def mark_step_cancelled(doc, sid, kind, by_step=None, reason=None, now=None):
    """``kind``: ``failure`` (kill/drain after a failed step), ``halt``, ``user``, ``upstream_failed`` (continue)."""
    now = now or utcnow_iso()
    if kind not in CANCEL_KINDS:
        raise WorkflowRunError("bad cancel kind %r (one of %s)" % (kind, CANCEL_KINDS))
    _step(doc, sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["ended_at"] = "cancelled", now
        st["cancelled_by"] = {"kind": kind, "step": by_step, "reason": reason}
    return _next(doc, m)


def mark_step_halted(doc, sid, now=None):
    """A halt step fired. It is never submitted; it settles ``completed`` with ``halt.fired`` true."""
    now = now or utcnow_iso()
    st0 = _step(doc, sid)
    if st0["kind"] != "halt":
        raise WorkflowRunError("step %r is not a halt step" % sid)

    def m(d):
        st = d["steps"][sid]
        st["state"], st["ended_at"] = "completed", now
        if st["started_at"] is None:
            st["started_at"] = now
        st["halt"]["fired"] = True
    return _next(doc, m)


def mark_scatter_connector(doc, sid, job_id, now=None):
    """The scatter connector job was submitted: like :func:`mark_step_queued`, and the scatter is in
    phase ``connector`` (the attempts of the step are the connector's)."""
    new = mark_step_queued(doc, sid, job_id, now=now)
    new["steps"][sid]["scatter"]["phase"] = "connector"
    return new


def mark_scatter_fanout(doc, sid, expected, now=None):
    """The item list is known (``expected`` children): the step is running, in phase ``children``. Children
    are ordinary jobs tracked in Redis, so the step has no attempt of its own (a connector attempt, if any,
    stays as the record of the lister job)."""
    now = now or utcnow_iso()
    st0 = _step(doc, sid)
    if "scatter" not in st0:
        raise WorkflowRunError("step %r is not a scatter step" % sid)

    def m(d):
        st = d["steps"][sid]
        st["state"] = "running"
        if st["started_at"] is None:
            st["started_at"] = now
        sc = st["scatter"]
        sc["expected"], sc["phase"] = expected, "children"
    return _next(doc, m)


def update_scatter(doc, sid, expected=None, n_completed=None, n_failed=None, children=None):
    """Update a scattered step's counters / child job ids (only the given fields change)."""
    st0 = _step(doc, sid, allow_terminal=True)
    if "scatter" not in st0:
        raise WorkflowRunError("step %r is not a scatter step" % sid)

    def m(d):
        sc = d["steps"][sid]["scatter"]
        if expected is not None:
            sc["expected"] = expected
        if n_completed is not None:
            sc["n_completed"] = n_completed
        if n_failed is not None:
            sc["n_failed"] = n_failed
        if children is not None:
            sc["children"] = list(children)
    return _next(doc, m)


def set_run_status(doc, status, now=None, error_class=None, reason=None, failed_step=None, cancelled_by=None):
    """Move the run to ``status``. A settled run cannot change. Terminal statuses set ``ended_at``."""
    now = now or utcnow_iso()
    if status not in RUN_STATUSES:
        raise WorkflowRunError("bad run status %r" % (status,))
    if doc["status"] in TERMINAL_RUN_STATUSES:
        raise WorkflowRunError("run is already %s" % doc["status"])

    def m(d):
        d["status"] = status
        if status in TERMINAL_RUN_STATUSES:
            d["ended_at"] = now
        for k, v in (("error_class", error_class), ("reason", reason),
                     ("failed_step", failed_step), ("cancelled_by", cancelled_by)):
            if v is not None:
                d[k] = v
    return _next(doc, m)


def set_run_outputs(doc, outputs):
    return _next(doc, lambda d: d.update(outputs=copy.deepcopy(outputs)))


def unsettled_steps(doc):
    """Step ids not yet in a terminal state."""
    return sorted(s for s, st in doc["steps"].items() if st["state"] not in TERMINAL_STEP_STATES)


# --------------------------------------------------------------------------- validator


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_run_doc(doc):
    """List of ALL problems with a run doc (empty == valid)."""
    if not isinstance(doc, dict):
        return ["run doc: must be a JSON object"]
    e = []
    if doc.get("schema_version") != RUN_DOC_SCHEMA_VERSION:
        e.append("schema_version: must be %r" % RUN_DOC_SCHEMA_VERSION)
    for k in ("run_id", "spec_id", "spec_version", "created_at"):
        if not isinstance(doc.get(k), str) or not doc.get(k):
            e.append("'%s' is required (non-empty string)" % k)
    if not _is_int(doc.get("version")) or doc["version"] < 1:
        e.append("version: must be an integer >= 1")
    if doc.get("status") not in RUN_STATUSES:
        e.append("status: must be one of %s" % (RUN_STATUSES,))
    if doc.get("on_error") not in ("kill", "drain", "continue"):
        e.append("on_error: must be kill, drain or continue")
    if not isinstance(doc.get("inputs"), dict):
        e.append("inputs: must be an object")
    if doc.get("outputs") is not None and not isinstance(doc.get("outputs"), dict):
        e.append("outputs: must be an object or null")
    if doc.get("status") in TERMINAL_RUN_STATUSES and not doc.get("ended_at"):
        e.append("ended_at: required once the run is %s" % doc.get("status"))
    if doc.get("status") == "failed" and not doc.get("error_class"):
        e.append("error_class: required when the run failed")
    if doc.get("status") == "cancelled" and not doc.get("cancelled_by"):
        e.append("cancelled_by: required when the run is cancelled")
    steps = doc.get("steps")
    if not isinstance(steps, dict) or not steps:
        e.append("steps: must be a non-empty object")
        steps = {}
    defs = doc.get("job_definitions")
    if not isinstance(defs, dict):
        e.append("job_definitions: must be an object")
        defs = {}
    for jt, d in defs.items():
        if not isinstance(d, dict):
            e.append("job_definitions.%s: must be an object" % jt)
            continue
        for k in JOB_DEFINITION_KEYS:
            if k not in d:
                e.append("job_definitions.%s: missing '%s'" % (jt, k))
    for sid, st in steps.items():
        _validate_step(sid, st, defs, e)
    if doc.get("status") in TERMINAL_RUN_STATUSES:
        for sid in unsettled_steps(doc):
            e.append("steps.%s: run is %s but the step is %s" % (sid, doc["status"], steps[sid].get("state")))
    return e


def _validate_step(sid, st, defs, e):
    w = "steps.%s" % sid
    if not isinstance(st, dict):
        e.append("%s: must be an object" % w)
        return
    state = st.get("state")
    if state not in STEP_STATES:
        e.append("%s.state: must be one of %s" % (w, (STEP_STATES,)))
    if st.get("kind") not in ("job", "halt"):
        e.append("%s.kind: must be job or halt" % w)
    if st.get("kind") == "job":
        jt = st.get("job_type")
        if not jt:
            e.append("%s.job_type: required for a job step" % w)
        elif jt not in defs:
            e.append("%s.job_type: '%s' has no frozen job definition" % (w, jt))
    atts = st.get("attempts")
    if not isinstance(atts, list):
        e.append("%s.attempts: must be a list" % w)
        atts = []
    for i, a in enumerate(atts):
        aw = "%s.attempts[%d]" % (w, i)
        if not isinstance(a, dict):
            e.append("%s: must be an object" % aw)
            continue
        if a.get("attempt") != i + 1:
            e.append("%s.attempt: must be %d" % (aw, i + 1))
        if not a.get("job_id"):
            e.append("%s.job_id: required" % aw)
        if a.get("status") not in ATTEMPT_STATUSES:
            e.append("%s.status: must be one of %s" % (aw, (ATTEMPT_STATUSES,)))
        for k in ("started_at", "ended_at", "error"):
            if k not in a:
                e.append("%s: missing '%s'" % (aw, k))
    fanned_out = isinstance(st.get("scatter"), dict) and st["scatter"].get("phase") == "children"
    if state in ("queued", "running") and not atts and not fanned_out:
        e.append("%s: state %s needs at least one attempt" % (w, state))
    if state == "skipped" and not isinstance(st.get("skipped_by"), dict):
        e.append("%s.skipped_by: required for a skipped step" % w)
    if state == "cancelled":
        cb = st.get("cancelled_by")
        if not isinstance(cb, dict) or cb.get("kind") not in CANCEL_KINDS:
            e.append("%s.cancelled_by: required with kind in %s" % (w, (CANCEL_KINDS,)))
    if state == "failed" and not st.get("error_class") and not st.get("error"):
        e.append("%s: a failed step needs error or error_class" % w)
    if state in TERMINAL_STEP_STATES and not st.get("ended_at"):
        e.append("%s.ended_at: required for state %s" % (w, state))
    if st.get("kind") == "halt":
        h = st.get("halt")
        if not isinstance(h, dict) or h.get("status") not in ("failed", "completed") or not isinstance(h.get("fired"), bool):
            e.append("%s.halt: needs status failed|completed and boolean fired" % w)
        elif h["fired"] and state != "completed":
            e.append("%s.halt: fired but state is %s" % (w, state))
        if atts or st.get("job_id"):
            e.append("%s: a halt step is never submitted (no job_id / attempts)" % w)
    sc = st.get("scatter")
    if sc is not None:
        if not isinstance(sc, dict):
            e.append("%s.scatter: must be an object" % w)
        else:
            thr = sc.get("threshold")
            if not isinstance(thr, (int, float)) or isinstance(thr, bool) or not (0 < thr <= 1):
                e.append("%s.scatter.threshold: must be in (0, 1]" % w)
            for k in ("n_completed", "n_failed"):
                if not _is_int(sc.get(k)) or sc[k] < 0:
                    e.append("%s.scatter.%s: must be an integer >= 0" % (w, k))
            if sc.get("expected") is not None and (not _is_int(sc["expected"]) or sc["expected"] < 0):
                e.append("%s.scatter.expected: must be null or an integer >= 0" % w)
            if not isinstance(sc.get("children"), list):
                e.append("%s.scatter.children: must be a list" % w)


def check_run_doc(doc):
    from hysds_commons.workflow_spec import WorkflowSpecError
    errors = validate_run_doc(doc)
    if errors:
        raise WorkflowSpecError(errors)
    return doc


def is_newer(new_doc, old_doc):
    """True when ``new_doc`` supersedes ``old_doc`` (greater ``version``); the indexer's external_gte rule."""
    return new_doc["version"] > old_doc["version"]

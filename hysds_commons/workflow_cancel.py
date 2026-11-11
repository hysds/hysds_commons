"""Shared cancel/sweep logic for workflow runs (contract 12.5, 12.6).

Used by mozart ``POST /workflow/runs/<id>/cancel`` (in-process submit) and by the
hysds stalled-run reaper (REST submit). Everything here is client-agnostic: callers
pass the raw opensearch client (see :mod:`hysds_commons.workflow_status`) and a
``submit`` callable with the :func:`hysds_commons.workflow_submit.submit_workflow_job`
signature.

Revoke job: mozart's lightweight ``job-lw-mozart-revoke:<version>`` (queue
``system-jobs-queue``) with params ``query`` (JSON string, an ``ids`` query on
job_status-current over the payload ids), ``component="mozart"``,
``operation="revoke"`` -- exactly what Figaro's "revoke" action submits. The revoke job
is deliberately NOT workflow-tagged (no events, and it must survive the run going
terminal).
"""
import json
import uuid
from datetime import datetime, timezone

TERMINAL_JOB_STATUSES = frozenset(
    {"job-completed", "job-failed", "job-deduped", "job-offline", "job-revoked"}
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
TERMINAL_NODE_STATES = frozenset({"completed", "failed", "cancelled"})

REVOKE_JOB_TYPE_PREFIX = "job-lw-mozart-revoke"
REVOKE_QUEUE = "system-jobs-queue"
DEFAULT_PAGE_SIZE = 500
DEFAULT_REVOKE_BATCH = 200


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def non_terminal_jobs_query(run_id, size=DEFAULT_PAGE_SIZE, search_after=None):
    """job_status query: this run's jobs whose status is not terminal (paged)."""
    body = {
        "size": size,
        "_source": ["uuid", "payload_id", "job_id", "status"],
        "query": {
            "bool": {
                "filter": [{"term": {"workflow.run_id": run_id}}],
                "must_not": [{"terms": {"status": sorted(TERMINAL_JOB_STATUSES)}}],
            }
        },
        "sort": [
            {"@timestamp": {"order": "asc", "unmapped_type": "date"}},
            {"uuid": {"order": "asc", "unmapped_type": "keyword"}},
        ],
    }
    if search_after is not None:
        body["search_after"] = search_after
    return body


def collect_non_terminal_job_ids(es, job_status_index, run_id, page_size=DEFAULT_PAGE_SIZE,
                                 max_pages=200):
    """All payload ids (job_status ``_id``) of the run's non-terminal jobs."""
    es = getattr(es, "es", es)
    ids, after = [], None
    for _ in range(max_pages):
        r = es.search(index=job_status_index,
                      body=non_terminal_jobs_query(run_id, page_size, after))
        hits = r.get("hits", {}).get("hits", [])
        for h in hits:
            src = h.get("_source") or {}
            pid = h.get("_id") or src.get("payload_id") or src.get("uuid")
            if pid and pid not in ids:
                ids.append(pid)
        if len(hits) < page_size or not hits[-1].get("sort"):
            break
        after = hits[-1]["sort"]
    return ids


def cancel_mutate(user, status_value="cancelled", error=None, now=None):
    """OCC mutate fn (for ``update_status``) implementing 12.5 step 1.

    A running run gets ``status=status_value`` (``cancelled``, or ``failed`` for a
    timeout), ``error`` (default ``"cancelled by <user>"``) and every non-terminal node
    ``state=cancelled``. A run that is already terminal is left alone (returns None).
    """
    if status_value not in ("cancelled", "failed"):
        raise ValueError(f"cancel status must be cancelled or failed, got {status_value!r}")
    err = error if error is not None else f"cancelled by {user}"

    def mutate(doc):
        if doc.get("status") in TERMINAL_RUN_STATUSES:
            return None
        doc["status"] = status_value
        doc["error"] = err
        for nid, node in (doc.get("nodes") or {}).items():
            if node.get("state") not in TERMINAL_NODE_STATES:
                doc["nodes"][nid] = {**node, "state": "cancelled"}
        doc["updated_at"] = now or _now()
        return doc

    return mutate


def resolve_revoke_job_type(es, job_specs_index, configured=None):
    """``job-lw-mozart-revoke:<version>``: the configured id, else the registered one."""
    if configured:
        return configured
    es = getattr(es, "es", es)
    r = es.search(index=job_specs_index, body={
        "size": 5, "_source": ["id"],
        "query": {"prefix": {"id": REVOKE_JOB_TYPE_PREFIX + ":"}}})
    ids = sorted(h.get("_id") or h["_source"]["id"] for h in r.get("hits", {}).get("hits", []))
    if not ids:
        raise LookupError(f"no {REVOKE_JOB_TYPE_PREFIX}:<version> job_spec registered in {job_specs_index}")
    return ids[-1]


def revoke_params(payload_ids):
    """Params of one revoke job over ``payload_ids`` (a unique ``_name`` keeps dedup from
    swallowing a later sweep over the same ids)."""
    query = {"query": {"ids": {"values": list(payload_ids), "_name": "wf-revoke-" + uuid.uuid4().hex[:12]}}}
    return {"query": json.dumps(query), "component": "mozart", "operation": "revoke"}


def submit_revokes(submit, job_type, payload_ids, run_id, username=None,
                   batch_size=DEFAULT_REVOKE_BATCH, queue=REVOKE_QUEUE):
    """Submit one revoke job per batch of ids; return the number of ids covered by
    successfully submitted batches. ``submit`` raises on failure (the first failure
    propagates after earlier batches were already submitted)."""
    n = 0
    ids = list(payload_ids)
    for i in range(0, len(ids), batch_size):
        batch = ids[i:i + batch_size]
        submit(job_type=job_type, queue=queue, params=revoke_params(batch), workflow=None,
               tags=["workflow-revoke", f"wf_run:{run_id}"], username=username,
               job_name=f"revoke-{run_id}")
        n += len(batch)
    return n


def sweep_run(es, job_status_index, run_id, submit, job_type, username=None,
              batch_size=DEFAULT_REVOKE_BATCH):
    """12.5 step 2: revoke every non-terminal job of the run; return the count."""
    ids = collect_non_terminal_job_ids(es, job_status_index, run_id)
    if not ids:
        return 0
    return submit_revokes(submit, job_type, ids, run_id, username=username,
                          batch_size=batch_size)

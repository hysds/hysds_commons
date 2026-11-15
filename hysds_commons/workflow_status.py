"""Optimistic-concurrency (OCC) helpers for ``workflow_status`` documents.

Both mozart (``POST /workflow/runs``, cancel) and the hysds workflow consumer write the
same ``workflow_status`` doc, so every read-modify-write goes through :func:`update_status`,
which uses OpenSearch ``if_seq_no`` / ``if_primary_term`` and retries on version conflicts.

Client: the functions take the *raw* opensearch-py client (``OpenSearchUtility.es``, i.e.
``mozart_es.es`` in mozart and ``get_mozart_es().es`` in hysds). The ``SearchUtility`` wrapper
has no seq_no-aware method, so a raw client is the common denominator; for convenience a
wrapper object (anything with an ``.es`` attribute) is also accepted and unwrapped.

409/404 handling: the connection class used by ``OpenSearchUtility`` (``jittered_backoff_class_factory``)
retries *every* exception, 409 and 404 included, for up to ~34s and re-raises them wrapped in
``JitteredBackoffException``. Raising-and-catching ConflictError would therefore stall each OCC
conflict for ~30s. So expected 409/404 responses are requested with ``ignore=`` and detected in the
returned error body, which bypasses both the retry and the wrapping. Works with plain clients too.

Refresh: no ``refresh`` is requested. GET-by-id is realtime in OpenSearch (it reads the
translog), and this module only does GETs and id-addressed writes, so forcing a refresh on
every write would only add cost. Searches over ``workflow_status`` are eventually consistent
(~1s); callers needing read-your-write via search must pass their own refresh.
"""

import copy
import random
import time
from datetime import datetime, timezone

__all__ = [
    "WorkflowStatusError",
    "WorkflowStatusExists",
    "WorkflowStatusNotFound",
    "WorkflowStatusConflict",
    "create_status",
    "get_status",
    "update_status",
]


class WorkflowStatusError(Exception):
    """Base class for workflow_status helper errors."""


class WorkflowStatusExists(WorkflowStatusError):
    """create_status: a doc with this run_id already exists."""


class WorkflowStatusNotFound(WorkflowStatusError):
    """The workflow_status doc does not exist."""


class WorkflowStatusConflict(WorkflowStatusError):
    """update_status: version conflicts persisted through every attempt."""


TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
# "skipped": a node on a switch/gate arm that was not taken. Terminal, never "current", and it has
# no started_at/ended_at (it never ran).
TERMINAL_NODE_STATES = frozenset({"completed", "failed", "cancelled", "skipped"})
STARTED_NODE_STATES = frozenset({"queued", "running"})

# Run-level ``error_class`` (top-level, with ``failed_node``): a stable keyword saying what stopped the run.
# A failed job's own class is passed through unchanged (``exit_code:<n>``, ``pge:<token>``, ``worker``,
# ``signal``, ``offline``, ``timeout`` from log_utils.set_error_class); the run-level values are:
ERROR_CLASS_TIMEOUT = "timeout"          # reaper: the run exceeded its timeout (also a job-level class)
ERROR_CLASS_THRESHOLD = "threshold"      # scatter completed below its completion threshold
ERROR_CLASS_CANCELLED = "cancelled"      # cancelled by a user (no failed_node)
ERROR_CLASS_INPUT_RESOLUTION = "input_resolution"    # a node's $ref inputs/binds could not be resolved
ERROR_CLASS_OUTPUT_RESOLUTION = "output_resolution"  # the spec outputs could not be resolved at finalize
ERROR_CLASS_SUBMIT = "submit"            # mozart could not submit the entry job
ERROR_CLASS_NO_QUEUE = "no_queue"        # no queue for a node
ERROR_CLASS_HALTED = "halted"            # a user halt node (status failed) ended the run
ERROR_CLASS_ROUTING = "routing"          # a switch/gate could not evaluate (type/operator error)
ERROR_CLASS_UNKNOWN = "unknown"          # a failed event that carried no error_class


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _valid_ts(ts):
    return _parse_ts(ts) is not None


def _parse_ts(ts):
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp_ended_at(old, new):
    """Set ``ended_at`` (and ``duration_s``) when ``new`` moves a run from non-terminal to terminal.

    Every terminal transition (finalize, halt, cancel, reaper timeout/sweep, entry-submit
    failure) goes through :func:`update_status`, so stamping here covers all of them. Uses the
    mutator's own ``updated_at`` (same format as ``submitted_at``) when it is a valid timestamp,
    else now. A run that was already terminal keeps its existing ``ended_at``. ``duration_s`` is
    ``ended_at - submitted_at`` in seconds (>= 0, 3 decimals); ``current_nodes`` is cleared.
    """
    if (new.get("status") in TERMINAL_RUN_STATUSES and old.get("status") not in TERMINAL_RUN_STATUSES):
        if not new.get("ended_at"):
            ts = new.get("updated_at")
            new["ended_at"] = ts if _valid_ts(ts) else _now()
        start, end = _parse_ts(new.get("submitted_at")), _parse_ts(new["ended_at"])
        if start and end and new.get("duration_s") is None:
            new["duration_s"] = round(max(0.0, (end - start).total_seconds()), 3)


def _stamp_nodes(old, new):
    """Per-node display times (``nodes.<id>.started_at`` / ``ended_at``) and ``current_nodes``.

    ``started_at``: the node first becomes ``queued``/``running`` (connectors that go straight to
    ``completed`` set it themselves). ``ended_at``: the node first becomes terminal (its
    ``completed_at`` when present, else the doc's ``updated_at``); a node cancelled before it
    started gets none, and a ``skipped`` node never gets one. ``current_nodes`` is the ids of the nodes now queued/running, ``[]`` once
    the run is terminal. Never touches a value a mutator already set.
    """
    nodes = new.get("nodes")
    if not isinstance(nodes, dict):
        return
    old_nodes = old.get("nodes") if isinstance(old.get("nodes"), dict) else {}
    ts = new.get("updated_at") if _valid_ts(new.get("updated_at")) else _now()
    for nid, n in nodes.items():
        if not isinstance(n, dict):
            continue
        before = (old_nodes.get(nid) or {}).get("state")
        state = n.get("state")
        if state == before:
            continue
        if state in STARTED_NODE_STATES and "started_at" not in n:
            n["started_at"] = ts
        elif (state in TERMINAL_NODE_STATES and before not in TERMINAL_NODE_STATES and "ended_at" not in n
              and state != "skipped" and (state != "cancelled" or n.get("started_at"))):
            n["ended_at"] = n.get("completed_at") if _valid_ts(n.get("completed_at")) else ts
    if new.get("status") in TERMINAL_RUN_STATUSES:
        new["current_nodes"] = []
    else:
        new["current_nodes"] = [nid for nid, n in nodes.items()
                                if isinstance(n, dict) and n.get("state") in STARTED_NODE_STATES]


def set_failure(doc, failed_node, error_class):
    """Record what stopped the run: ``failed_node`` (spec node id, omitted when None) and ``error_class``
    (see the ``ERROR_CLASS_*`` values). Mutates and returns ``doc``."""
    if failed_node:
        doc["failed_node"] = failed_node
    doc["error_class"] = error_class or ERROR_CLASS_UNKNOWN
    return doc


def _raw(client):
    return getattr(client, "es", client)


def _is_error(resp, code):
    """True if ``resp`` is the body returned for an ``ignore``d HTTP error ``code``."""
    return isinstance(resp, dict) and "error" in resp and resp.get("status") == code


def create_status(client, index, run_id, doc):
    """Create the doc (``op_type=create``); raise WorkflowStatusExists if present."""
    r = _raw(client).index(index=index, id=run_id, body=doc, op_type="create", ignore=409)
    if _is_error(r, 409):
        raise WorkflowStatusExists(f"workflow_status {run_id} already exists in {index}")
    return r


def get_status(client, index, run_id):
    """Return ``(doc, seq_no, primary_term)``; raise WorkflowStatusNotFound if absent."""
    r = _raw(client).get(index=index, id=run_id, ignore=404)
    if _is_error(r, 404) or not r.get("found", True):
        raise WorkflowStatusNotFound(f"workflow_status {run_id} not found in {index}")
    return r["_source"], r["_seq_no"], r["_primary_term"]


def update_status(client, index, run_id, mutate, max_attempts=8, base_delay=0.01, max_delay=0.25):
    """Read-modify-write ``run_id`` with optimistic concurrency.

    ``mutate(doc) -> new_doc | None`` receives a deep copy of the current doc. Returning
    ``None`` means "nothing to change": nothing is written and the current doc is returned.
    ``mutate`` MAY BE CALLED MULTIPLE TIMES (once per attempt, on a freshly read doc), so it
    must be pure/idempotent: no side effects, and decisions must derive only from its argument.

    On a 409 the doc is re-read and ``mutate`` re-applied, up to ``max_attempts`` times with
    jittered exponential backoff (full jitter, capped at ``max_delay``). Raises
    WorkflowStatusConflict when exhausted, WorkflowStatusNotFound if the doc is missing.
    Returns the written doc (or the current doc on no-op).
    """
    es = _raw(client)
    for attempt in range(1, max_attempts + 1):
        doc, seq_no, primary_term = get_status(es, index, run_id)
        new = mutate(copy.deepcopy(doc))
        if new is None:
            return doc
        _stamp_ended_at(doc, new)
        _stamp_nodes(doc, new)
        r = es.index(index=index, id=run_id, body=new, if_seq_no=seq_no,
                     if_primary_term=primary_term, ignore=409)
        if not _is_error(r, 409):
            return new
        if attempt < max_attempts:
            time.sleep(random.uniform(0, min(max_delay, base_delay * (2 ** attempt))))
    raise WorkflowStatusConflict(
        f"workflow_status {run_id}: version conflict after {max_attempts} attempts")

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
        r = es.index(index=index, id=run_id, body=new, if_seq_no=seq_no,
                     if_primary_term=primary_term, ignore=409)
        if not _is_error(r, 409):
            return new
        if attempt < max_attempts:
            time.sleep(random.uniform(0, min(max_delay, base_delay * (2 ** attempt))))
    raise WorkflowStatusConflict(
        f"workflow_status {run_id}: version conflict after {max_attempts} attempts")

"""Submission path for workflow-tagged jobs (v1 runtime contract §3, §5).

Workflow jobs are submitted **through mozart's REST API** (``POST <mozart>/job/submit``),
so they get exactly the semantics of any other HySDS job submission, plus any
policy that endpoint enforces now or later (auth, attribution, quotas). Callers:

- the workflow_rules consumer (successors and the internal _finalize/_halt connectors)
- the scatter connector (children, Phase 3), from inside its container

Mozart's own ``POST /workflow/runs`` (it *is* mozart) submits the entry job with
:func:`submit_workflow_job_inprocess`, which runs the same two calls that
``/job/submit`` makes after parsing its form.

Every workflow job is submitted with ``enable_dedup=true``. The orchestrator folds
the workflow tag (run_id, step_id, child_index) into the dedup hash.
"""

import json
import os

import requests

# connector job_spec ids (mozart/configs/workflow_connectors)
CONNECTOR_JOB_TYPES = {
    "pass": "job-workflow-pass:v1.0.0",
    "scatter": "job-workflow-scatter:v1.0.0",
    "gather": "job-workflow-gather:v1.0.0",
    "finalize": "job-workflow-finalize:v1.0.0",
    "halt": "job-workflow-halt:v1.0.0",
}

# internal step ids for runtime-materialized connectors
FINALIZE_STEP = "_finalize"
HALT_STEP = "_halt"

# env fallback for the mozart REST base URL (e.g. inside connector containers)
MOZART_REST_URL_ENV = "MOZART_REST_URL"
DEFAULT_TIMEOUT = 30


class WorkflowSubmitError(Exception):
    """Mozart rejected the submission, or it could not be reached."""


def workflow_tag(run_id, step_id, spec_id, child_index=None, child_total=None):
    return {
        "run_id": run_id,
        "step_id": step_id,
        "spec_id": spec_id,
        "child_index": child_index,
        "child_total": child_total,
    }


def _default_tags(workflow):
    return ["workflow", f"wf_run:{workflow['run_id']}"]


def submit_workflow_job(job_type, queue, params, workflow, priority=5, tags=None,
                        username=None, job_name=None, mozart_rest_url=None,
                        session=None, timeout=DEFAULT_TIMEOUT, verify=False):
    """Submit one workflow-tagged job via mozart ``POST /job/submit``; return its
    payload id (the job_status doc _id).

    ``mozart_rest_url`` is the API base, e.g. ``http://mozart:8888/api/v0.1``. It
    defaults to ``$MOZART_REST_URL``. Raises :class:`WorkflowSubmitError` on a
    transport error or ``success: false``.
    """
    base = (mozart_rest_url or os.environ.get(MOZART_REST_URL_ENV) or "").rstrip("/")
    if not base:
        raise WorkflowSubmitError(
            f"no mozart REST URL: pass mozart_rest_url or set ${MOZART_REST_URL_ENV}")
    form = {
        "type": job_type,
        "queue": queue,
        "priority": str(priority),
        "tags": json.dumps(tags if tags is not None else _default_tags(workflow)),
        "params": json.dumps(params or {}),
        "enable_dedup": "true",
        "workflow": json.dumps(workflow),
    }
    if username:
        form["username"] = username
    if job_name:
        form["name"] = job_name
    http = session or requests
    try:
        r = http.post(f"{base}/job/submit", data=form, timeout=timeout, verify=verify)
    except requests.RequestException as e:
        raise WorkflowSubmitError(f"mozart /job/submit unreachable: {e}") from e
    try:
        body = r.json()
    except ValueError:
        raise WorkflowSubmitError(
            f"mozart /job/submit returned HTTP {r.status_code}, non-JSON: {r.text[:300]}")
    if r.status_code >= 300 or not body.get("success"):
        raise WorkflowSubmitError(
            f"mozart /job/submit failed (HTTP {r.status_code}): {body.get('message')}")
    return body["result"]


def submit_workflow_job_inprocess(job_type, queue, params, workflow, priority=5, tags=None,
                                  username=None, job_name=None):
    """In-process equivalent of ``/job/submit``, for mozart itself only."""
    from hysds_commons.job_utils import resolve_hysds_job, submit_hysds_job

    job = resolve_hysds_job(
        job_type=job_type,
        queue=queue,
        priority=priority,
        tags=json.dumps(tags if tags is not None else _default_tags(workflow)),
        params=params,
        job_name=job_name,
        enable_dedup=True,
        username=username,
        workflow=workflow,
    )
    return submit_hysds_job(job)


def connector_params(run_id, step_id, spec_id, workflow_event):
    """Context params every connector job receives (contract §4)."""
    return {
        "run_id": run_id,
        "step_id": step_id,
        "spec_id": spec_id,
        "workflow_event": json.dumps(workflow_event),
    }

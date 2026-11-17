"""Submission of workflow-tagged jobs through mozart's REST API (``POST <mozart>/job/submit``).

Jobs submitted this way get exactly the semantics of any other HySDS job submission, plus any
policy that endpoint enforces (auth, attribution, quotas). Used by the reaper's revoke path.
"""

import json
import os

import requests

# env fallback for the mozart REST base URL
MOZART_REST_URL_ENV = "MOZART_REST_URL"
DEFAULT_TIMEOUT = 30


class WorkflowSubmitError(Exception):
    """Mozart rejected the submission, or it could not be reached."""


def _default_tags(workflow):
    """Job tags when the caller gives none: the step id. Run-level tags live on the run doc only."""
    return [workflow["step_id"]]


def submit_workflow_job(job_type, queue, params, workflow, priority=5, tags=None,
                        username=None, job_name=None, mozart_rest_url=None,
                        session=None, timeout=DEFAULT_TIMEOUT, verify=False, enable_dedup=True):
    """Submit one workflow-tagged job via mozart ``POST /job/submit``; return its
    payload id (the job_status doc _id).

    ``mozart_rest_url`` is the API base, e.g. ``http://mozart:8888/api/v0.1``. It
    defaults to ``$MOZART_REST_URL``. ``workflow=None`` submits an untagged job with no tags (the retry job,
    which must not emit workflow events itself) and ``enable_dedup=False`` skips dedup. Raises :class:`WorkflowSubmitError` on a
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
        "tags": json.dumps(tags if tags is not None else (_default_tags(workflow) if workflow else [])),
        "params": json.dumps(params or {}),
        "enable_dedup": "true" if enable_dedup else "false",
    }
    if workflow:
        form["workflow"] = json.dumps(workflow)
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

"""Single submission path for workflow-tagged jobs (v1 runtime contract §3, §5).

Used by mozart (entry job), the workflow_rules consumer (successors and the
internal _finalize/_halt connectors) and the scatter connector (children).
Every workflow job is submitted with ``enable_dedup=True``; the orchestrator
folds the workflow tag (run_id, step_id, child_index) into the dedup hash, so a
redelivered event that re-submits the same successor collapses onto the job
that already exists.
"""

import json

from hysds_commons.job_utils import resolve_hysds_job, submit_hysds_job

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


def workflow_tag(run_id, step_id, spec_id, child_index=None, child_total=None):
    return {
        "run_id": run_id,
        "step_id": step_id,
        "spec_id": spec_id,
        "child_index": child_index,
        "child_total": child_total,
    }


def submit_workflow_job(job_type, queue, params, workflow, priority=5, tags=None,
                        username=None, job_name=None):
    """Resolve and submit one workflow-tagged job; return its payload id
    (the job_status doc _id)."""
    if tags is None:
        tags = ["workflow", f"wf_run:{workflow['run_id']}"]
    job = resolve_hysds_job(
        job_type=job_type,
        queue=queue,
        priority=priority,
        tags=json.dumps(tags),
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

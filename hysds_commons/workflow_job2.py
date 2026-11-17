"""Build the HySDS job JSON of a workflow step from the run's FROZEN job definitions (B26).

``resolve_hysds_job`` (job_utils) reads job_specs, hysds_ios and containers from OpenSearch on every
call. A running workflow must not need OpenSearch, so the engine uses :func:`resolve_frozen_job`: the
same logic, but the three documents come from ``{"job_spec", "hysds_io", "container", "queue"}`` frozen
at run start (``workflow_run2.freeze_job_definitions``). Nothing here does I/O.

The helpers shared with ``resolve_hysds_job`` (``route_outputs``, ``get_command_line``,
``run_type_conversion``) are imported lazily from ``hysds_commons.job_utils`` (which needs the celery
config of a real HySDS process); tests may pass ``helpers=`` instead.
"""

import copy


class FrozenJobError(Exception):
    """The frozen definition is missing or unusable, or a required job param is absent."""


def _helpers():
    from hysds_commons import job_utils as ju
    return ju.route_outputs, ju.get_command_line, ju.run_type_conversion


def resolve_frozen_job(definition, job_type, queue, params, workflow=None, tags=None, priority=5,
                       username=None, job_name=None, time_limit=None, soft_time_limit=None,
                       enable_dedup=False, helpers=None):
    """The job JSON ``orchestrator.submit_job`` consumes. ``definition`` is one entry of the run doc's
    ``job_definitions``; ``params`` the step's resolved inputs (null values are dropped, so the hysds-io
    default / optional rules apply). Raises :class:`FrozenJobError`."""
    route_outputs, get_command_line, run_type_conversion = helpers or _helpers()
    if not definition or not definition.get("job_spec") or not definition.get("container"):
        raise FrozenJobError("no frozen job definition (job_spec/container) for %s" % job_type)
    specification = copy.deepcopy(definition["job_spec"])
    container_spec = copy.deepcopy(definition["container"])
    hysdsio = definition.get("hysds_io") or {}
    hysdsio_params, optional_params = {}, set()
    for p in hysdsio.get("params") or []:
        hysdsio_params[p["name"]] = p
        if p.get("optional", False) is True:
            optional_params.add(p["name"])
    params = {k: v for k, v in (params or {}).items() if v is not None}

    positional, context = [], {}
    localize_urls = specification.get("localize_urls", [])
    for param in specification.get("params", []):
        name = param["name"]
        if name not in params:
            default_value = hysdsio_params.get(name, {}).get("default", None)
            if default_value is not None:
                if isinstance(default_value, str):
                    default_value = run_type_conversion(hysdsio_params[name], default_value)
                param["value"] = default_value
            elif name in optional_params:
                continue
            else:
                raise FrozenJobError("params must specify '%s' parameter" % name)
        else:
            param["value"] = params[name]
        route_outputs(param, context, positional, localize_urls)

    cmd = get_command_line(specification.get("command", None), positional)
    if time_limit is None:
        time_limit = specification.get("time_limit", None)
    if soft_time_limit is None:
        soft_time_limit = specification.get("soft_time_limit", None)
    disk_usage = specification.get("disk_usage", None)

    job = {
        "job_type": "job:%s" % job_type,
        "job_queue": queue,
        "container_image_url": container_spec.get("url", None),
        "container_image_urls": container_spec.get("urls", None),
        "container_image_name": container_spec.get("id", None),
        "container_mappings": specification.get("imported_worker_files", {}),
        "runtime_options": specification.get("runtime_options", {}),
        "time_limit": time_limit,
        "soft_time_limit": soft_time_limit,
        "disk_usage": disk_usage,
        "enable_dedup": enable_dedup,
        "publish_overwrite_ok": False,
        "payload": {"_command": cmd, "localize_urls": localize_urls, "_disk_usage": disk_usage},
    }
    if job_name is not None:
        job["job_name"] = job_name
    if priority is not None:
        job["priority"] = priority
    if username is not None:
        job["username"] = username
    if workflow is not None:
        job["workflow"] = workflow
    if tags is not None:
        job["tags"] = list(tags)
        if tags:
            job["tag"] = tags[0]
    job["payload"]["job_specification"] = specification
    job["payload"]["container_specification"] = container_spec
    for k, v in context.items():
        if k not in job["payload"]:
            job["payload"][k] = v
    if "tag" in job and "tag" not in job["payload"]:
        job["payload"]["tag"] = job["tag"]
    return job


def submit_frozen_job(job, payload_id, task=None):
    """Send ``job`` to the orchestrator (queue ``jobs_processed``) with a caller-chosen task id, so the
    engine can record the attempt BEFORE the job can possibly report back. Returns the payload id.
    Same as ``orchestrator.do_submit_job`` plus ``task_id``."""
    if task is None:
        from hysds.orchestrator import submit_job as task
    kwargs = {"priority": job["priority"]} if "priority" in job else {}
    res = task.apply_async((job,), queue="jobs_processed", task_id=payload_id, **kwargs)
    return res.id


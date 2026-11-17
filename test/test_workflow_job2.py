import json

import pytest

from hysds_commons.workflow_job2 import FrozenJobError, resolve_frozen_job


def _route(param, context, positional, localize_urls):
    d = param.get("destination")
    if d == "positional":
        context[param["name"]] = param["value"]
        positional.append(param["value"])
    elif d == "context":
        context[param["name"]] = param.get("value", "")
    elif d == "localize":
        localize_urls.append({"url": param["value"]})


def _cmd(command, positional):
    return " ".join([command] + ["'%s'" % p for p in positional])


HELP = (_route, _cmd, lambda wire, val: val)
DEF = {"job_spec": {"command": "/run", "time_limit": 99, "params": [
        {"name": "a", "destination": "positional"}, {"name": "m", "destination": "context"},
        {"name": "o", "destination": "context"}, {"name": "u", "destination": "localize"}]},
       "hysds_io": {"params": [{"name": "m", "default": "dflt"}, {"name": "o", "optional": True}]},
       "container": {"id": "cont", "url": "u"}, "queue": "q"}


def test_builds_job_from_frozen_definition_without_io():
    snap = json.dumps(DEF)
    job = resolve_frozen_job(DEF, "job-x:v1", "qq", {"a": "1", "u": "s3://b/k", "o": None},
                             workflow={"run_id": "r", "step_id": "s"}, tags=["s"], helpers=HELP)
    assert json.dumps(DEF) == snap  # definition untouched
    assert job["job_type"] == "job:job-x:v1" and job["job_queue"] == "qq" and job["enable_dedup"] is False
    assert job["payload"]["_command"] == "/run '1'" and job["payload"]["m"] == "dflt"
    assert "o" not in job["payload"]  # optional + null: dropped
    assert job["time_limit"] == 99 and job["workflow"] == {"run_id": "r", "step_id": "s"} and job["tag"] == "s"
    assert job["container_image_name"] == "cont"
    assert resolve_frozen_job(DEF, "j", "q", {"a": "1", "u": "x"}, time_limit=5, helpers=HELP)["time_limit"] == 5


def test_missing_required_param_and_missing_definition():
    with pytest.raises(FrozenJobError, match="'a'"):
        resolve_frozen_job(DEF, "j", "q", {}, helpers=HELP)
    with pytest.raises(FrozenJobError):
        resolve_frozen_job({}, "j", "q", {}, helpers=HELP)

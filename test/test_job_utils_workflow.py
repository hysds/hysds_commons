"""Unit tests for the optional ``workflow`` tag on hysds_commons.job_utils."""
import importlib
import json
import sys
import unittest.mock as umock

import pytest

WF = {"run_id": "run-1", "step_id": "step-1", "spec_id": "spec-1", "child_index": 2, "child_total": 5}


@pytest.fixture
def ju(monkeypatch):
    """job_utils imported with hysds.orchestrator / es_util stubbed and ES mocked."""
    monkeypatch.setitem(sys.modules, "hysds.orchestrator", umock.MagicMock())
    monkeypatch.setitem(sys.modules, "hysds.es_util", umock.MagicMock())
    monkeypatch.delitem(sys.modules, "hysds_commons.job_utils", raising=False)
    mod = importlib.import_module("hysds_commons.job_utils")

    def get_by_id(index, id, **kw):
        if index == "job_specs":
            return {"_source": {"container": "c1", "command": "/bin/true", "params": []}}
        return {"_source": {"id": "c1", "url": "http://x/c1.tar.gz"}}

    mod.mozart_es = umock.MagicMock()
    mod.mozart_es.get_by_id.side_effect = get_by_id
    monkeypatch.setattr(mod, "get_hysds_io_params", lambda jt: ({}, set()))
    yield mod
    sys.modules.pop("hysds_commons.job_utils", None)


def test_resolve_without_workflow_has_no_key(ju):
    job = ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={})
    assert "workflow" not in job


def test_resolve_with_workflow_dict(ju):
    job = ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={}, workflow=WF)
    assert job["workflow"] == WF


def test_resolve_with_workflow_json_string(ju):
    job = ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={}, workflow=json.dumps(WF))
    assert job["workflow"] == WF


def test_workflow_tag_does_not_alter_rest_of_job(ju):
    a = ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={})
    b = ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={}, workflow=WF)
    b.pop("workflow")
    assert a == b


@pytest.mark.parametrize("bad", [{"run_id": "r"}, {"step_id": "s"}, [1], 5])
def test_resolve_rejects_invalid_workflow(ju, bad):
    with pytest.raises(RuntimeError):
        ju.resolve_hysds_job(job_type="job-x:v1", queue="q", params={}, workflow=bad)


def test_submit_mozart_job_passes_workflow(ju, monkeypatch):
    monkeypatch.setattr(ju, "resolve_mozart_job", lambda *a, **k: {
        "type": "job-x:v1", "queue": "q", "priority": 0, "tags": "[]", "params": "{}",
        "enable_dedup": True, "username": "u", "soft_time_limit": None, "time_limit": None,
        "disk_usage": None})
    monkeypatch.setattr(ju, "submit_hysds_job", lambda job: job)
    job = ju.submit_mozart_job({}, {}, hysdsio={}, workflow=WF)
    assert job["workflow"] == WF
    assert "workflow" not in ju.submit_mozart_job({}, {}, hysdsio={})

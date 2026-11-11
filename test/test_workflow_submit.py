import json

import pytest
import requests

from hysds_commons import workflow_submit as ws


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Session:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


WF = ws.workflow_tag("run-1", "B", "s:1")


def test_posts_job_submit_form():
    s = Session(Resp(200, {"success": True, "result": "pid-1"}))
    pid = ws.submit_workflow_job("job-x:v1", "q", {"a": [1]}, WF, mozart_rest_url="http://m/api/v0.1/", session=s)
    assert pid == "pid-1"
    url, kw = s.calls[0]
    assert url == "http://m/api/v0.1/job/submit"
    f = kw["data"]
    assert f["type"] == "job-x:v1" and f["queue"] == "q" and f["enable_dedup"] == "true"
    assert json.loads(f["params"]) == {"a": [1]}
    assert json.loads(f["workflow"]) == WF
    assert json.loads(f["tags"]) == ["workflow", "wf_run:run-1"]


def test_env_url(monkeypatch):
    monkeypatch.setenv(ws.MOZART_REST_URL_ENV, "http://env/api/v0.1")
    s = Session(Resp(200, {"success": True, "result": "p"}))
    ws.submit_workflow_job("t", "q", {}, WF, session=s)
    assert s.calls[0][0] == "http://env/api/v0.1/job/submit"


def test_no_url(monkeypatch):
    monkeypatch.delenv(ws.MOZART_REST_URL_ENV, raising=False)
    with pytest.raises(ws.WorkflowSubmitError, match="no mozart REST URL"):
        ws.submit_workflow_job("t", "q", {}, WF, session=Session())


@pytest.mark.parametrize("resp,exc,match", [
    (Resp(500, {"success": False, "message": "boom"}), None, "boom"),
    (Resp(200, {"success": False, "message": "bad type"}), None, "bad type"),
    (Resp(502, ValueError("x")), None, "non-JSON"),
    (None, requests.ConnectionError("refused"), "unreachable"),
])
def test_errors(resp, exc, match):
    with pytest.raises(ws.WorkflowSubmitError, match=match):
        ws.submit_workflow_job("t", "q", {}, WF, mozart_rest_url="http://m", session=Session(resp, exc))


# ------------------------------------------------------------------ slice 4.1: retry submission
def test_untagged_submit_without_dedup_omits_workflow_field():
    s = Session(Resp(200, {"success": True, "result": "pid"}))
    ws.submit_workflow_job("job-lw-mozart-retry:v1", ws.RETRY_QUEUE, ws.retry_params("jid", 3), None,
                           mozart_rest_url="http://m/api/v0.1", session=s, enable_dedup=False)
    f = s.calls[0][1]["data"]
    assert "workflow" not in f
    assert f["enable_dedup"] == "false"
    assert json.loads(f["tags"]) == []
    assert f["type"] == "job-lw-mozart-retry:v1" and f["queue"] == "system-jobs-queue"


def test_tagged_submit_unchanged_dedup_on_by_default():
    s = Session(Resp(200, {"success": True, "result": "pid"}))
    ws.submit_workflow_job("t", "q", {}, WF, mozart_rest_url="http://m", session=s)
    f = s.calls[0][1]["data"]
    assert f["enable_dedup"] == "true" and json.loads(f["workflow"]) == WF


def test_retry_params_shape():
    assert ws.retry_params("jid", 3) == {"retry_job_id": "jid", "retry_count_max": 3,
                                         "job_priority_increment": 0, "workflow_retry": True}
    assert ws.RETRY_JOB_TYPE == "job-lw-mozart-retry"

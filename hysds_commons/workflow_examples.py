"""Valid example workflow specs, importable by tests in any package.

Each function returns a fresh deep copy so callers may mutate it.
"""

import copy

_LINEAR = {
    "id": "demo-linear",
    "version": "1.0.0",
    "schema_version": "1.0",
    "label": "linear demo",
    "defaults": {"queue": "hello_world_worker", "timeout_hours": 48, "max_retries": 0, "retry_on": []},
    "inputs": {"greeting": {"type": "string", "default": "hi"}, "n": {"type": "int"}},
    "entry": "A",
    "nodes": {
        "A": {
            "kind": "job",
            "run": "job-wf_test:v1.0",
            "in": {"mode": "ok", "outputs": "$.inputs.greeting"},
            "out": ["count", "items"],
            "next": ["pass_1"],
        },
        "pass_1": {"kind": "pass", "bind": {"B.outputs": "$.A.items"}, "next": ["B"]},
        "B": {
            "kind": "job",
            "run": "job-wf_test:v1.0",
            "in": {"outputs": "$.pass_1.outputs"},
            "out": ["result"],
            "next": [],
        },
    },
    "outputs": {"final": "$.B.result"},
}

_SCATTER_GATHER = {
    "id": "demo-scatter",
    "version": "1.0.0",
    "schema_version": "1.0",
    "label": "scatter/gather demo",
    "inputs": {"x": {"type": "string", "default": "x0"}},
    "entry": "A",
    "nodes": {
        "A": {"kind": "job", "run": "job-wf_test:v1.0", "in": {"mode": "ok"}, "out": ["items"], "next": ["scatter_1"]},
        "scatter_1": {
            "kind": "scatter",
            "child": "P",
            "over": "$.A.items",
            "completion": {"threshold": 0.6},
            "next": ["gather_1"],
        },
        "P": {
            "kind": "job",
            "run": "job-wf_test:v1.0",
            "in": {"item": "$.scatter.item", "x": "$.inputs.x"},
            "out": ["result"],
            "next": [],
        },
        "gather_1": {"kind": "gather", "collect": "$.P.result", "next": ["pass_2"]},
        "pass_2": {"kind": "pass", "bind": {"B.files": "$.gather_1.result"}, "next": ["B"]},
        "B": {"kind": "job", "run": "job-wf_test:v1.0", "in": {"files": "$.pass_2.files"}, "out": ["done"], "next": []},
    },
    "outputs": {"final": "$.B.done"},
}


def linear_spec():
    """The linear spec from contract §1."""
    return copy.deepcopy(_LINEAR)


def scatter_gather_spec():
    """A -> scatter_1(child P, threshold 0.6) -> gather_1 -> pass_2 -> B."""
    return copy.deepcopy(_SCATTER_GATHER)

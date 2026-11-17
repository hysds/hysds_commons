import copy
import json
import os

import pytest

from hysds_commons.workflow_spec import WorkflowSpecError
from hysds_commons.workflow_spec2 import (
    check_spec2,
    compute_deps,
    dependents_index,
    load_schema,
    retry_policy2,
    spec_doc_id,
    step_queue,
    topo_order,
    validate_spec2,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "spec2-radiometer.json")


def golden():
    with open(FIXTURE) as f:
        return json.load(f)


def minimal():
    return {
        "schema_version": "2.0", "id": "m", "version": "1",
        "inputs": {"x": {"type": "string"}},
        "steps": {
            "a": {"job_type": "job-a:v1", "deps": [], "in": {"x": {"source": ["$.inputs.x"]}}, "out": ["o", "flag"]},
            "b": {"job_type": "job-b:v1", "deps": ["a"], "in": {"y": {"source": ["$.a.o"]}}, "out": ["p"]},
        },
        "outputs": {"p": {"type": "string", "source": ["$.b.p"]}},
    }


def test_golden_fixture_valid():
    assert validate_spec2(golden()) == []
    check_spec2(golden())


def test_minimal_valid_and_schema_loads():
    assert validate_spec2(minimal()) == []
    assert load_schema()["properties"]["schema_version"] == {"const": "2.0"}


def _del(path):
    def f(s):
        d = s
        for p in path[:-1]:
            d = d[p]
        del d[path[-1]]
    return f


def _set(path, v):
    def f(s):
        d = s
        for p in path[:-1]:
            d = d[p]
        d[path[-1]] = v
    return f


# (case id, base factory, mutation, substring expected in some error)
CASES = [
    ("not-a-dict", None, None, "must be a JSON object"),
    ("schema-version-1", minimal, _set(["schema_version"], "1.0"), "schema_version"),
    ("schema-version-missing", minimal, _del(["schema_version"]), "schema_version"),
    ("missing-id", minimal, _del(["id"]), "'id' is a required property"),
    ("empty-version", minimal, _set(["version"], ""), "version"),
    ("unknown-top-field", minimal, _set(["bogus"], 1), "bogus"),
    ("no-steps", minimal, _set(["steps"], {}), "steps"),
    ("bad-on-error", minimal, _set(["on_error"], "explode"), "on_error"),
    ("bad-input-type", minimal, _set(["inputs", "x", "type"], "str ing"), "inputs.x.type"),
    ("bad-step-id", minimal, lambda s: s["steps"].update({"bad id": copy.deepcopy(s["steps"]["a"])}), "bad id"),
    ("reserved-step-id", minimal, lambda s: s["steps"].update({"inputs": copy.deepcopy(s["steps"]["a"])}), "reserved"),
    ("missing-job-type", minimal, _del(["steps", "a", "job_type"]), "job_type"),
    ("missing-deps", minimal, _del(["steps", "b", "deps"]), "deps"),
    ("missing-out", minimal, _del(["steps", "a", "out"]), "'out' is a required property"),
    ("unknown-step-field", minimal, _set(["steps", "a", "next"], []), "next"),
    ("negative-retries", minimal, _set(["steps", "a", "max_retries"], -1), "max_retries"),
    ("bad-retry-on", minimal, _set(["steps", "a", "retry_on"], ["bogus"]), "retry_on"),
    ("zero-time-limit", minimal, _set(["steps", "a", "time_limit"], 0), "time_limit"),
    ("empty-queue", minimal, _set(["steps", "a", "queue"], ""), "queue"),
    ("bad-when-form", minimal, _set(["steps", "b", "when"], "$(inputs.x)"), "when"),
    ("bad-source-form", minimal, _set(["steps", "b", "in", "y", "source"], ["a.o"]), "in.y.source"),
    ("empty-source", minimal, _set(["steps", "b", "in", "y", "source"], []), "in.y.source"),
    ("bad-pick", minimal, _set(["steps", "b", "in", "y", "pick"], "last_non_null"), "in.y.pick"),
    ("bad-link-merge", minimal, _set(["steps", "b", "in", "y", "link_merge"], "zip"), "in.y.link_merge"),
    ("in-entry-empty", minimal, _set(["steps", "b", "in", "y"], {}), "in.y"),
    ("in-entry-unknown-field", minimal, _set(["steps", "b", "in", "y", "x"], 1), "in.y"),
    ("pick-without-source", minimal, _set(["steps", "b", "in", "y"], {"pick": "first_non_null", "default": 1}), "require a 'source'"),
    ("deps-missing", minimal, _set(["steps", "b", "deps"], []), "missing ['a']"),
    ("deps-extra", minimal, _set(["steps", "a", "deps"], ["b"]), "unreferenced ['b']"),
    ("deps-unknown", minimal, _set(["steps", "b", "deps"], ["a", "zz"]), "unknown step 'zz'"),
    ("deps-when-missing", minimal,
     lambda s: s["steps"].update(c={"job_type": "j:v1", "deps": [], "when": "$.a.flag", "out": []}), "missing ['a']"),
    ("when-undeclared-output", minimal, lambda s: s["steps"]["b"].update(when="$.a.nope"), "does not declare output 'nope'"),
    ("when-unknown-step", minimal, lambda s: s["steps"]["b"].update(when="$.zz.f", deps=["a", "zz"]), "unknown step 'zz'"),
    ("ref-undeclared-output", minimal, _set(["steps", "b", "in", "y", "source"], ["$.a.nope"]), "does not declare output 'nope'"),
    ("ref-unknown-step", minimal, _set(["steps", "b", "in", "y", "source"], ["$.zz.o"]), "unknown step 'zz'"),
    ("ref-undeclared-input", minimal, _set(["steps", "a", "in", "x", "source"], ["$.inputs.nope"]), "undeclared workflow input 'nope'"),
    ("self-cycle", minimal, lambda s: (s["steps"]["a"]["in"].update(z={"source": ["$.a.o"]}), s["steps"]["a"].update(deps=["a"])), "cycle"),
    ("two-cycle", minimal, lambda s: (s["steps"]["a"]["in"].update(z={"source": ["$.b.p"]}), s["steps"]["a"].update(deps=["b"])), "cycle"),
    ("scatter-input-missing", minimal, _set(["steps", "b", "scatter"], {"input": "nope"}), "scatter: input 'nope'"),
    ("scatter-threshold-zero", minimal, _set(["steps", "b", "scatter"], {"input": "y", "threshold": 0}), "threshold"),
    ("scatter-threshold-high", minimal, _set(["steps", "b", "scatter"], {"input": "y", "threshold": 1.5}), "threshold"),
    ("scatter-no-input", minimal, _set(["steps", "b", "scatter"], {"threshold": 0.5}), "'input' is a required property"),
    ("scatter-and-when", minimal, lambda s: s["steps"]["b"].update(scatter={"input": "y"}, when="$.a.flag"), "cannot be combined"),
    ("halt-with-job-type", minimal, lambda s: s["steps"].update(h={"halt": {"status": "failed"}, "deps": [], "job_type": "j:v1"}), "must not have a job_type"),
    ("halt-with-in", minimal, lambda s: s["steps"].update(h={"halt": {"status": "failed"}, "deps": ["a"], "in": {}}), "'in'"),
    ("halt-bad-status", minimal, lambda s: s["steps"].update(h={"halt": {"status": "weird"}, "deps": []}), "halt.status"),
    ("halt-no-status", minimal, lambda s: s["steps"].update(h={"halt": {}, "deps": []}), "'status' is a required property"),
    ("halt-reason-too-long", minimal, lambda s: s["steps"].update(h={"halt": {"status": "failed", "reason": "x" * 513}, "deps": []}), "halt.reason"),
    ("halt-as-dep", minimal, lambda s: (s["steps"].update(h={"halt": {"status": "failed"}, "deps": []}), s["steps"]["b"].update(deps=["a", "h"])), "halt step and cannot be depended on"),
    ("halt-as-source", minimal, lambda s: (s["steps"].update(h={"halt": {"status": "failed"}, "deps": []}), s["steps"]["b"]["in"].update(z={"source": ["$.h.o"]}), s["steps"]["b"].update(deps=["a", "h"])), "halt step 'h', which has no outputs"),
    ("output-undeclared-step-output", minimal, _set(["outputs", "p", "source"], ["$.b.nope"]), "does not declare output 'nope'"),
    ("output-unknown-step", minimal, _set(["outputs", "p", "source"], ["$.zz.p"]), "unknown step 'zz'"),
    ("output-no-source", minimal, _set(["outputs", "p"], {"type": "string"}), "'source' is a required property"),
    ("output-bad-pick", minimal, _set(["outputs", "p", "pick"], "none"), "outputs.p.pick"),
    ("output-undeclared-input", minimal, _set(["outputs", "p", "source"], ["$.inputs.q"]), "undeclared workflow input 'q'"),
    ("halt-completed-requires-optional-outputs", minimal,
     lambda s: s["steps"].update(h={"halt": {"status": "completed", "reason": "ok"}, "deps": []}), "(B21)"),
    ("source-bad-sha", minimal, _set(["source"], {"format": "cwl", "sha256": "<x>"}), "source.sha256"),
    ("source-bad-format", minimal, _set(["source"], {"format": "yaml", "sha256": "0" * 64}), "source.format"),
    ("source-path-map-nonstring", minimal, _set(["source"], {"format": "cwl", "sha256": "0" * 64, "path_map": {"a": 1}}), "source.path_map"),
]


@pytest.mark.parametrize("name,base,mutate,expected", CASES, ids=[c[0] for c in CASES])
def test_semantic_and_schema_errors(name, base, mutate, expected):
    if base is None:
        spec = ["not", "a", "dict"]
    else:
        spec = base()
        mutate(spec)
    errors = validate_spec2(spec)
    assert errors, name
    assert any(expected in e for e in errors), (expected, errors)


def test_halt_completed_with_optional_outputs_ok():
    s = minimal()
    s["steps"]["h"] = {"halt": {"status": "completed"}, "deps": []}
    s["outputs"]["p"]["type"] = "string?"
    assert validate_spec2(s) == []


def test_halt_completed_output_without_type_flagged():
    s = minimal()
    s["steps"]["h"] = {"halt": {"status": "completed"}, "deps": []}
    del s["outputs"]["p"]["type"]
    assert any("(B21)" in e for e in validate_spec2(s))


def test_halt_failed_does_not_force_optional():
    s = minimal()
    s["steps"]["h"] = {"halt": {"status": "failed"}, "deps": []}
    assert validate_spec2(s) == []


def test_entry_with_only_default_ok():
    s = minimal()
    s["steps"]["b"]["in"]["d"] = {"default": "normal"}
    assert validate_spec2(s) == []


def test_all_errors_reported_not_just_first():
    s = minimal()
    s["steps"]["b"]["deps"] = []
    s["outputs"]["p"]["source"] = ["$.b.nope"]
    s["steps"]["a"]["max_retries"] = -1
    assert len(validate_spec2(s)) >= 3


def test_check_spec2_raises_with_errors():
    s = minimal()
    s["steps"]["b"]["deps"] = []
    with pytest.raises(WorkflowSpecError) as ei:
        check_spec2(s)
    assert any("deps" in e for e in ei.value.errors)


def test_errors_are_strings_with_location():
    s = minimal()
    s["steps"]["b"]["in"]["y"]["pick"] = "x"
    errs = validate_spec2(s)
    assert all(isinstance(e, str) for e in errs)
    assert any(e.startswith("steps.b.in.y.pick:") for e in errs)


# ------------------------------------------------------------------ helpers


def test_compute_deps_matches_golden():
    g = golden()
    for sid, step in g["steps"].items():
        assert compute_deps(step) == sorted(step["deps"]), sid


def test_dependents_index_golden():
    rev = dependents_index(golden())
    assert rev["find_l0b"] == ["l1a"]
    assert rev["l1c"] == ["l2smp", "make_browse", "reprocess_l2smp"]
    assert rev["qa_check"] == ["deliver", "reject", "reprocess_l2smp"]
    assert rev["deliver"] == [] and rev["reject"] == []
    assert set(rev) == set(golden()["steps"])


def test_topo_order_respects_deps():
    g = golden()
    order = topo_order(g)
    pos = {s: i for i, s in enumerate(order)}
    for sid, step in g["steps"].items():
        for d in step["deps"]:
            assert pos[d] < pos[sid]


def test_topo_order_cycle():
    s = minimal()
    s["steps"]["a"]["deps"] = ["b"]
    with pytest.raises(ValueError):
        topo_order(s)


def test_misc_helpers():
    g = golden()
    assert spec_doc_id(g) == "radiometer-orbit-l2smp:1.0.0"
    assert step_queue(g, "l1a") == "radiometer-large"
    assert step_queue(minimal(), "a", "dflt") == "dflt"
    assert retry_policy2(g, "l1a") == {"max_retries": 2, "retry_on": ["exit_code:137", "offline"]}
    assert retry_policy2(minimal(), "a") == {"max_retries": 0, "retry_on": []}


def test_golden_scatter_and_halt_shape():
    g = golden()
    assert g["steps"]["l1a"]["scatter"] == {"input": "l0b_file", "threshold": 0.95}
    assert "job_type" not in g["steps"]["reject"]

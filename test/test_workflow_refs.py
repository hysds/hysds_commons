import pytest

from hysds_commons.workflow_examples import linear_spec, scatter_gather_spec
from hysds_commons.workflow_refs import (
    WorkflowRefError,
    build_context,
    is_ref,
    outputs_to_dict,
    parse_ref,
    resolve,
    resolve_binds,
    resolve_inputs,
)

STATUS = {
    "inputs": {"greeting": "hi", "n": 3},
    "nodes": {
        "A": {"state": "completed", "outputs": {"count": 3, "items": ["a", "b"]}},
        "pass_1": {"state": "completed", "outputs": {"outputs": ["a", "b"]}},
        "B": {"state": "running"},
    },
}


def test_is_ref_and_parse():
    assert is_ref("$.a.b") and not is_ref("a.b") and not is_ref("$a") and not is_ref(3) and not is_ref(None)
    assert parse_ref("$.inputs.x") == ("inputs", "x")
    assert parse_ref("$.A.items") == ("node", "A", "items")
    assert parse_ref("$.scatter.item") == ("scatter_item",)


@pytest.mark.parametrize("bad", ["x", "$.", "$.a", "$.a.b.c", "$..b", "$.a.", "$.scatter.other", "$.scatter", 5])
def test_parse_ref_malformed(bad):
    with pytest.raises(WorkflowRefError):
        parse_ref(bad)


def test_outputs_to_dict_slots():
    kv = [
        {"name": "s", "value_s": "x"},
        {"name": "d", "value_d": 0.5},
        {"name": "b", "value_b": False},
        {"name": "ls", "value_s": ["a", "b"]},
        {"name": "ld", "value_d": [1, 2.5]},
        {"name": "lb", "value_b": [True, False]},
    ]
    assert outputs_to_dict(kv) == {"s": "x", "d": 0.5, "b": False, "ls": ["a", "b"], "ld": [1, 2.5], "lb": [True, False]}
    assert outputs_to_dict(None) == {} and outputs_to_dict([]) == {}


@pytest.mark.parametrize("bad", [
    {"a": 1}, [1], [{"value_s": "x"}], [{"name": "a"}], [{"name": "a", "value_s": "x", "value_d": 1}],
])
def test_outputs_to_dict_bad(bad):
    with pytest.raises(WorkflowRefError):
        outputs_to_dict(bad)


def test_build_context_status_only():
    ctx = build_context(STATUS)
    assert ctx["inputs"] == {"greeting": "hi", "n": 3}
    assert ctx["nodes"]["A"]["count"] == 3
    assert "B" not in ctx["nodes"]  # no outputs yet


def test_build_context_event_overlay_precedence():
    event = {"step_id": "A", "workflow_outputs": [{"name": "count", "value_d": 99}, {"name": "extra", "value_s": "e"}]}
    ctx = build_context(STATUS, event)
    assert ctx["nodes"]["A"] == {"count": 99, "items": ["a", "b"], "extra": "e"}
    assert STATUS["nodes"]["A"]["outputs"]["count"] == 3  # input untouched


def test_build_context_event_for_node_without_status_outputs():
    event = {"step_id": "B", "workflow_outputs": [{"name": "result", "value_s": "r"}]}
    assert build_context(STATUS, event)["nodes"]["B"] == {"result": "r"}


def test_build_context_event_without_outputs():
    ctx = build_context(STATUS, {"step_id": "B", "workflow_outputs": None})
    assert "B" not in ctx["nodes"]
    assert build_context(STATUS, {"step_id": "B"})["nodes"].keys() == {"A", "pass_1"}


def test_build_context_event_document_form():
    ev = {"step_id": "B", "workflow_outputs": {"version": "1.0", "outputs": [{"name": "r", "value_s": "x"}]}}
    assert build_context(STATUS, ev)["nodes"]["B"] == {"r": "x"}


CTX = build_context(STATUS)


def test_resolve_literals():
    for v in ["plain", 3, 1.5, True, None, "$notref", ""]:
        assert resolve(v, CTX) == v


def test_resolve_refs_and_nesting():
    assert resolve("$.inputs.greeting", CTX) == "hi"
    assert resolve("$.A.items", CTX) == ["a", "b"]
    v = {"a": "$.A.count", "l": ["x", "$.inputs.n", {"d": "$.pass_1.outputs"}], "lit": 1}
    assert resolve(v, CTX) == {"a": 3, "l": ["x", 3, {"d": ["a", "b"]}], "lit": 1}


def test_resolve_returns_copy():
    r = resolve("$.A.items", CTX)
    r.append("z")
    assert CTX["nodes"]["A"]["items"] == ["a", "b"]


def test_resolve_scatter_item():
    assert resolve({"i": "$.scatter.item"}, CTX, scatter_item="f.h5") == {"i": "f.h5"}
    assert resolve("$.scatter.item", CTX, scatter_item=None) is None
    assert resolve("$.scatter.item", CTX, scatter_item=0) == 0


@pytest.mark.parametrize("ref,msg", [
    ("$.inputs.nope", "unknown run input 'nope'"),
    ("$.B.result", "node 'B' has no outputs yet"),
    ("$.ghost.x", "node 'ghost' has no outputs yet"),
    ("$.A.nope", "has no output 'nope'"),
    ("$.scatter.item", "only valid inside a scatter child"),
    ("$.a.b.c", "malformed ref"),
])
def test_resolve_errors(ref, msg):
    with pytest.raises(WorkflowRefError, match=msg):
        resolve({"k": [ref]}, CTX)


def test_resolve_inputs_job_node():
    spec = scatter_gather_spec()
    ctx = {"inputs": {"x": "X"}, "nodes": {}}
    assert resolve_inputs(spec["nodes"]["P"], ctx, scatter_item="it") == {"item": "it", "x": "X"}
    with pytest.raises(WorkflowRefError):
        resolve_inputs(spec["nodes"]["P"], ctx)
    lin = linear_spec()
    assert resolve_inputs(lin["nodes"]["A"], CTX) == {"mode": "ok", "outputs": "hi"}


def test_resolve_binds_pass_node():
    lin = linear_spec()
    assert resolve_binds(lin["nodes"]["pass_1"], CTX) == {"outputs": ["a", "b"]}
    with pytest.raises(WorkflowRefError):
        resolve_binds({"kind": "pass", "bind": {"B": "$.A.items"}}, CTX)
    with pytest.raises(WorkflowRefError, match="no output"):
        resolve_binds({"kind": "pass", "bind": {"B.p": "$.A.zip"}}, CTX)


def test_context_with_event_feeds_pass_resolution():
    lin = linear_spec()
    status = {"inputs": {}, "nodes": {"A": {"state": "running"}}}
    event = {"step_id": "A", "workflow_outputs": [{"name": "items", "value_s": ["q"]}]}
    assert resolve_binds(lin["nodes"]["pass_1"], build_context(status, event)) == {"outputs": ["q"]}

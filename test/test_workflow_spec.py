import copy

import pytest

from hysds_commons.workflow_examples import linear_spec, scatter_gather_spec
from hysds_commons.workflow_spec import (
    WorkflowSpecError,
    ancestors,
    DEFAULT_WORKFLOW_CONNECTORS,
    check_spec,
    predecessors,
    resolve_connectors,
    spec_doc_id,
    topo_order,
    validate_run_inputs,
    validate_spec,
)


def _mut(base, fn):
    s = base()
    fn(s)
    return s


def _job(**kw):
    n = {"kind": "job", "run": "job-x:v1", "in": {}, "out": ["o"], "next": []}
    n.update(kw)
    return n


# (case id, base factory, mutation, substring expected in some error)
CASES = [
    ("not-a-dict", None, None, "must be a JSON object"),
    ("missing-id", linear_spec, lambda s: s.pop("id"), "'id' is required"),
    ("empty-version", linear_spec, lambda s: s.update(version=" "), "'version' is required"),
    ("bad-schema-version", linear_spec, lambda s: s.update(schema_version="2.0"), "schema_version"),
    ("unknown-top-field", linear_spec, lambda s: s.update(bogus=1), "unknown field 'bogus'"),
    ("no-nodes", linear_spec, lambda s: s.update(nodes={}), "'nodes' is required"),
    ("bad-node-id-chars", linear_spec, lambda s: s["nodes"].update({"a.b": _job()}), "node id must match"),
    ("reserved-id-inputs", linear_spec, lambda s: s["nodes"].update({"inputs": _job()}), "reserved"),
    ("reserved-id-scatter", linear_spec, lambda s: s["nodes"].update({"scatter": _job()}), "reserved"),
    ("reserved-id-finalize", linear_spec, lambda s: s["nodes"].update({"_finalize": _job()}), "reserved"),
    ("unknown-kind", linear_spec, lambda s: s["nodes"]["A"].update(kind="wat"), "unknown or missing 'kind'"),
    ("kind-gate", linear_spec, lambda s: s["nodes"]["A"].update(kind="gate"), "not supported in v1"),
    ("kind-switch", linear_spec, lambda s: s["nodes"]["A"].update(kind="switch"), "not supported in v1"),
    ("kind-halt", linear_spec, lambda s: s["nodes"]["B"].update(kind="halt"), "not supported in v1"),
    ("job-missing-run", linear_spec, lambda s: s["nodes"]["A"].pop("run"), "'run' is required"),
    ("job-missing-out", linear_spec, lambda s: s["nodes"]["A"].pop("out"), "'out' is required"),
    ("job-unknown-field", linear_spec, lambda s: s["nodes"]["A"].update(nxt=[]), "unknown field 'nxt'"),
    ("job-bad-max-retries", linear_spec, lambda s: s["nodes"]["A"].update(max_retries=True), "max_retries"),
    ("entry-missing", linear_spec, lambda s: s.update(entry="Z"), "'entry' 'Z' is not a node"),
    ("entry-not-job", linear_spec, lambda s: s.update(entry="pass_1"), "entry node 'pass_1' must be a job"),
    ("next-target-missing", linear_spec, lambda s: s["nodes"]["A"].update(next=["nope"]), "does not exist"),
    ("next-too-many", linear_spec, lambda s: s["nodes"]["pass_1"].update(next=["B", "A"]), "at most 1"),
    ("job-to-job", linear_spec, lambda s: s["nodes"]["A"].update(next=["B"]), "must be a pass or scatter"),
    (
        "pass-to-pass",
        linear_spec,
        lambda s: (s["nodes"].update({"pass_2": {"kind": "pass", "bind": {"B.x": "$.A.count"}, "next": ["B"]}}),
                   s["nodes"]["pass_1"].update(next=["pass_2"])),
        "must be a job",
    ),
    ("pass-no-next", linear_spec, lambda s: s["nodes"]["pass_1"].update(next=[]), "exactly one next"),
    ("pass-bind-wrong-target", linear_spec, lambda s: s["nodes"]["pass_1"].update(bind={"X.outputs": "$.A.items"}), "targets 'X'"),
    ("pass-bind-literal", linear_spec, lambda s: s["nodes"]["pass_1"].update(bind={"B.outputs": "lit"}), "must be a '$.' ref"),
    (
        "fan-in",
        linear_spec,
        lambda s: (s["nodes"].update({"A2": _job(next=["pass_1"])}), s["nodes"]["A"].update(next=["pass_1"])),
        "fan-in",
    ),
    ("cycle", linear_spec, lambda s: s["nodes"]["B"].update(next=["pass_1"]), "cycle"),
    ("unreachable", linear_spec, lambda s: s["nodes"].update({"Z": _job()}), "'Z': not reachable"),
    (
        "entry-has-predecessor",
        linear_spec,
        lambda s: (s["nodes"].update({"px": {"kind": "pass", "bind": {"A.x": "$.B.result"}, "next": ["A"]}}),
                   s["nodes"]["B"].update(next=["px"])),
        "entry must have no predecessors",
    ),
    ("scatter-next-not-gather", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(next=["pass_2"]), "must be a gather"),
    ("scatter-no-next", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(next=[]), "exactly one next"),
    ("scatter-child-has-next", scatter_gather_spec, lambda s: s["nodes"]["P"].update(next=["pass_2"]), "scatter child must have next: []"),
    (
        "scatter-child-in-next",
        scatter_gather_spec,
        lambda s: (s["nodes"].update({"px": {"kind": "pass", "bind": {"P.x": "$.A.items"}, "next": ["P"]}}),
                   s["nodes"]["B"].update(next=["px"])),
        "scatter child 'P' must not appear in any next",
    ),
    ("scatter-child-missing", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(child="Q"), "child 'Q' does not exist"),
    ("scatter-child-not-job", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(child="pass_2"), "child 'pass_2' must be a job"),
    ("scatter-bad-threshold-zero", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(completion={"threshold": 0}), "(0, 1]"),
    ("scatter-bad-threshold-high", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(completion={"threshold": 1.5}), "(0, 1]"),
    ("scatter-over-missing", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].pop("over"), "'over' is required"),
    ("job-with-connector", linear_spec, lambda s: s["nodes"]["A"].update(connector="job-x:v1"), "unknown field 'connector'"),
    ("pass-empty-connector", linear_spec, lambda s: s["nodes"]["pass_1"].update(connector=""), "'connector' must be a non-empty string"),
    ("scatter-bad-connector-queue", scatter_gather_spec, lambda s: s["nodes"]["scatter_1"].update(connector_queue=3), "'connector_queue' must be a string"),
    (
        "two-scatters-same-child",
        scatter_gather_spec,
        lambda s: (s["nodes"].update({"scatter_2": {"kind": "scatter", "child": "P", "over": "$.A.items", "next": ["gather_1"]}})),
        "multiple scatters",
    ),
    ("gather-missing-collect", scatter_gather_spec, lambda s: s["nodes"]["gather_1"].pop("collect"), "'collect' is required"),
    ("gather-collect-wrong-node", scatter_gather_spec, lambda s: s["nodes"]["gather_1"].update(collect="$.A.items"), "must refer to the scatter child"),
    ("gather-collect-undeclared", scatter_gather_spec, lambda s: s["nodes"]["gather_1"].update(collect="$.P.nope"), "not declared by node 'P'"),
    ("gather-next-job", scatter_gather_spec, lambda s: s["nodes"]["gather_1"].update(next=["B"]), "must be a pass"),
    ("ref-non-ancestor", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x="$.B.result"), "not a strict ancestor"),
    ("ref-self", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x="$.A.count"), "not a strict ancestor"),
    ("ref-undeclared-output", linear_spec, lambda s: s["nodes"]["pass_1"].update(bind={"B.outputs": "$.A.nope"}), "not declared by node 'A'"),
    ("ref-unknown-node", linear_spec, lambda s: s["nodes"]["B"]["in"].update(x="$.ghost.out"), "unknown node 'ghost'"),
    ("ref-undeclared-input", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x="$.inputs.zzz"), "undeclared input 'zzz'"),
    ("ref-malformed", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x="$.a.b.c"), "malformed ref"),
    ("ref-nested-in-list", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x=[{"y": "$.inputs.zzz"}]), "in.x[0].y"),
    ("scatter-item-in-plain-job", linear_spec, lambda s: s["nodes"]["A"]["in"].update(x="$.scatter.item"), "only allowed in a scatter child"),
    ("scatter-item-in-pass", scatter_gather_spec, lambda s: s["nodes"]["pass_2"].update(bind={"B.files": "$.scatter.item"}), "only allowed in a scatter child"),
    ("scatter-item-bad-form", scatter_gather_spec, lambda s: s["nodes"]["P"]["in"].update(x="$.scatter.other"), "malformed ref"),
    ("child-refs-sibling-gather-downstream", scatter_gather_spec, lambda s: s["nodes"]["P"]["in"].update(x="$.gather_1.result"), "not a strict ancestor"),
    ("downstream-refs-child", scatter_gather_spec, lambda s: s["nodes"]["B"]["in"].update(x="$.P.result"), "not a strict ancestor"),
    ("outputs-undeclared", linear_spec, lambda s: s.update(outputs={"final": "$.B.nope"}), "not declared by node 'B'"),
    ("outputs-unknown-node", linear_spec, lambda s: s.update(outputs={"final": "$.Q.x"}), "unknown node 'Q'"),
    ("outputs-scatter-item", scatter_gather_spec, lambda s: s.update(outputs={"f": "$.scatter.item"}), "only allowed in a scatter child"),
    ("outputs-child", scatter_gather_spec, lambda s: s.update(outputs={"f": "$.P.result"}), "outputs are not stored"),
    ("outputs-literal", linear_spec, lambda s: s.update(outputs={"final": "x"}), "must be a '$.' ref"),
    ("input-bad-type", linear_spec, lambda s: s["inputs"].update(n={"type": "number"}), "'type' must be one of"),
    ("input-default-wrong-type", linear_spec, lambda s: s["inputs"].update(n={"type": "int", "default": "three"}), "default does not match type int"),
    ("input-default-bool-as-int", linear_spec, lambda s: s["inputs"].update(n={"type": "int", "default": True}), "default does not match type int"),
    ("input-default-bool-as-float", linear_spec, lambda s: s["inputs"].update(n={"type": "float", "default": False}), "default does not match type float"),
    ("input-default-array-elem", linear_spec, lambda s: s["inputs"].update(n={"type": "int[]", "default": [1, True]}), "element 1"),
    ("input-default-not-list", linear_spec, lambda s: s["inputs"].update(n={"type": "string[]", "default": "a"}), "expected a list"),
    ("input-bad-name", linear_spec, lambda s: s["inputs"].update({"bad name": {"type": "int"}}), "name must match"),
]


@pytest.mark.parametrize("case_id,base,fn,expected", CASES, ids=[c[0] for c in CASES])
def test_invalid_spec_case(case_id, base, fn, expected):
    spec = "nope" if base is None else _mut(base, fn)
    errors = validate_spec(spec)
    assert any(expected in e for e in errors), "expected %r in %s" % (expected, errors)
    with pytest.raises(WorkflowSpecError) as ei:
        check_spec(spec)
    assert ei.value.errors == errors


def test_case_count():
    assert len(CASES) >= 20


@pytest.mark.parametrize("factory", [linear_spec, scatter_gather_spec])
def test_valid_fixtures(factory):
    assert validate_spec(factory()) == []
    check_spec(factory())


def test_fixtures_are_fresh_copies():
    a = linear_spec()
    a["nodes"].clear()
    assert linear_spec()["nodes"]


def test_all_errors_reported_together():
    s = linear_spec()
    s["schema_version"] = "9"
    s["nodes"]["A"]["in"]["x"] = "$.inputs.zzz"
    s["nodes"]["pass_1"]["next"] = []
    errs = validate_spec(s)
    assert len(errs) >= 3


def test_errors_name_node_and_field():
    s = linear_spec()
    s["nodes"]["A"]["in"]["x"] = "$.inputs.zzz"
    (err,) = validate_spec(s)
    assert "node 'A'" in err and "in.x" in err


def test_dedicated_gate_error_names_node():
    s = linear_spec()
    s["nodes"]["B"]["kind"] = "gate"
    assert any("node 'B'" in e and "not supported in v1" in e for e in validate_spec(s))


def test_scatter_child_can_ref_scatter_ancestors_but_not_unrelated():
    s = scatter_gather_spec()
    assert validate_spec(s) == []
    s["nodes"]["P"]["in"]["y"] = "$.A.items"
    assert validate_spec(s) == []


def test_validate_does_not_mutate():
    s = scatter_gather_spec()
    before = copy.deepcopy(s)
    validate_spec(s)
    assert s == before


# ------------------------------------------------------------------ graph helpers


def test_spec_doc_id():
    assert spec_doc_id(linear_spec()) == "demo-linear:1.0.0"


def test_predecessors_and_ancestors_linear():
    s = linear_spec()
    assert predecessors(s) == {"A": [], "pass_1": ["A"], "B": ["pass_1"]}
    assert ancestors(s, "B") == {"A", "pass_1"}
    assert ancestors(s, "A") == set()


def test_scatter_graph():
    s = scatter_gather_spec()
    assert predecessors(s)["P"] == ["scatter_1"]
    assert ancestors(s, "P") == {"scatter_1", "A"}
    assert ancestors(s, "pass_2") == {"gather_1", "scatter_1", "A"}
    order = topo_order(s)
    assert order.index("A") < order.index("scatter_1") < order.index("P")
    assert order.index("scatter_1") < order.index("gather_1") < order.index("pass_2") < order.index("B")
    assert set(order) == set(s["nodes"])


def test_topo_order_linear_and_cycle():
    assert topo_order(linear_spec()) == ["A", "pass_1", "B"]
    s = linear_spec()
    s["nodes"]["B"]["next"] = ["pass_1"]
    with pytest.raises(WorkflowSpecError):
        topo_order(s)
    assert ancestors(s, "A") != {"A"}  # cycle-safe, terminates


# ------------------------------------------------------------------ run inputs


def test_run_inputs_defaults_and_types():
    s = linear_spec()
    assert validate_run_inputs(s, {"n": 3}) == ({"greeting": "hi", "n": 3}, [])
    assert validate_run_inputs(s, {"n": 3, "greeting": "yo"})[0]["greeting"] == "yo"
    assert validate_run_inputs(s, None)[1] == ["input 'n': required but not provided"]


def test_run_inputs_errors():
    s = linear_spec()
    _, errs = validate_run_inputs(s, {"n": "3", "extra": 1})
    assert any("'extra': unknown input" in e for e in errs)
    assert any("'n': expected int" in e for e in errs)
    assert validate_run_inputs(s, [])[1] == ["inputs: must be an object"]


def test_run_inputs_bool_is_not_int_or_float():
    s = linear_spec()
    s["inputs"]["f"] = {"type": "float", "default": 1.5}
    assert validate_run_inputs(s, {"n": True})[1]
    assert validate_run_inputs(s, {"n": 1, "f": True})[1]
    assert validate_run_inputs(s, {"n": 1, "f": 2})[1] == []  # int is an acceptable float


def test_run_inputs_all_types():
    s = linear_spec()
    s["inputs"] = {
        "a": {"type": "string"}, "b": {"type": "int"}, "c": {"type": "float"}, "d": {"type": "bool"},
        "e": {"type": "string[]"}, "f": {"type": "int[]"}, "g": {"type": "float[]"},
    }
    good = {"a": "s", "b": 1, "c": 1.5, "d": False, "e": ["x"], "f": [1, 2], "g": [1, 2.5]}
    assert validate_run_inputs(s, good) == (good, [])
    for k, bad in [("a", 1), ("b", 1.5), ("c", "1"), ("d", 0), ("e", "x"), ("f", [True]), ("g", ["1"]), ("e", [1])]:
        assert validate_run_inputs(s, dict(good, **{k: bad}))[1], (k, bad)


def test_run_inputs_default_is_copied():
    s = linear_spec()
    s["inputs"]["l"] = {"type": "string[]", "default": ["a"]}
    r, _ = validate_run_inputs(s, {"n": 1})
    r["l"].append("b")
    assert s["inputs"]["l"]["default"] == ["a"]


# ---- pluggable connectors (contract §11)

def test_over_accepts_literal_and_ref():
    for over in ("items", ["a", "b"], "$.A.items", "s3://bucket/manifest.json"):
        s = scatter_gather_spec()
        s["nodes"]["scatter_1"]["over"] = over
        assert validate_spec(s) == [], over


def test_connector_fields_accepted_on_pluggable_kinds():
    s = scatter_gather_spec()
    for nid in ("pass_2", "scatter_1", "gather_1"):
        s["nodes"][nid].update(connector="job-x:v1", connector_queue="q")
    assert validate_spec(s) == []


def test_default_connectors_shape():
    assert set(DEFAULT_WORKFLOW_CONNECTORS) == {"pass", "scatter", "gather"}
    assert DEFAULT_WORKFLOW_CONNECTORS["scatter"] == {
        "default": "job-workflow-scatter:v1.0.0",
        "allowed": {"job-workflow-scatter:v1.0.0": "workflow-connector-scatter"}}


def test_resolve_fills_defaults_and_does_not_mutate():
    s = scatter_gather_spec()
    before = copy.deepcopy(s)
    r, errs = resolve_connectors(s, DEFAULT_WORKFLOW_CONNECTORS)
    assert errs == [] and s == before and r is not s
    for nid, n in r["nodes"].items():
        if n["kind"] in ("pass", "scatter", "gather"):
            k = n["kind"]
            assert n["connector"] == "job-workflow-%s:v1.0.0" % k
            assert n["connector_queue"] == "workflow-connector-%s" % k
        else:
            assert "connector" not in n
    assert validate_spec(r) == []


def test_resolve_overwrites_author_queue_and_keeps_allowed_connector():
    cfg = copy.deepcopy(DEFAULT_WORKFLOW_CONNECTORS)
    cfg["scatter"]["allowed"]["job-mission-scatter:v2"] = "mission-q"
    s = scatter_gather_spec()
    s["nodes"]["scatter_1"].update(connector="job-mission-scatter:v2", connector_queue="evil")
    s["nodes"]["pass_2"].update(connector_queue="evil")
    r, errs = resolve_connectors(s, cfg)
    assert errs == []
    assert r["nodes"]["scatter_1"]["connector"] == "job-mission-scatter:v2"
    assert r["nodes"]["scatter_1"]["connector_queue"] == "mission-q"
    assert r["nodes"]["pass_2"]["connector_queue"] == "workflow-connector-pass"


def test_resolve_rejects_disallowed_and_names_kind_and_allowed():
    s = scatter_gather_spec()
    s["nodes"]["gather_1"]["connector"] = "job-rogue:v1"
    _, errs = resolve_connectors(s, DEFAULT_WORKFLOW_CONNECTORS)
    assert len(errs) == 1
    assert "gather_1" in errs[0] and "gather" in errs[0] and "job-workflow-gather:v1.0.0" in errs[0]
    # a pass connector is not allowed for a gather node
    s["nodes"]["gather_1"]["connector"] = "job-workflow-pass:v1.0.0"
    assert resolve_connectors(s, DEFAULT_WORKFLOW_CONNECTORS)[1]


def test_resolve_missing_kind_in_config_is_error():
    cfg = {k: v for k, v in DEFAULT_WORKFLOW_CONNECTORS.items() if k != "scatter"}
    _, errs = resolve_connectors(scatter_gather_spec(), cfg)
    assert len(errs) == 1 and "scatter_1" in errs[0] and "scatter" in errs[0]
    assert resolve_connectors(linear_spec(), {})[1]  # linear has a pass node

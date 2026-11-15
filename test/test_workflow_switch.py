"""Routing (v1.2): switch / gate / halt validation, evaluator, dominators, nodes_to_skip, skipped state."""
import copy
import glob
import json
import os

import pytest

from hysds_commons import workflow_cancel
from hysds_commons import workflow_refs as refs
from hysds_commons import workflow_status as ws
from hysds_commons.workflow_examples import linear_spec, scatter_gather_spec
from hysds_commons.workflow_refs import WorkflowRefError, WorkflowRoutingError
from hysds_commons.workflow_spec import (
    DEFAULT_WORKFLOW_CONNECTORS,
    UNSUPPORTED_KINDS,
    dominators,
    evaluate_switch,
    nodes_to_skip,
    predecessors,
    resolve_connectors,
    successors,
    switch_arms,
    validate_spec,
)

HERE = os.path.dirname(__file__)
FIXTURE = os.path.join(HERE, "fixtures", "spec-switch.json")
E2E_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "local-stack", "wf-e2e"))


def job(nxt=None, ins=None, out=("o",), **kw):
    n = {"kind": "job", "run": "job-x:v1", "in": ins or {}, "out": list(out), "next": nxt or []}
    n.update(kw)
    return n


def leaf(left, op, right):
    return {"left": left, "op": op, "right": right}


def switch_spec():
    """A -> sw -(lo)-> L -> P(pass) -> J ; sw -(default)-> J (exclusive rejoin); sw -(bad)-> H (halt)."""
    return {
        "id": "s", "version": "1", "schema_version": "1.0", "inputs": {"t": {"type": "float", "default": 1}},
        "entry": "A",
        "nodes": {
            "A": job(["sw"], out=("score",)),
            "sw": {"kind": "switch", "cases": [
                {"name": "bad", "when": leaf("$.A.score", "lt", 0.3), "next": ["H"]},
                {"name": "lo", "when": leaf("$.A.score", "lt", "$.inputs.t"), "next": ["L"]}],
                "default": ["J"]},
            "H": {"kind": "halt", "status": "failed", "reason": "bad"},
            "L": job(["P"], out=("lo",)),
            "P": {"kind": "pass", "bind": {"J.m": "$.L.lo"}, "next": ["J"]},
            "J": job([], ins={"a": "$.A.score", "r": "$.sw.arm"}),
        },
        "outputs": {"arm": "$.sw.arm", "lo": "$.L.lo"},
    }


def mut(fn, base=switch_spec):
    s = base()
    fn(s)
    return s


def errs(spec):
    return validate_spec(spec)


# ------------------------------------------------------------------ valid shapes


def test_switch_spec_valid():
    assert errs(switch_spec()) == []


def test_fixture_valid_and_grammar():
    spec = json.load(open(FIXTURE))
    assert errs(spec) == []
    assert switch_arms(spec["nodes"]["tier"]) == [
        ("gold", "archive_gold"), ("silver", "archive_silver"), ("default", "archive_other")]


def test_unsupported_kinds_is_empty_tuple():
    assert UNSUPPORTED_KINDS == ()


@pytest.mark.skipif(not os.path.isdir(E2E_DIR), reason="local-stack not checked out")
def test_all_local_stack_e2e_specs_still_valid():
    files = sorted(glob.glob(os.path.join(E2E_DIR, "*.json")))
    assert files
    for f in files:
        d = json.load(open(f))
        if isinstance(d, dict) and "nodes" in d:
            assert errs(d) == [], f


def test_existing_v1_specs_still_valid():
    assert errs(linear_spec()) == [] and errs(scatter_gather_spec()) == []


# ------------------------------------------------------------------ schema negatives

SCHEMA_CASES = [
    ("both-cases-and-on", lambda s: s["nodes"]["sw"].update(on="$.A.score", choices={"x": ["J"]}), "exactly one of 'cases'"),
    ("neither", lambda s: s["nodes"]["sw"].pop("cases"), "exactly one of 'cases'"),
    ("on-without-choices", lambda s: (s["nodes"]["sw"].pop("cases"), s["nodes"]["sw"].update(on="$.A.score")), "exactly one of 'cases'"),
    ("empty-cases", lambda s: s["nodes"]["sw"].update(cases=[]), "'cases' must be a non-empty list"),
    ("no-default", lambda s: s["nodes"]["sw"].pop("default"), "'default' is required"),
    ("default-two-ids", lambda s: s["nodes"]["sw"].update(default=["J", "H"]), "exactly one node id"),
    ("default-not-list", lambda s: s["nodes"]["sw"].update(default="J"), "exactly one node id"),
    ("arm-empty", lambda s: s["nodes"]["sw"]["cases"][0].update(next=[]), "exactly one node id"),
    ("case-missing-when", lambda s: s["nodes"]["sw"]["cases"][0].pop("when"), "cases[0].when is required"),
    ("case-missing-next", lambda s: s["nodes"]["sw"]["cases"][0].pop("next"), "cases[0].next is required"),
    ("case-unknown-field", lambda s: s["nodes"]["sw"]["cases"][0].update(x=1), "cases[0] has unknown field 'x'"),
    ("case-bad-name", lambda s: s["nodes"]["sw"]["cases"][0].update(name="has space"), "name must be a string matching"),
    ("case-dup-name", lambda s: s["nodes"]["sw"]["cases"][1].update(name="bad"), "duplicate arm names"),
    ("case-default-name-collides", lambda s: (s["nodes"]["sw"]["cases"][1].pop("name"), s["nodes"]["sw"]["cases"][0].update(name="case_1")), "duplicate arm names"),
    ("case-name-default-reserved", lambda s: s["nodes"]["sw"]["cases"][0].update(name="default"), "reserved"),
    ("switch-unknown-field", lambda s: s["nodes"]["sw"].update(next=["J"]), "unknown field 'next' for kind 'switch'"),
    ("switch-tag-not-allowed", lambda s: s["nodes"]["sw"].update(tag="t"), "unknown field 'tag' for kind 'switch'"),
    ("when-bad-op", lambda s: s["nodes"]["sw"]["cases"][0]["when"].update(op="like"), ".op must be one of"),
    ("when-missing-op", lambda s: s["nodes"]["sw"]["cases"][0]["when"].pop("op"), "is missing 'op'"),
    ("when-missing-right", lambda s: s["nodes"]["sw"]["cases"][0]["when"].pop("right"), "is missing 'right'"),
    ("when-extra-key", lambda s: s["nodes"]["sw"]["cases"][0]["when"].update(foo=1), "unknown field 'foo'"),
    ("when-not-object", lambda s: s["nodes"]["sw"]["cases"][0].update(when="a<b"), "must be an object"),
    ("when-empty-all", lambda s: s["nodes"]["sw"]["cases"][0].update(when={"all": []}), "non-empty list"),
    ("when-empty-any", lambda s: s["nodes"]["sw"]["cases"][0].update(when={"any": []}), "non-empty list"),
    ("when-all-and-any", lambda s: s["nodes"]["sw"]["cases"][0].update(when={"all": [leaf(1, "eq", 1)], "any": [leaf(1, "eq", 1)]}), "exactly one of all/any/not"),
    ("when-in-needs-list", lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf("$.A.score", "in", 3)), "'in' needs a list"),
    ("when-ref-undeclared-input", lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf("$.inputs.zz", "eq", 1)), "undeclared input 'zz'"),
    ("when-ref-malformed", lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf("$.a.b.c", "eq", 1)), "malformed ref"),
    ("when-scatter-item", lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf("$.scatter.item", "eq", 1)), "only allowed in a scatter child"),
    ("when-ref-undeclared-output", lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf("$.A.nope", "eq", 1)), "not declared by node 'A'"),
    ("arm-target-missing", lambda s: s["nodes"]["sw"].update(default=["ghost"]), "target 'ghost' does not exist"),
    ("arm-target-gather", lambda s: s["nodes"]["sw"].update(default=["G"]) or s["nodes"].update(
        G={"kind": "gather", "collect": "$.L.lo", "next": []}), "must be a job or pass or scatter or switch or gate or halt"),
    ("halt-bad-status", lambda s: s["nodes"]["H"].update(status="boom"), "'status' must be one of"),
    ("halt-no-reason", lambda s: s["nodes"]["H"].pop("reason"), "'reason' is required"),
    ("halt-empty-reason", lambda s: s["nodes"]["H"].update(reason=""), "'reason' is required"),
    ("halt-long-reason", lambda s: s["nodes"]["H"].update(reason="x" * 513), "'reason' is required"),
    ("halt-with-next", lambda s: s["nodes"]["H"].update(next=[]), "unknown field 'next' for kind 'halt'"),
    ("halt-with-connector", lambda s: s["nodes"]["H"].update(connector="c"), "unknown field 'connector' for kind 'halt'"),
    ("halt-as-entry", lambda s: s.update(entry="H"), "entry node 'H' must be a job"),
    ("switch-as-entry", lambda s: s.update(entry="sw"), "entry node 'sw' must be a job"),
    ("job-to-job-still-banned", lambda s: s["nodes"]["A"].update(next=["J"]), "must be a pass or scatter or switch or gate or halt"),
    ("pass-to-switch-banned", lambda s: s["nodes"]["P"].update(next=["sw"]), "must be a job node"),
    ("scatter-child-in-arm", lambda s: (s["nodes"].update(
        SC={"kind": "scatter", "child": "C", "over": "$.inputs.t", "next": ["GA"]}, C=job(), GA={"kind": "gather", "collect": "$.C.o", "next": []}),
        s["nodes"]["sw"].update(default=["C"])), "scatter child 'C' must not appear in any next"),
    ("connector-empty", lambda s: s["nodes"]["sw"].update(connector=""), "'connector' must be a non-empty string"),
    ("unreachable-arm-node", lambda s: s["nodes"].update(Z=job()), "'Z': not reachable"),
    ("cycle-through-arm", lambda s: s["nodes"]["L"].update(next=["P"]) or s["nodes"]["P"].update(next=["J"]) or s["nodes"]["J"].update(next=["sw"]), "cycle"),
    ("duplicate-choice-label-default", lambda s: (s["nodes"]["sw"].pop("cases"), s["nodes"]["sw"].update(
        on="$.A.score", choices={"default": ["J"]})), "reserved"),
    ("choice-bad-key", lambda s: (s["nodes"]["sw"].pop("cases"), s["nodes"]["sw"].update(
        on="$.A.score", choices={"a b": ["J"]})), "choices key 'a b' must match"),
    ("on-literal", lambda s: (s["nodes"]["sw"].pop("cases"), s["nodes"]["sw"].update(
        on="lit", choices={"a": ["J"]})), "'on' must be a '$.' ref"),
    ("choices-empty", lambda s: (s["nodes"]["sw"].pop("cases"), s["nodes"]["sw"].update(on="$.A.score", choices={})), "non-empty object"),
]


@pytest.mark.parametrize("cid,fn,expected", SCHEMA_CASES, ids=[c[0] for c in SCHEMA_CASES])
def test_switch_negative(cid, fn, expected):
    e = errs(mut(fn))
    assert any(expected in x for x in e), (expected, e)


def test_when_depth_cap_8_ok_9_rejected():
    def nest(n):
        t = leaf(1, "eq", 1)
        for _ in range(n - 1):
            t = {"not": t}
        return t
    assert errs(mut(lambda s: s["nodes"]["sw"]["cases"][0].update(when=nest(8)))) == []
    e = errs(mut(lambda s: s["nodes"]["sw"]["cases"][0].update(when=nest(9))))
    assert any("deeper than 8" in x for x in e)


def test_when_tree_forms_valid():
    tree = {"any": [{"not": leaf("$.A.score", "eq", 1)}, {"all": [leaf("$.A.score", "in", [1, 2]), leaf("a", "ne", "b")]}]}
    assert errs(mut(lambda s: s["nodes"]["sw"]["cases"][0].update(when=tree))) == []


def test_in_with_ref_right_valid():
    assert errs(mut(lambda s: s["nodes"]["sw"]["cases"][0].update(when=leaf(1, "in", "$.A.score")))) == []


def test_choices_switch_valid_and_errors():
    def f(s):
        s["nodes"]["sw"].pop("cases")
        s["nodes"]["sw"].update(on="$.A.score", choices={"1": ["L"], "2": ["H"]})
    assert errs(mut(f)) == []


# gate
def gate_spec():
    s = switch_spec()
    s["nodes"]["sw"] = {"kind": "gate", "when": leaf("$.A.score", "ge", 0.5), "on_true": ["J"], "on_false": ["H"]}
    del s["nodes"]["L"], s["nodes"]["P"]
    s["outputs"] = {"arm": "$.sw.arm"}
    return s


def test_gate_valid():
    assert errs(gate_spec()) == []
    assert switch_arms(gate_spec()["nodes"]["sw"]) == [("true", "J"), ("false", "H")]


GATE_CASES = [
    ("no-when", lambda s: s["nodes"]["sw"].pop("when"), "'when' is required"),
    ("no-on-true", lambda s: s["nodes"]["sw"].pop("on_true"), "'on_true' is required"),
    ("no-on-false", lambda s: s["nodes"]["sw"].pop("on_false"), "'on_false' is required"),
    ("two-targets", lambda s: s["nodes"]["sw"].update(on_true=["J", "H"]), "exactly one node id"),
    ("cases-not-allowed", lambda s: s["nodes"]["sw"].update(cases=[]), "unknown field 'cases'"),
    ("default-not-allowed", lambda s: s["nodes"]["sw"].update(default=["J"]), "unknown field 'default'"),
    ("bad-op", lambda s: s["nodes"]["sw"]["when"].update(op="zz"), ".op must be one of"),
    ("missing-target", lambda s: s["nodes"]["sw"].update(on_false=["nope"]), "arm 'false' target 'nope' does not exist"),
]


@pytest.mark.parametrize("cid,fn,expected", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_gate_negative(cid, fn, expected):
    assert any(expected in x for x in errs(mut(fn, gate_spec)))


# ------------------------------------------------------------------ grammar


@pytest.mark.parametrize("target_kind", ["job", "pass", "scatter", "switch", "gate", "halt"])
def test_arm_may_target_each_allowed_kind(target_kind):
    s = {
        "id": "g", "version": "1", "schema_version": "1.0", "inputs": {"x": {"type": "int", "default": 1}},
        "entry": "A",
        "nodes": {"A": job(["sw"], out=("v",)),
                  "sw": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["T"], "on_false": ["H"]},
                  "H": {"kind": "halt", "status": "completed", "reason": "r"}},
    }
    t = {
        "job": job(), "pass": {"kind": "pass", "bind": {"B.m": "$.A.v"}, "next": ["B"]},
        "scatter": {"kind": "scatter", "child": "C", "over": "$.A.v", "next": ["GA"]},
        "switch": {"kind": "switch", "cases": [{"when": leaf(1, "eq", 1), "next": ["H"]}], "default": ["H"]},
        "gate": {"kind": "gate", "when": leaf(1, "eq", 1), "on_true": ["H"], "on_false": ["H"]},
        "halt": {"kind": "halt", "status": "failed", "reason": "r"},
    }[target_kind]
    s["nodes"]["T"] = t
    if target_kind == "pass":
        s["nodes"]["B"] = job()
    if target_kind == "scatter":
        s["nodes"]["C"] = job()
        s["nodes"]["GA"] = {"kind": "gather", "collect": "$.C.o", "next": []}
    assert errs(s) == []


@pytest.mark.parametrize("src_next", ["switch", "gate", "halt"])
def test_job_and_gather_may_target_routing_kinds(src_next):
    s = {"id": "g", "version": "1", "schema_version": "1.0", "entry": "A", "nodes": {"A": job(["T"], out=("v",))}}
    s["nodes"]["T"] = {
        "switch": {"kind": "switch", "cases": [{"when": leaf("$.A.v", "eq", 1), "next": ["H"]}], "default": ["H"]},
        "gate": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["H"], "on_false": ["H"]},
        "halt": {"kind": "halt", "status": "failed", "reason": "r"},
    }[src_next]
    if src_next != "halt":
        s["nodes"]["H"] = {"kind": "halt", "status": "failed", "reason": "r"}
    assert errs(s) == []


def test_gather_may_target_switch_and_halt():
    s = scatter_gather_spec()
    s["nodes"]["gather_1"]["next"] = ["H"]
    s["nodes"]["H"] = {"kind": "halt", "status": "completed", "reason": "r"}
    del s["nodes"]["pass_2"], s["nodes"]["B"]
    s["outputs"] = {}
    assert errs(s) == []


def test_pass_scatter_still_restricted():
    s = switch_spec()
    s["nodes"]["P"]["next"] = ["H"]
    assert any("must be a job node" in e for e in errs(s))


# ------------------------------------------------------------------ rejoin (D1)


def test_exclusive_rejoin_valid():
    assert errs(switch_spec()) == []
    assert sorted(predecessors(switch_spec())["J"]) == ["P", "sw"]


def test_plain_fanin_rejected_message():
    s = linear_spec()
    s["nodes"]["A2"] = job(["pass_1"])
    s["nodes"]["A"]["next"] = ["pass_1"]
    e = errs(s)
    assert any("node 'pass_1': fan-in is only allowed where the incoming edges come from different arms of one switch; "
               "has predecessors ['A', 'A2']" in x for x in e)


def test_rejoin_from_two_arms_directly():
    s = switch_spec()
    s["nodes"]["sw"]["cases"][0]["next"] = ["J"]  # the "bad" case now also targets J
    del s["nodes"]["H"]
    # edges sw->J (default and case 0 collapse to one edge), P->J: arm targets J and L: distinct
    assert errs(s) == []


def test_two_incoming_from_same_arm_rejected():
    s = switch_spec()
    # L -> P -> J and L2 chained in the same arm also joining J
    s["nodes"]["L"]["next"] = ["P2"]
    s["nodes"]["P2"] = {"kind": "pass", "bind": {"X.m": "$.L.lo"}, "next": ["X"]}
    s["nodes"]["X"] = job(["P"], out=("o",))
    s["nodes"]["P"]["next"] = ["J"]
    s["nodes"]["X"]["next"] = ["P3"]
    s["nodes"]["P3"] = {"kind": "pass", "bind": {"J.q": "$.X.o"}, "next": ["J"]}
    e = errs(s)
    assert any("node 'J': fan-in is only allowed" in x for x in e)


def test_fanin_without_any_switch_dominator_rejected():
    s = switch_spec()
    s["nodes"]["A2"] = job(["J"])
    e = errs(s)
    assert any("node 'J': fan-in is only allowed" in x for x in e)


def test_rejoin_with_nested_switch_arms():
    def p(nxt):
        return {"kind": "pass", "bind": {}, "next": [nxt]}
    s = {
        "id": "n", "version": "1", "schema_version": "1.0", "entry": "A",
        "nodes": {
            "A": job(["s1"], out=("v",)),
            "s1": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["s2"], "on_false": ["X"]},
            "s2": {"kind": "gate", "when": leaf("$.A.v", "eq", 2), "on_true": ["Y"], "on_false": ["Z"]},
            "X": job(["pX"]), "Y": job(["pY"]), "Z": job(["pZ"]),
            "pX": p("End"), "pY": p("End"), "pZ": p("End"),
            "End": job([]),
        },
    }
    # End has three preds: pX (arm false of s1), pY and pZ (arms of s2, under arm true of s1).
    # s1 dominates End; pX maps to X, pY and pZ both map to s2 (the s1 arm target) -> not distinct.
    assert any("node 'End': fan-in" in x for x in errs(s))
    # inner join first (Mid), then the outer join: valid
    s["nodes"]["pY"]["next"] = ["Mid"]
    s["nodes"]["pZ"]["next"] = ["Mid"]
    s["nodes"]["Mid"] = job(["pM"])
    s["nodes"]["pM"] = p("End")
    s["nodes"]["End"]["in"] = {}
    s["nodes"]["Mid"]["kind"] = "job"
    assert errs(s) == []


def test_arm_target_same_for_two_arms_is_single_edge_ok():
    s = switch_spec()
    s["nodes"]["sw"]["cases"][1]["next"] = ["J"]
    del s["nodes"]["L"], s["nodes"]["P"]
    s["outputs"] = {"arm": "$.sw.arm"}
    assert errs(s) == []
    assert sorted(successors(s["nodes"]["sw"])) == ["H", "J"]


# ------------------------------------------------------------------ dominator ref rule


def test_ref_into_sibling_arm_rejected():
    s = switch_spec()
    s["nodes"]["H2"] = job([])
    s["nodes"]["sw"]["cases"][0]["next"] = ["H2"]
    s["nodes"]["H2"]["in"] = {"x": "$.L.lo"}  # L is in the sibling arm
    del s["nodes"]["H"]
    e = errs(s)
    assert any("node 'H2'" in x and "'L', which is not on every path to 'H2' (it may be skipped)" in x for x in e)


def test_ref_to_arm_node_after_rejoin_rejected():
    s = switch_spec()
    s["nodes"]["J"]["in"]["x"] = "$.L.lo"
    e = errs(s)
    assert any("refers to 'L', which is not on every path to 'J' (it may be skipped)" in x for x in e)


def test_ref_to_switch_predecessor_and_switch_after_rejoin_allowed():
    s = switch_spec()
    s["nodes"]["J"]["in"].update(x="$.A.score", y="$.sw.target")
    assert errs(s) == []


def test_pass_bind_ref_to_arm_node_inside_arm_allowed():
    assert errs(switch_spec()) == []  # P binds $.L.lo; L dominates P


def test_spec_outputs_may_reference_skippable_nodes_but_not_children():
    s = switch_spec()
    s["outputs"] = {"o": "$.L.lo", "p": "$.sw.target"}
    assert errs(s) == []
    sg = scatter_gather_spec()
    sg["outputs"] = {"f": "$.P.result"}
    assert any("outputs are not stored" in x for x in errs(sg))


def test_switch_output_names_declared():
    s = switch_spec()
    s["nodes"]["J"]["in"]["x"] = "$.sw.nope"
    assert any("output 'nope' not declared by node 'sw'" in x for x in errs(s))
    g = gate_spec()
    g["nodes"]["J"]["in"]["x"] = "$.sw.arm"
    assert errs(g) == []


def test_halt_declares_no_outputs():
    s = switch_spec()
    s["outputs"] = {"x": "$.H.arm"}
    assert any("not declared by node 'H'" in x for x in errs(s))


def test_switch_cannot_ref_its_own_arm_nodes():
    s = switch_spec()
    s["nodes"]["sw"]["cases"][0]["when"] = leaf("$.L.lo", "eq", 1)
    assert any("'L', which is not on every path to 'sw'" in x for x in errs(s))


def test_dominator_ref_equals_ancestor_rule_without_switches():
    s = linear_spec()
    s["nodes"]["A"]["in"]["x"] = "$.B.result"
    assert any("not on every path" in x for x in errs(s))


def test_scatter_child_still_refs_scatter_ancestors():
    s = scatter_gather_spec()
    s["nodes"]["P"]["in"]["y"] = "$.A.items"
    assert errs(s) == []


# ------------------------------------------------------------------ helpers


def test_dominators_and_successors():
    d = dominators(switch_spec())
    assert d["J"] == {"A", "sw", "J"}
    assert d["P"] == {"A", "sw", "L", "P"}
    assert d["A"] == {"A"}
    assert dominators({"nodes": {}}) == {}
    sg = dominators(scatter_gather_spec())
    assert "scatter_1" in sg["P"]


def test_dominators_ignores_unreachable_and_survives_cycle():
    s = switch_spec()
    s["nodes"]["Z"] = job()
    assert "Z" not in dominators(s)
    s["nodes"]["J"]["next"] = ["sw"]
    assert "J" in dominators(s)


def test_successors_dedup_and_order():
    s = switch_spec()
    assert successors(s["nodes"]["sw"]) == ["H", "L", "J"]
    assert successors(s["nodes"]["A"]) == ["sw"]
    assert successors(s["nodes"]["H"]) == []
    assert successors({"kind": "switch", "cases": [{"when": {}, "next": ["X"]}], "default": ["X"]}) == ["X"]


def test_switch_arms_labels():
    s = switch_spec()["nodes"]["sw"]
    assert switch_arms(s) == [("bad", "H"), ("lo", "L"), ("default", "J")]
    s["cases"][0].pop("name")
    assert switch_arms(s)[0] == ("case_0", "H")
    assert switch_arms(job()) == []
    assert switch_arms(None) == []


# ------------------------------------------------------------------ nodes_to_skip


def test_nodes_to_skip_with_rejoin():
    s = switch_spec()
    assert nodes_to_skip(s, "sw", "J") == ["H", "L", "P"]  # J itself and anything after it stays
    assert nodes_to_skip(s, "sw", "L") == ["H"]  # J reachable via L -> P -> J
    assert nodes_to_skip(s, "sw", "H") == ["L", "P", "J"]


def test_nodes_to_skip_same_target_two_arms():
    s = switch_spec()
    s["nodes"]["sw"]["cases"][1]["next"] = ["J"]
    del s["nodes"]["L"], s["nodes"]["P"]
    assert nodes_to_skip(s, "sw", "J") == ["H"]


def test_nodes_to_skip_includes_scatter_children():
    s = {
        "id": "x", "version": "1", "schema_version": "1.0", "entry": "A", "inputs": {"l": {"type": "string[]", "default": ["a"]}},
        "nodes": {
            "A": job(["sw"], out=("v",)),
            "sw": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["SC"], "on_false": ["H"]},
            "SC": {"kind": "scatter", "child": "C", "over": "$.inputs.l", "next": ["GA"]},
            "C": job(),
            "GA": {"kind": "gather", "collect": "$.C.o", "next": []},
            "H": {"kind": "halt", "status": "failed", "reason": "r"},
        },
    }
    assert errs(s) == []
    assert nodes_to_skip(s, "sw", "H") == ["SC", "C", "GA"]
    assert nodes_to_skip(s, "sw", "SC") == ["H"]


def test_nodes_to_skip_errors():
    s = switch_spec()
    with pytest.raises(WorkflowRoutingError):
        nodes_to_skip(s, "A", "J")
    with pytest.raises(WorkflowRoutingError):
        nodes_to_skip(s, "sw", "P")


# ------------------------------------------------------------------ evaluator


def ctx(**nodes):
    return {"inputs": {"t": 0.5, "name": "x", "flag": True, "lst": [1, "a"]}, "nodes": nodes}


def sw(*cases, default="D"):
    return {"kind": "switch", "cases": [{"when": w, "next": [t]} for w, t in cases], "default": [default]}


@pytest.mark.parametrize("op,l,r,expected", [
    ("eq", 1, 1, True), ("eq", 1, 1.0, True), ("eq", 1, "1", False), ("eq", True, 1, False), ("eq", [1, 2], [1, 2], True),
    ("eq", {"a": 1}, {"a": 1}, True), ("eq", [True], [1], False), ("eq", None, None, True),
    ("ne", 1, 2, True), ("ne", "a", "a", False), ("ne", True, 1, True),
    ("lt", 1, 2, True), ("lt", 2, 2, False), ("le", 2, 2, True), ("gt", 3, 2.5, True), ("gt", 2, 2, False),
    ("ge", 2, 2, True), ("ge", 1, 2, False),
    ("lt", "a", "b", True), ("ge", "b", "a", True), ("gt", "a", "b", False),
    ("in", 1, [1, 2], True), ("in", "z", ["a"], False), ("in", 1, [True], False), ("in", [1], [[1]], True),
])
def test_ops(op, l, r, expected):
    label, tgt = evaluate_switch(sw((leaf(l, op, r), "T")), ctx())
    assert (label, tgt) == (("case_0", "T") if expected else ("default", "D"))


@pytest.mark.parametrize("op,l,r", [
    ("lt", "a", 1), ("lt", 1, "a"), ("ge", True, 1), ("gt", 1, False), ("le", None, 1), ("lt", [1], [2]), ("gt", {}, 1),
])
def test_ordering_type_errors(op, l, r):
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw((leaf(l, op, r), "T")), ctx())


def test_in_needs_list_on_right():
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw((leaf(1, "in", "abc"), "T")), ctx())
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw((leaf(1, "in", 5), "T")), ctx())


def test_unknown_op_and_malformed_when():
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw((leaf(1, "zz", 1), "T")), ctx())
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw(({"left": 1}, "T")), ctx())


def test_refs_resolved_in_left_right_and_in_list():
    c = ctx(A={"score": 0.2})
    assert evaluate_switch(sw((leaf("$.A.score", "lt", "$.inputs.t"), "T")), c) == ("case_0", "T")
    assert evaluate_switch(sw((leaf("$.inputs.name", "in", ["x", "y"]), "T")), c) == ("case_0", "T")
    assert evaluate_switch(sw((leaf("$.inputs.name", "in", "$.inputs.lst"), "T")), c) == ("default", "D")
    assert evaluate_switch(sw((leaf(1, "in", "$.inputs.lst"), "T")), c) == ("case_0", "T")


def test_first_match_wins_and_names():
    node = {"kind": "switch", "cases": [
        {"name": "a", "when": leaf(1, "eq", 2), "next": ["X"]},
        {"name": "b", "when": leaf(1, "eq", 1), "next": ["Y"]},
        {"when": leaf(1, "eq", 1), "next": ["Z"]}], "default": ["D"]}
    assert evaluate_switch(node, ctx()) == ("b", "Y")


def test_tree_all_any_not():
    t, f = leaf(1, "eq", 1), leaf(1, "eq", 2)
    def run(w):
        return evaluate_switch(sw((w, "T")), ctx())[1]
    assert run({"all": [t, t]}) == "T" and run({"all": [t, f]}) == "D"
    assert run({"any": [f, t]}) == "T" and run({"any": [f, f]}) == "D"
    assert run({"not": f}) == "T" and run({"not": t}) == "D"
    assert run({"not": {"all": [t, {"any": [f, {"not": t}]}]}}) == "T"


def test_short_circuit_skips_missing_refs_and_type_errors():
    missing, bad = leaf("$.ghost.x", "eq", 1), leaf("a", "lt", 1)
    t, f = leaf(1, "eq", 1), leaf(1, "eq", 2)
    assert evaluate_switch(sw(({"all": [f, missing]}, "T")), ctx()) == ("default", "D")
    assert evaluate_switch(sw(({"any": [t, missing]}, "T")), ctx()) == ("case_0", "T")
    assert evaluate_switch(sw(({"all": [f, bad]}, "T")), ctx()) == ("default", "D")
    with pytest.raises(WorkflowRefError):
        evaluate_switch(sw(({"all": [t, missing]}, "T")), ctx())
    with pytest.raises(WorkflowRefError):
        evaluate_switch(sw(({"any": [f, missing]}, "T")), ctx())
    # a later case is not evaluated once an earlier one matched
    assert evaluate_switch(sw((t, "T"), (missing, "U")), ctx()) == ("case_0", "T")


def test_missing_and_null_ref_is_ref_error():
    for r in ("$.ghost.x", "$.inputs.nope", "$.A.nope"):
        with pytest.raises(WorkflowRefError):
            evaluate_switch(sw((leaf(r, "eq", 1), "T")), ctx(A={"score": 1}))
    with pytest.raises(WorkflowRefError, match="null"):
        evaluate_switch(sw((leaf("$.A.score", "eq", 1), "T")), ctx(A={"score": None}))
    with pytest.raises(WorkflowRefError):
        evaluate_switch(sw((leaf(1, "eq", "$.A.score"), "T")), ctx(A={"score": None}))


def test_literal_null_is_allowed():
    assert evaluate_switch(sw((leaf(None, "eq", None), "T")), ctx()) == ("case_0", "T")


def test_gate_evaluation():
    g = {"kind": "gate", "when": leaf("$.A.v", "ge", 0.5), "on_true": ["Y"], "on_false": ["N"]}
    assert evaluate_switch(g, ctx(A={"v": 0.7})) == ("true", "Y")
    assert evaluate_switch(g, ctx(A={"v": 0.1})) == ("false", "N")
    with pytest.raises(WorkflowRefError):
        evaluate_switch(g, ctx())
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(g, ctx(A={"v": "hi"}))


def test_on_choices_str_coercion_and_miss():
    node = {"kind": "switch", "on": "$.A.v", "choices": {"1": ["one"], "1.5": ["f"], "true": ["t"], "x": ["ex"]}, "default": ["D"]}
    for v, exp in [(1, ("1", "one")), (1.5, ("1.5", "f")), (True, ("true", "t")), ("x", ("x", "ex")),
                   ("nope", ("default", "D")), (2, ("default", "D")), (False, ("default", "D")), ([1], ("default", "D"))]:
        assert evaluate_switch(node, ctx(A={"v": v})) == exp
    with pytest.raises(WorkflowRefError):
        evaluate_switch(node, ctx(A={"v": None}))
    with pytest.raises(WorkflowRefError):
        evaluate_switch(node, ctx())


def test_evaluate_rejects_non_switch():
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(job(), ctx())


def test_depth_cap_enforced_at_runtime_too():
    t = leaf(1, "eq", 1)
    for _ in range(9):
        t = {"not": t}
    with pytest.raises(WorkflowRoutingError):
        evaluate_switch(sw((t, "T")), ctx())


def test_evaluate_against_build_context_with_event_overlay():
    status = {"inputs": {"t": 0.8}, "nodes": {"A": {"state": "running"}}}
    event = {"step_id": "A", "workflow_outputs": [{"name": "score", "value_d": 0.4}]}
    c = refs.build_context(status, event)
    assert evaluate_switch(switch_spec()["nodes"]["sw"], c) == ("lo", "L")
    assert evaluate_switch(switch_spec()["nodes"]["sw"], refs.build_context({"inputs": {"t": 0.8}, "nodes": {"A": {"outputs": {"score": 0.9}}}})) == ("default", "J")


# ------------------------------------------------------------------ skipped state / context


def test_build_context_marks_skipped_and_resolve_allow_skipped():
    status = {"inputs": {}, "nodes": {"A": {"state": "completed", "outputs": {"x": 1}},
                                      "B": {"state": "skipped"}}}
    c = refs.build_context(status)
    assert c["skipped"] == {"B"}
    with pytest.raises(WorkflowRefError):
        refs.resolve("$.B.y", c)  # node inputs: unchanged strict semantics
    assert refs.resolve({"a": "$.A.x", "b": "$.B.y", "l": ["$.B.z"]}, c, allow_skipped=True) == {"a": 1, "b": None, "l": [None]}
    with pytest.raises(WorkflowRefError):
        refs.resolve("$.C.y", c, allow_skipped=True)  # unknown / not-yet-run still errors


def test_build_context_shape_unchanged_without_skipped():
    assert "skipped" not in refs.build_context({"inputs": {}, "nodes": {"A": {"state": "completed", "outputs": {"x": 1}}}})


def test_skipped_is_terminal_and_cancel_shares_the_constant():
    assert "skipped" in ws.TERMINAL_NODE_STATES
    assert workflow_cancel.TERMINAL_NODE_STATES is ws.TERMINAL_NODE_STATES
    doc = {"status": "running", "nodes": {"a": {"state": "skipped"}, "b": {"state": "pending"}}}
    out = workflow_cancel.cancel_mutate("u")(doc)
    assert out["nodes"]["a"]["state"] == "skipped" and out["nodes"]["b"]["state"] == "cancelled"


def test_error_classes():
    assert ws.ERROR_CLASS_HALTED == "halted" and ws.ERROR_CLASS_ROUTING == "routing"


def test_stamp_nodes_skipped_gets_no_times_and_is_not_current():
    old = {"status": "running", "nodes": {"A": {"state": "running", "started_at": "t"}, "B": {"state": "pending"}}}
    new = copy.deepcopy(old)
    new["nodes"]["A"]["state"] = "completed"
    new["nodes"]["B"]["state"] = "skipped"
    new["updated_at"] = "2026-11-15T00:00:00.000Z"
    ws._stamp_nodes(old, new)
    assert "started_at" not in new["nodes"]["B"] and "ended_at" not in new["nodes"]["B"]
    assert new["nodes"]["A"]["ended_at"] == "2026-11-15T00:00:00.000Z"
    assert new["current_nodes"] == []


# ------------------------------------------------------------------ connectors


def test_resolve_connectors_switch_and_gate():
    s = switch_spec()
    s["nodes"]["g"] = {"kind": "gate", "when": leaf(1, "eq", 1), "on_true": ["H"], "on_false": ["H"]}
    resolved, e = resolve_connectors(s, DEFAULT_WORKFLOW_CONNECTORS)
    assert e == []
    assert resolved["nodes"]["sw"]["connector"] == "job-workflow-switch:v1.0.0"
    assert resolved["nodes"]["g"]["connector_queue"] == "workflow-connector-switch"
    assert "connector" not in resolved["nodes"]["H"]
    assert "connector" not in s["nodes"]["sw"]


def test_resolve_connectors_errors():
    s = switch_spec()
    s["nodes"]["sw"]["connector"] = "evil:1"
    _, e = resolve_connectors(s, DEFAULT_WORKFLOW_CONNECTORS)
    assert any("node 'sw' (switch): connector 'evil:1' is not allowed" in x for x in e)
    cfg = {k: v for k, v in DEFAULT_WORKFLOW_CONNECTORS.items() if k != "switch"}
    s = switch_spec()
    s["nodes"]["g"] = {"kind": "gate", "when": leaf(1, "eq", 1), "on_true": ["H"], "on_false": ["H"]}
    _, e = resolve_connectors(s, cfg)
    assert any("node 'g' (gate): no connectors configured for kind 'switch'" in x for x in e)

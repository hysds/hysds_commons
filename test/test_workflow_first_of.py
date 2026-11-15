"""first_of (D5): validation (forms, placement, order, coverage) and runtime resolution."""
import pytest

from hysds_commons import workflow_refs as refs
from hysds_commons.workflow_refs import WorkflowRefError
from hysds_commons.workflow_spec import validate_spec


def job(nxt=None, ins=None, out=("o",)):
    return {"kind": "job", "run": "job-x:v1", "in": ins or {}, "out": list(out), "next": nxt or []}


def pas(nxt, bind=None):
    return {"kind": "pass", "bind": bind or {}, "next": [nxt]}


def leaf(left, op, right):
    return {"left": left, "op": op, "right": right}


def FO(*e):
    return {"first_of": list(e)}


def diamond():
    """A -> g -(true)-> X -> pX -> J ; g -(false)-> Y -> pY -> J (exclusive rejoin)."""
    return {
        "id": "d", "version": "1", "schema_version": "1.0", "inputs": {"t": {"type": "string", "default": "a"}},
        "entry": "A",
        "nodes": {
            "A": job(["g"], out=("v",)),
            "g": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["X"], "on_false": ["Y"]},
            "X": job(["pX"]), "Y": job(["pY"]),
            "pX": pas("J"), "pY": pas("J"),
            "J": job([], ins={"granule": FO("$.X.o", "$.Y.o")}),
        },
        "outputs": {"g": FO("$.X.o", "$.Y.o")},
    }


def errs(s):
    return validate_spec(s)


def setj(s, v, field="granule"):
    s["nodes"]["J"]["in"] = {field: v}
    return s


def test_diamond_valid_with_coverage():
    assert errs(diamond()) == []


def test_nested_inside_in_values_and_lists():
    s = diamond()
    s["nodes"]["J"]["in"] = {"a": {"b": [FO("$.X.o", "$.Y.o")]}}
    assert errs(s) == []
    s["nodes"]["J"]["in"] = {"a": {"b": [FO("$.X.o", "$.pX.nope")]}}
    assert any("in.a.b[0]" in e and "not declared by node 'pX'" in e for e in errs(s))


def test_coverage_fail_names_uncovered_predecessor_exact_message():
    s = setj(diamond(), FO("$.X.o", "$.pX.m"))
    s["nodes"]["pX"]["bind"] = {"J.m": "$.X.o"}
    e = errs(s)
    assert ("node 'J' field 'in.granule': first_of does not cover every path to 'J'; "
            "add a ref on the path through ['pY']") in e


def test_coverage_dominator_last_and_inputs_last_ok():
    assert errs(setj(diamond(), FO("$.X.o", "$.A.v"))) == []
    assert errs(setj(diamond(), FO("$.X.o", "$.inputs.t"))) == []


def test_never_used_after_dominator_exact_wording():
    e = errs(setj(diamond(), FO("$.A.v", "$.X.o")))
    assert "node 'J' field 'in.granule': first_of entry $.X.o can never be used; $.A.v before it always resolves " \
           "(it is on every path to 'J')" in e


def test_never_used_after_input():
    e = errs(setj(diamond(), FO("$.inputs.t", "$.X.o")))
    assert "node 'J' field 'in.granule': first_of entry $.X.o can never be used; $.inputs.t before it always " \
           "resolves (is a workflow input)" in e


def test_a_dominator_in_the_middle_makes_the_rest_dead():
    e = errs(setj(diamond(), FO("$.X.o", "$.A.v", "$.Y.o")))
    assert any("$.Y.o can never be used; $.A.v before it" in x for x in e)


def nested():
    def p(n):
        return pas(n)
    return {
        "id": "n", "version": "1", "schema_version": "1.0", "entry": "A",
        "nodes": {
            "A": job(["s1"], out=("v",)),
            "s1": {"kind": "gate", "when": leaf("$.A.v", "eq", 1), "on_true": ["s2"], "on_false": ["X"]},
            "s2": {"kind": "gate", "when": leaf("$.A.v", "eq", 2), "on_true": ["Y"], "on_false": ["Z"]},
            "X": job(["pX"]), "Y": job(["pY"]), "Z": job(["pZ"]),
            "pX": p("End"), "pY": p("Mid"), "pZ": p("Mid"),
            "Mid": job(["pM"]), "pM": p("End"),
            "End": job([], ins={"g": FO("$.X.o", "$.Mid.o")}),
        },
    }


def test_nested_switch_coverage():
    assert errs(nested()) == []  # Mid is the inner rejoin, X the other outer arm
    s = nested()
    s["nodes"]["End"]["in"] = {"g": FO("$.X.o", "$.Y.o")}  # Z path uncovered
    e = errs(s)
    assert any("does not cover every path to 'End'" in x and "'pM'" in x for x in e)
    s["nodes"]["End"]["in"] = {"g": FO("$.Y.o", "$.Z.o", "$.X.o")}
    assert errs(s) == []


def test_entry_in_list_covers_everything():
    s = setj(diamond(), FO("$.X.o", "$.A.v"))
    assert errs(s) == []


@pytest.mark.parametrize("bad,expected", [
    (FO("$.X.o"), "at least 2"),
    ({"first_of": "$.X.o"}, "at least 2"),
    ({"first_of": ["$.X.o", "$.Y.o"], "x": 1}, "single key"),
    (FO("$.X.o", "literal"), "must be a '$.' ref"),
    (FO("$.X.o", 3), "must be a '$.' ref"),
    (FO("$.X.o", FO("$.Y.o", "$.A.v")), "must be a '$.' ref"),
    (FO("$.X.o", "$.scatter.item"), "scatter.item"),
    (FO("$.X.o", "$.a.b.c"), "malformed ref"),
    (FO("$.X.o", "$.ghost.o"), "unknown node 'ghost'"),
    (FO("$.X.o", "$.inputs.zz"), "undeclared input 'zz'"),
    (FO("$.X.o", "$.X.nope"), "not declared by node 'X'"),
    (FO("$.X.o", "$.J.o"), "not an ancestor of 'J'"),
])
def test_form_errors(bad, expected):
    e = errs(setj(diamond(), bad))
    assert any(expected in x and "node 'J' field 'in.granule'" in x for x in e), e


def test_entry_not_an_ancestor():
    s = diamond()
    s["nodes"]["X"]["in"] = {"g": FO("$.Y.o", "$.A.v")}  # Y is a sibling-arm node: never on a path to X
    e = errs(s)
    assert any("first_of entry $.Y.o refers to 'Y', which is not an ancestor of 'X'" in x for x in e)


def test_scatter_child_cannot_be_listed():
    from hysds_commons.workflow_examples import scatter_gather_spec
    s = scatter_gather_spec()
    s["nodes"]["B"]["in"]["x"] = FO("$.P.result", "$.A.items")
    assert any("first_of entry $.P.result refers to 'P', which is not an ancestor of 'B'" in x for x in errs(s))


def test_pass_bind_value_allowed_and_checked():
    s = nested()
    s["nodes"]["pM"]["bind"] = {"End.m": FO("$.Y.o", "$.Z.o", "$.Mid.o")}
    assert errs(s) == []
    s["nodes"]["pM"]["bind"] = {"End.m": FO("$.Mid.o", "$.Y.o")}
    assert any("node 'pM' field 'bind.End.m'" in x and "can never be used" in x for x in errs(s))
    s["nodes"]["pM"]["bind"] = {"End.m": FO("$.Y.o", "$.Z.o")}  # Mid itself unlisted but Y/Z cover both paths
    assert errs(s) == []
    s["nodes"]["pM"]["bind"] = {"End.m": FO("$.Y.o", "$.inputs.zz")}
    assert any("undeclared input 'zz'" in x for x in errs(s))


@pytest.mark.parametrize("where", ["switch-when", "switch-on", "gate-when", "scatter-over", "gather-collect"])
def test_rejected_elsewhere(where):
    from hysds_commons.workflow_examples import scatter_gather_spec
    fo = FO("$.A.v", "$.inputs.t")
    if where == "switch-when":
        s = diamond()
        s["nodes"]["g"] = {"kind": "switch", "cases": [{"when": leaf(fo, "eq", 1), "next": ["X"]}], "default": ["Y"]}
    elif where == "switch-on":
        s = diamond()
        s["nodes"]["g"] = {"kind": "switch", "on": fo, "choices": {"a": ["X"]}, "default": ["Y"]}
    elif where == "gate-when":
        s = diamond()
        s["nodes"]["g"]["when"] = leaf(1, "eq", fo)
    elif where == "scatter-over":
        s = scatter_gather_spec()
        s["nodes"]["scatter_1"]["over"] = fo
    else:
        s = scatter_gather_spec()
        s["nodes"]["gather_1"]["collect"] = [fo]
    assert any("first_of is only allowed in" in x for x in errs(s)), errs(s)


# ---- spec outputs

def test_spec_outputs_first_of_no_coverage_needed():
    s = diamond()
    s["outputs"] = {"o": FO("$.X.o", "$.pX.nope2")}
    assert any("not declared by node 'pX'" in x for x in errs(s))
    s["outputs"] = {"o": FO("$.X.o", "$.Y.o"), "p": FO("$.X.o", "$.inputs.t"), "q": FO("$.X.o", "$.A.v")}
    assert errs(s) == []


def test_spec_outputs_inputs_must_be_last_but_dominators_need_not():
    s = diamond()
    s["outputs"] = {"o": FO("$.inputs.t", "$.X.o")}
    assert any("spec outputs field 'o': first_of entry $.X.o can never be used; $.inputs.t before it" in x for x in errs(s))
    s["outputs"] = {"o": FO("$.A.v", "$.X.o")}  # a dominator may still be skipped? no: completes, but no rule for outputs
    assert errs(s) == []


def test_spec_outputs_child_and_literal_rejected():
    from hysds_commons.workflow_examples import scatter_gather_spec
    s = scatter_gather_spec()
    s["outputs"] = {"o": FO("$.P.result", "$.A.items")}
    assert any("outputs are not stored" in x for x in errs(s))
    s["outputs"] = {"o": FO("$.A.items", "lit")}
    assert any("must be a '$.' ref" in x for x in errs(s))


# ---- runtime

def ctx(done=(), skipped=(), **nodes):
    c = {"inputs": {"t": "in"}, "nodes": nodes, "completed": set(done)}
    if skipped:
        c["skipped"] = set(skipped)
    return c


def test_first_completed_wins_and_falls_through():
    c = ctx(done=["A", "Y"], A={"v": "a"}, Y={"o": "y"})
    assert refs.resolve(FO("$.X.o", "$.Y.o", "$.A.v"), c) == "y"
    c = ctx(done=["A", "X", "Y"], A={"v": "a"}, X={"o": "x"}, Y={"o": "y"})
    assert refs.resolve(FO("$.X.o", "$.Y.o"), c) == "x"
    assert refs.resolve(FO("$.Y.o", "$.X.o"), c) == "y"


def test_skipped_pending_fall_through_and_inputs_last():
    c = ctx(done=["A"], skipped=["X"], A={"v": "a"})
    assert refs.resolve({"k": [FO("$.X.o", "$.Y.o", "$.inputs.t")]}, c) == {"k": ["in"]}
    assert refs.resolve(FO("$.X.o", "$.A.v"), c) == "a"


def test_completed_node_missing_or_null_output_is_an_error_not_fall_through():
    with pytest.raises(WorkflowRefError, match="completed but has no"):
        refs.resolve(FO("$.X.o", "$.A.v"), ctx(done=["A", "X"], A={"v": "a"}, X={}))
    with pytest.raises(WorkflowRefError, match="completed but has no"):
        refs.resolve(FO("$.X.o", "$.A.v"), ctx(done=["A", "X"], A={"v": "a"}, X={"o": None}))
    with pytest.raises(WorkflowRefError):  # completed with no outputs recorded at all
        refs.resolve(FO("$.X.o", "$.A.v"), ctx(done=["A", "X"], A={"v": "a"}))


def test_none_resolvable_errors_but_none_for_spec_outputs():
    c = ctx(done=["A"], skipped=["X"], A={"v": "a"})
    with pytest.raises(WorkflowRefError, match="none of its nodes completed"):
        refs.resolve(FO("$.X.o", "$.Y.o"), c)
    assert refs.resolve({"o": FO("$.X.o", "$.Y.o")}, c, allow_skipped=True) == {"o": None}


def test_false_and_zero_values_are_real_values():
    assert refs.resolve(FO("$.X.o", "$.Y.o"), ctx(done=["X"], X={"o": False})) is False
    assert refs.resolve(FO("$.X.o", "$.Y.o"), ctx(done=["X"], X={"o": 0})) == 0


def test_build_context_completed_from_status_and_event():
    status = {"inputs": {}, "nodes": {"A": {"state": "completed", "outputs": {"v": 1}},
                                      "X": {"state": "running"}, "Y": {"state": "skipped"}}}
    c = refs.build_context(status, {"step_id": "X", "workflow_outputs": [{"name": "o", "value_s": "x"}]})
    assert c["completed"] == {"A", "X"}
    assert refs.resolve(FO("$.Y.o", "$.X.o", "$.A.v"), c) == "x"
    # event of a failed job does not mark it completed
    c = refs.build_context(status, {"step_id": "X", "status": "job-failed"})
    assert "X" not in c.get("completed", set())


def test_context_without_completed_key_uses_presence_of_outputs():
    c = {"inputs": {}, "nodes": {"Y": {"o": "y"}}}
    assert refs.resolve(FO("$.X.o", "$.Y.o"), c) == "y"


def test_resolve_binds_and_inputs_use_first_of():
    node = {"in": {"g": FO("$.X.o", "$.Y.o")}, "bind": {"J.m": FO("$.X.o", "$.Y.o")}}
    c = ctx(done=["Y"], Y={"o": "y"})
    assert refs.resolve_inputs(node, c) == {"g": "y"}
    assert refs.resolve_binds(node, c) == {"m": "y"}


def test_malformed_first_of_at_runtime():
    with pytest.raises(WorkflowRefError):
        refs.resolve({"first_of": []}, ctx())
    with pytest.raises(WorkflowRefError):
        refs.resolve({"first_of": ["lit", "$.A.v"]}, ctx())


def test_e2e_switch_fixture_uses_first_of_and_resolves_per_arm():
    import json
    import os
    spec = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "spec-switch.json")))
    assert errs(spec) == []
    node = spec["nodes"]["publish"]
    inputs = {"publish_outputs": "[]"}
    default_arm = ctx(done=["L2SMP", "qa_route"], skipped=["reprocess_L2SMP"], L2SMP={"product": "L2SMP.h5"}, qa_route={"arm": "default"})
    default_arm["inputs"] = inputs
    assert refs.resolve_inputs(node, default_arm)["granule"] == "L2SMP.h5"
    reprocess_arm = ctx(done=["L2SMP", "reprocess_L2SMP"], L2SMP={"product": "L2SMP.h5"}, reprocess_L2SMP={"product": "L2SMP-r.h5"},
                        qa_route={"arm": "reprocess"})
    reprocess_arm["inputs"] = inputs
    assert refs.resolve_inputs(node, reprocess_arm)["granule"] == "L2SMP-r.h5"

import itertools

import pytest

from hysds_commons.workflow_refs import WorkflowRefError
from hysds_commons.workflow_refs2 import (
    evaluate_when,
    link_merge,
    lookup_ref,
    pick_value,
    resolve_entry,
    resolve_outputs,
    resolve_run_inputs,
    resolve_step_inputs,
)

SETTLED = {
    "a": {"state": "completed", "outputs": {"v": 1, "nul": None, "lst": [1, None, 2], "flag": True, "off": False}},
    "b": {"state": "completed", "outputs": {"v": 2}},
    "sk": {"state": "skipped"},
    "run": {"state": "running", "outputs": {}},
}
INPUTS = {"x": "hello", "n": 0}


def ent(**kw):
    return kw


# ------------------------------------------------------------------ lookup


def test_lookup_cases():
    assert lookup_ref("$.inputs.x", SETTLED, INPUTS) == "hello"
    assert lookup_ref("$.inputs.n", SETTLED, INPUTS) == 0
    assert lookup_ref("$.a.v", SETTLED, INPUTS) == 1
    assert lookup_ref("$.a.nul", SETTLED, INPUTS) is None          # present, null
    assert lookup_ref("$.sk.anything", SETTLED, INPUTS) is None    # skipped -> null


@pytest.mark.parametrize("ref,msg", [
    ("$.inputs.nope", "unknown run input"),
    ("$.zz.v", "has not settled"),          # absent
    ("$.a.nope", "has no output"),          # completed, output absent
    ("$.run.v", "not completed or skipped"),
    ("a.v", "malformed"),
    ("$.a", "malformed"),
    ("$.a.b.c", "malformed"),
    (5, "malformed"),
])
def test_lookup_errors(ref, msg):
    with pytest.raises(WorkflowRefError, match=msg):
        lookup_ref(ref, SETTLED, INPUTS)


def test_lookup_returns_copy():
    v = lookup_ref("$.a.lst", SETTLED, INPUTS)
    v.append(9)
    assert SETTLED["a"]["outputs"]["lst"] == [1, None, 2]


# ------------------------------------------------------------------ link_merge


def test_link_merge_single_source_no_mode_is_value():
    assert link_merge([[1, 2]], None) == [1, 2]
    assert link_merge([None], None) is None


def test_link_merge_multi_source_default_nested():
    assert link_merge([1, None, [2]], None) == [1, None, [2]]


def test_link_merge_nested_explicit():
    assert link_merge([[1, 2]], "merge_nested") == [[1, 2]]
    assert link_merge([1, 2], "merge_nested") == [1, 2]


def test_link_merge_flattened():
    assert link_merge([[1, 2]], "merge_flattened") == [1, 2]
    assert link_merge([[1, None], 3, None, [4]], "merge_flattened") == [1, None, 3, None, 4]
    assert link_merge([5], "merge_flattened") == [5]


def test_link_merge_unknown():
    with pytest.raises(WorkflowRefError):
        link_merge([1], "zip")


# ------------------------------------------------------------------ pick


def test_pick_first_non_null():
    assert pick_value([None, 2, 3], "first_non_null") == 2
    assert pick_value([0, 1], "first_non_null") == 0  # falsy is not null
    assert pick_value([False], "first_non_null") is False


def test_pick_first_non_null_all_null_errors():
    with pytest.raises(WorkflowRefError, match="first_non_null"):
        pick_value([None, None], "first_non_null")
    with pytest.raises(WorkflowRefError):
        pick_value([], "first_non_null")


def test_pick_the_only_non_null():
    assert pick_value([None, 7, None], "the_only_non_null") == 7


@pytest.mark.parametrize("vals", [[1, 2], [None, None], [], [1, None, 3]])
def test_pick_the_only_non_null_errors(vals):
    with pytest.raises(WorkflowRefError, match="the_only_non_null"):
        pick_value(vals, "the_only_non_null")


def test_pick_all_non_null():
    assert pick_value([None, 1, None, 2], "all_non_null") == [1, 2]
    assert pick_value([None], "all_non_null") == []
    assert pick_value([], "all_non_null") == []


def test_pick_needs_list_and_known_mode():
    with pytest.raises(WorkflowRefError, match="needs a list"):
        pick_value(3, "first_non_null")
    with pytest.raises(WorkflowRefError, match="unknown pick"):
        pick_value([1], "bogus")


# ------------------------------------------------------------------ every link_merge x pick through resolve_entry

LM = [None, "merge_nested", "merge_flattened"]
PK = [None, "first_non_null", "the_only_non_null", "all_non_null"]

# two sources: a.lst = [1, None, 2] and b.v = 2 ; sk.o = null
MATRIX_SOURCES = {
    "two": ["$.a.lst", "$.b.v"],
    "with_skipped": ["$.sk.o", "$.b.v"],
    "all_null": ["$.sk.o", "$.a.nul"],
}


def _expected(sources_key, lm, pk):
    raw = {"two": [[1, None, 2], 2], "with_skipped": [None, 2], "all_null": [None, None]}[sources_key]
    merged = raw if lm in (None, "merge_nested") else [x for v in raw for x in (v if isinstance(v, list) else [v])]
    if pk is None:
        return merged
    non_null = [v for v in merged if v is not None]
    if pk == "all_non_null":
        return non_null
    if pk == "first_non_null":
        return non_null[0] if non_null else WorkflowRefError
    return non_null[0] if len(non_null) == 1 else WorkflowRefError


@pytest.mark.parametrize("key,lm,pk", list(itertools.product(MATRIX_SOURCES, LM, PK)))
def test_link_merge_pick_matrix(key, lm, pk):
    entry = {"source": MATRIX_SOURCES[key]}
    if lm:
        entry["link_merge"] = lm
    if pk:
        entry["pick"] = pk
    exp = _expected(key, lm, pk)
    if exp is WorkflowRefError:
        with pytest.raises(WorkflowRefError):
            resolve_entry(entry, SETTLED, INPUTS)
    else:
        assert resolve_entry(entry, SETTLED, INPUTS) == exp


def test_single_source_list_with_pick_no_link_merge():
    # B23 single-source form: pick applies to the list value directly
    assert resolve_entry({"source": ["$.a.lst"], "pick": "all_non_null"}, SETTLED, INPUTS) == [1, 2]


def test_single_source_scalar_with_pick_errors():
    with pytest.raises(WorkflowRefError, match="needs a list"):
        resolve_entry({"source": ["$.a.v"], "pick": "first_non_null"}, SETTLED, INPUTS)


def test_canonical_scatter_null_drop():
    e = {"source": ["$.a.lst"], "link_merge": "merge_flattened", "pick": "all_non_null"}
    assert resolve_entry(e, SETTLED, INPUTS) == [1, 2]


# ------------------------------------------------------------------ default chain


def test_step_default_applies_only_when_null():
    assert resolve_entry({"source": ["$.a.v"], "default": 9}, SETTLED, INPUTS) == 1
    assert resolve_entry({"source": ["$.a.nul"], "default": 9}, SETTLED, INPUTS) == 9
    assert resolve_entry({"source": ["$.sk.o"], "default": 9}, SETTLED, INPUTS) == 9
    assert resolve_entry({"default": "normal"}, SETTLED, INPUTS) == "normal"


def test_falsy_value_not_replaced_by_default():
    assert resolve_entry({"source": ["$.a.off"], "default": True}, SETTLED, INPUTS) is False
    assert resolve_entry({"source": ["$.inputs.n"], "default": 5}, SETTLED, INPUTS) == 0


def test_default_does_not_rescue_pick_errors():
    for pk in ("first_non_null", "the_only_non_null"):
        e = {"source": ["$.sk.o", "$.a.nul"], "pick": pk, "default": "d"}
        with pytest.raises(WorkflowRefError):
            resolve_entry(e, SETTLED, INPUTS)


def test_all_non_null_never_null_so_default_unused():
    e = {"source": ["$.sk.o", "$.a.nul"], "pick": "all_non_null", "default": ["d"]}
    assert resolve_entry(e, SETTLED, INPUTS) == []


def test_default_after_pick_after_merge():
    # merge_nested of [None] -> [[...]]? single null source, nested -> [None]; pick all -> []
    e = {"source": ["$.sk.o"], "link_merge": "merge_nested", "pick": "all_non_null", "default": "d"}
    assert resolve_entry(e, SETTLED, INPUTS) == []
    # without pick, nested keeps a list (not null) so default is not used
    assert resolve_entry({"source": ["$.sk.o"], "link_merge": "merge_nested", "default": "d"}, SETTLED, INPUTS) == [None]


def test_default_chain_order_step_then_op_then_null():
    step = {"in": {"p": {"source": ["$.sk.o"], "default": "step"},
                   "q": {"source": ["$.sk.o"]},
                   "r": {"source": ["$.a.v"]},
                   "s": {"source": ["$.sk.o"]}},
            "op_defaults": {"p": "op", "q": "op", "r": "op", "extra": "op"}}
    out = resolve_step_inputs(step, SETTLED, INPUTS)
    assert out == {"p": "step", "q": "op", "r": 1, "s": None, "extra": "op"}


def test_op_defaults_not_overriding_present_value_and_copy():
    d = {"k": [1]}
    out = resolve_step_inputs({"in": {}, "op_defaults": {"z": d}}, SETTLED, INPUTS)
    out["z"]["k"].append(2)
    assert d == {"k": [1]}


def test_resolve_step_inputs_golden_deliver():
    import json
    import os
    spec = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "spec2-radiometer.json")))
    settled = {
        "qa_check": {"state": "completed", "outputs": {"deliver": True, "reprocess": False, "reject": False, "qa_score": 0.9}},
        "reprocess_l2smp": {"state": "skipped"},
        "l2smp": {"state": "completed", "outputs": {"product": "p.h5"}},
        "make_browse": {"state": "completed", "outputs": {"browse_png": "b.png"}},
        "deliver": {"state": "completed", "outputs": {"receipt": "r1"}},
    }
    inputs = {"orbit_id": "o1"}
    step = spec["steps"]["deliver"]
    assert evaluate_when(step["when"], {}, settled, inputs) is True
    assert resolve_step_inputs(step, settled, inputs) == {"product": "p.h5", "browse": "b.png", "orbit_id": "o1"}
    assert evaluate_when(spec["steps"]["reprocess_l2smp"]["when"], {}, settled, inputs) is False
    assert resolve_outputs(spec, settled, inputs) == {"product": "p.h5", "browse": "b.png", "receipt": "r1"}


def test_resolve_step_inputs_propagates_errors():
    with pytest.raises(WorkflowRefError):
        resolve_step_inputs({"in": {"p": {"source": ["$.zz.o"]}}}, SETTLED, INPUTS)


# ------------------------------------------------------------------ outputs


def test_resolve_outputs_optional_and_required():
    spec = {"outputs": {"o1": {"type": "File?", "source": ["$.sk.o", "$.b.v"], "pick": "first_non_null"},
                        "o2": {"type": "string?", "source": ["$.sk.o"]}}}
    assert resolve_outputs(spec, SETTLED, INPUTS) == {"o1": 2, "o2": None}


def test_resolve_outputs_null_in_required_fails():
    spec = {"outputs": {"o": {"type": "string", "source": ["$.sk.o"]}}}
    with pytest.raises(WorkflowRefError, match="not optional"):
        resolve_outputs(spec, SETTLED, INPUTS)


def test_resolve_outputs_untyped_null_allowed():
    assert resolve_outputs({"outputs": {"o": {"source": ["$.sk.o"]}}}, SETTLED, INPUTS) == {"o": None}


# ------------------------------------------------------------------ when


def test_when_input_form_true_false():
    assert evaluate_when("$(inputs.go)", {"go": True}) is True
    assert evaluate_when("$(inputs.go)", {"go": False}) is False
    assert evaluate_when("$( inputs.go )", {"go": True}) is True


def test_when_null_is_error():
    with pytest.raises(WorkflowRefError, match="boolean, got null"):
        evaluate_when("$(inputs.go)", {"go": None})


@pytest.mark.parametrize("val", [0, 1, "true", [], {}])
def test_when_non_boolean_is_error(val):
    with pytest.raises(WorkflowRefError, match="boolean"):
        evaluate_when("$(inputs.go)", {"go": val})


def test_when_missing_input_and_unsupported_forms():
    with pytest.raises(WorkflowRefError, match="no input"):
        evaluate_when("$(inputs.go)", {})
    for bad in ["$(inputs.a == 1)", "inputs.go", "$(inputs.a) && $(inputs.b)", "true", "$(self)", None, 3]:
        with pytest.raises(WorkflowRefError):
            evaluate_when(bad, {"a": True, "b": True, "go": True})


def test_when_ref_form():
    assert evaluate_when("$.a.flag", {}, SETTLED, INPUTS) is True
    assert evaluate_when("$.a.off", {}, SETTLED, INPUTS) is False
    with pytest.raises(WorkflowRefError, match="boolean, got null"):
        evaluate_when("$.sk.flag", {}, SETTLED, INPUTS)   # skipped flag source -> null
    with pytest.raises(WorkflowRefError, match="boolean, got null"):
        evaluate_when("$.a.nul", {}, SETTLED, INPUTS)
    with pytest.raises(WorkflowRefError, match="has not settled"):
        evaluate_when("$.zz.flag", {}, SETTLED, INPUTS)


# ------------------------------------------------------------------ run inputs


def test_resolve_run_inputs_defaults_and_types():
    spec = {"inputs": {"s": {"type": "string"}, "d": {"type": "string", "default": "v7"},
                       "o": {"type": "int?"}, "l": {"type": "float[]"}, "b": {"type": "boolean"}}}
    res, errs = resolve_run_inputs(spec, {"s": "x", "l": [1, 2.5], "b": False})
    assert errs == [] and res == {"s": "x", "d": "v7", "o": None, "l": [1, 2.5], "b": False}


@pytest.mark.parametrize("inputs,msg", [
    ({}, "required but not provided"),
    ({"s": 3}, "expected string"),
    ({"s": "x", "zz": 1}, "unknown input"),
    ({"s": "x", "l": ["a"]}, "item 0"),
    ({"s": "x", "l": "a"}, "expected float[]"),
    ({"s": "x", "b": 1}, "expected boolean"),
    ({"s": None}, "null is not allowed"),
])
def test_resolve_run_inputs_errors(inputs, msg):
    spec = {"inputs": {"s": {"type": "string"}, "l": {"type": "float[]", "default": []}, "b": {"type": "boolean", "default": True}}}
    _, errs = resolve_run_inputs(spec, inputs)
    assert any(msg in e for e in errs), errs


def test_resolve_run_inputs_not_object():
    assert resolve_run_inputs({"inputs": {}}, [1])[1]
    assert resolve_run_inputs({"inputs": {}}, None) == ({}, [])

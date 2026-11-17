import pytest

pytest.importorskip("cwltool")

from hysds_commons.cwl import compile_cwl  # noqa: E402
from cwl_testlib import base, codes, dump, lookup_for, op, radiometer_bytes, with_step  # noqa: E402

JOBS = {
    "job-a:v1": (["x"], ["f", "ok", "n"]),
}


def run(wf, jobs=JOBS, **kw):
    return compile_cwl(dump(wf), job_type_lookup=lookup_for(jobs), **kw)


def err(res, code):
    hits = [e for e in res.errors if e["code"] == code]
    assert hits, codes(res.errors)
    return hits[0]


class TestRegistry:
    def test_ok(self):
        assert run(base()).ok

    def test_no_lookup_skips_checks(self):
        assert compile_cwl(dump(base())).ok

    def test_unknown_job_type(self):
        e = err(run(base(), jobs={}), "unknown_job_type")
        assert "job-a:v1" in e["message"] and e["path"].startswith("steps.a")

    def test_operation_input_not_a_param(self):
        wf = base()
        wf["steps"]["a"]["run"]["inputs"]["extra"] = "string?"
        e = err(run(wf), "unknown_param")
        assert "extra" in e["message"] and "x" in e["message"]

    def test_operation_inputs_must_be_subset(self):
        wf = base()
        wf["steps"]["a"]["run"]["inputs"]["extra"] = {"type": "string", "default": "d"}
        assert "unknown_param" in codes(run(wf).errors)

    def test_required_param_missing_from_operation(self):
        jobs = {"job-a:v1": (["x", "y"], ["f", "ok", "n"])}
        e = err(run(base(), jobs=jobs), "missing_param")
        assert "'y'" in e["message"]

    def test_required_param_declared_but_unbound(self):
        jobs = {"job-a:v1": (["x", "y"], ["f", "ok", "n"])}
        wf = base()
        wf["steps"]["a"]["run"]["inputs"]["y"] = "string?"
        assert "missing_param" in codes(run(wf, jobs=jobs).errors)
        wf["steps"]["a"]["in"]["y"] = {"default": "v"}
        assert run(wf, jobs=jobs).ok

    def test_optional_and_defaulted_params_not_required(self):
        info = {"inputs": [{"name": "x"}, {"name": "o", "optional": True}, {"name": "d", "default": 1},
                           {"name": "sys", "from": "dataset_jpath:_id"}],
                "outputs": ["f", "ok", "n"]}
        assert compile_cwl(dump(base()), job_type_lookup=lambda jt: info).ok

    def test_params_key_and_dict_forms(self):
        info = {"params": {"x": {}}, "outputs": {"f": {}, "ok": {}, "n": {}}}
        assert compile_cwl(dump(base()), job_type_lookup=lambda jt: info).ok

    def test_output_not_produced_by_job(self):
        jobs = {"job-a:v1": (["x"], ["f", "ok"])}
        e = err(run(base(), jobs=jobs), "unknown_output")
        assert "'n'" in e["message"]

    def test_unknown_outputs_are_not_checked(self):
        assert compile_cwl(dump(base()), job_type_lookup=lambda jt: {"inputs": ["x"]}).ok

    def test_halt_steps_are_not_looked_up(self):
        res = compile_cwl(radiometer_bytes(), job_type_lookup=lambda jt: None)
        assert [e["path"] for e in res.errors if e["code"] == "unknown_job_type"]
        assert not any("reject" in e["path"] for e in res.errors)

    def test_radiometer_with_matching_registry(self):
        from hysds_commons.cwl import compile_cwl as cc
        first = cc(radiometer_bytes())
        jobs = {}
        for sid, st in first.spec["steps"].items():
            if "job_type" in st:
                prev_in, prev_out = jobs.get(st["job_type"], (set(), set()))
                jobs[st["job_type"]] = (prev_in | set(st["in"]) | set(st.get("op_defaults", {})),
                                        prev_out | set(st["out"]))
        res = cc(radiometer_bytes(), job_type_lookup=lookup_for(jobs))
        assert res.ok, res.errors

    def test_lookup_errors_do_not_hide_profile_errors(self):
        wf = base()
        wf["steps"]["a"]["run"]["hints"] = {}
        res = run(wf)
        assert "unbound_run" in codes(res.errors)


# ------------------------------------------------------------------------------- decide job (section 9)

DECIDE = "job-workflow-decide:v1"


def decide_wf(cases=None, outs=("high", "low", "otherwise"), otherwise=None, extra_in=None, op_outputs=None):
    wf = base()
    wf["requirements"]["SchemaDefRequirement"] = {"types": [
        {"name": "Case", "type": "record", "fields": [{"name": "name", "type": "string"}, {"name": "when", "type": "Any"}]}]}
    cases = cases if cases is not None else [{"name": "high", "when": {"op": "ge", "right": 0.8}},
                                             {"name": "low", "when": {"op": "lt", "right": 0.3}}]
    ins = {"value": "float", "cases": {"type": {"type": "array", "items": "Case"}}}
    step_in = {"value": "score", "cases": {"default": cases}}
    if otherwise is not None:
        ins["otherwise"] = "string?"
        step_in["otherwise"] = {"default": otherwise}
    step_in.update(extra_in or {})
    wf["inputs"]["score"] = "float"
    with_step(wf, "route", {"run": op(DECIDE, ins, op_outputs or {o: "boolean" for o in outs}), "in": step_in,
                            "out": list(outs)})
    return wf


DECIDE_JOBS = {
    DECIDE: {"inputs": [{"name": "value", "optional": True}, {"name": "cases"},
                        {"name": "otherwise", "optional": True, "default": "otherwise"}]},
    "job-a:v1": (["x"], ["f", "ok", "n"]),
}


class TestDecide:
    def test_compiles(self):
        res = run(decide_wf(), jobs=DECIDE_JOBS)
        assert res.ok, res.errors
        st = res.spec["steps"]["route"]
        assert st["job_type"] == DECIDE and st["out"] == ["high", "low", "otherwise"]
        assert st["in"]["cases"]["default"][1] == {"name": "low", "when": {"op": "lt", "right": 0.3}}
        assert st["in"]["value"] == {"source": ["$.inputs.score"]}

    def test_compiles_without_registry(self):
        assert compile_cwl(dump(decide_wf())).ok

    def test_custom_otherwise_name(self):
        res = run(decide_wf(outs=("high", "low", "rest"), otherwise="rest"), jobs=DECIDE_JOBS)
        assert res.ok, res.errors

    def test_out_must_be_case_or_otherwise(self):
        e = err(run(decide_wf(outs=("high", "bogus")), jobs=DECIDE_JOBS), "decide_out")
        assert "bogus" in e["message"]

    def test_default_is_not_the_otherwise_name(self):
        # outputs include `otherwise`, never `default` (B24)
        assert "decide_out" in codes(run(decide_wf(outs=("high", "default")), jobs=DECIDE_JOBS).errors)

    def test_outputs_must_be_boolean(self):
        wf = decide_wf(op_outputs={"high": "string", "low": "boolean", "otherwise": "boolean?"})
        assert "decide_out" in codes(run(wf, jobs=DECIDE_JOBS).errors)

    @pytest.mark.parametrize("name", ["1bad", "has space", "", "value", "cases", None, 3])
    def test_bad_case_names(self, name):
        wf = decide_wf(cases=[{"name": name, "when": True}], outs=("otherwise",))
        assert "decide_cases" in codes(run(wf, jobs=DECIDE_JOBS).errors)

    def test_duplicate_case_names(self):
        wf = decide_wf(cases=[{"name": "a", "when": True}, {"name": "a", "when": False}], outs=("a", "otherwise"))
        e = err(run(wf, jobs=DECIDE_JOBS), "decide_cases")
        assert "duplicate" in e["message"]

    def test_otherwise_collides_with_case(self):
        wf = decide_wf(cases=[{"name": "rest", "when": True}], outs=("rest",), otherwise="rest")
        assert "decide_cases" in codes(run(wf, jobs=DECIDE_JOBS).errors)

    @pytest.mark.parametrize("when", [
        True, False,
        {"op": "eq", "right": 1}, {"left": 3, "op": "ne", "right": 4}, {"op": "in", "right": [1, 2]},
        {"all": [True, {"op": "gt", "right": 1}]}, {"any": [False]}, {"not": {"op": "le", "right": 2}},
    ])
    def test_valid_when_forms(self, when):
        wf = decide_wf(cases=[{"name": "c", "when": when}], outs=("c", "otherwise"))
        assert run(wf, jobs=DECIDE_JOBS).ok

    @pytest.mark.parametrize("when", [
        "yes", 3, None, [], {}, {"op": "like", "right": 1}, {"op": "eq"}, {"op": "in", "right": 3},
        {"all": []}, {"any": []}, {"all": "x"}, {"not": []}, {"op": "eq", "right": 1, "extra": 1},
        {"all": [True], "any": [True]},
    ])
    def test_invalid_when_forms(self, when):
        wf = decide_wf(cases=[{"name": "c", "when": when}], outs=("c", "otherwise"))
        assert "decide_cases" in codes(run(wf, jobs=DECIDE_JOBS).errors)

    def test_nesting_limit(self):
        tree = True
        for _ in range(7):
            tree = {"not": tree}
        ok = decide_wf(cases=[{"name": "c", "when": tree}], outs=("c", "otherwise"))
        assert run(ok, jobs=DECIDE_JOBS).ok  # 8 levels deep
        deeper = {"not": tree}
        bad = decide_wf(cases=[{"name": "c", "when": deeper}], outs=("c", "otherwise"))
        assert "decide_cases" in codes(run(bad, jobs=DECIDE_JOBS).errors)

    def test_case_with_extra_or_missing_fields(self):
        for case in ({"name": "c"}, {"when": True}, {"name": "c", "when": True, "x": 1}, "c"):
            wf = decide_wf(cases=[case], outs=("otherwise",))
            assert "decide_cases" in codes(run(wf, jobs=DECIDE_JOBS).errors)

    def test_cases_not_a_list(self):
        wf = decide_wf(cases=[], outs=("otherwise",))
        wf["steps"]["route"]["in"]["cases"]["default"] = {"name": "c", "when": True}
        res = run(wf, jobs=DECIDE_JOBS)
        assert not res.ok

    def test_decide_feeds_when_of_another_step(self):
        wf = decide_wf()
        with_step(wf, "next", {"when": "$(inputs.go)", "run": op("job-a:v1", {"x": "string"}, {"f": "File"}),
                               "in": {"go": "route/high", "x": "x"}, "out": ["f"]})
        res = run(wf, jobs=DECIDE_JOBS)
        assert res.ok, res.errors
        assert res.spec["steps"]["next"]["when"] == "$.route.high"
        assert res.spec["steps"]["next"]["deps"] == ["route"]

import hashlib
import json

import pytest

pytest.importorskip("cwltool")

from hysds_commons.cwl import (  # noqa: E402
    CompileResult, CWLCompileError, SOURCE_FORMAT, compile_cwl, compile_cwl_or_raise)
from cwl_testlib import (  # noqa: E402
    base, codes, dump, golden, lookup_for, norm_spec, op, radiometer_bytes, with_step)


def compile_(wf, **kw):
    return compile_cwl(dump(wf), **kw)


def assert_error(res, code, path_part=None):
    assert not res.ok, "expected an error %s" % code
    hits = [e for e in res.errors if e["code"] == code]
    assert hits, "no %s in %s" % (code, codes(res.errors))
    if path_part:
        assert any(path_part in e["path"] for e in hits), [e["path"] for e in hits]
    return hits[0]


# ------------------------------------------------------------------------------------------ radiometer


class TestRadiometer:
    def test_compiles_and_matches_golden(self):
        res = compile_cwl(radiometer_bytes())
        assert res.ok, res.errors
        assert res.warnings == []
        assert norm_spec(res.spec) == norm_spec(golden())

    def test_source_block(self):
        raw = radiometer_bytes()
        res = compile_cwl(raw)
        src = res.spec["source"]
        assert src["format"] == SOURCE_FORMAT == "cwl"
        assert src["sha256"] == hashlib.sha256(raw).hexdigest()
        assert src["path_map"]["steps.deliver.in.product"] == "/steps/deliver/in/product"
        assert src["path_map"]["steps.l1a"] == "/steps/l1a"

    def test_graph_and_result_shape(self):
        res = compile_cwl(radiometer_bytes())
        assert isinstance(res, CompileResult)
        d = res.as_dict()
        assert d["ok"] and d["compiled"] is res.spec
        assert {"from": "l1c", "to": "make_browse"} in d["graph"]["edges"]
        kinds = {n["id"]: n["kind"] for n in d["graph"]["nodes"]}
        assert kinds["reject"] == "halt" and kinds["deliver"] == "job"

    def test_hint_precedence_and_mapping(self):
        spec = compile_cwl(radiometer_bytes()).spec
        l1a, l1b = spec["steps"]["l1a"], spec["steps"]["l1b"]
        assert l1a["queue"] == "radiometer-large" and l1b["queue"] == "radiometer-default"
        # Retry object is replaced whole by the step hint, never merged (B11)
        assert l1a["max_retries"] == 2 and l1a["retry_on"] == ["exit_code:137", "offline"]
        assert l1b["max_retries"] == 1 and l1b["retry_on"] == ["offline", "worker"]
        assert l1a["time_limit"] == 3600 and "time_limit" not in l1b
        assert l1a["scatter"] == {"input": "l0b_file", "threshold": 0.95}
        assert spec["on_error"] == "kill" and spec["version"] == "1.0.0"
        assert spec["steps"]["reject"] == {"halt": {"status": "failed", "reason": "L2SMP failed QA"},
                                           "deps": ["qa_check"], "when": "$.qa_check.reject"}

    def test_when_flag_not_passed_to_job(self):
        st = compile_cwl(radiometer_bytes()).spec["steps"]["deliver"]
        assert "go" not in st["in"] and st["when"] == "$.qa_check.deliver"
        assert st["deps"][0] == "qa_check"

    def test_compile_or_raise(self):
        assert compile_cwl_or_raise(radiometer_bytes())["id"] == "radiometer-orbit-l2smp"
        wf = base()
        wf["class"] = "CommandLineTool"
        with pytest.raises(CWLCompileError) as ei:
            compile_cwl_or_raise(dump(wf))
        assert ei.value.errors


# ------------------------------------------------------------------------------------- document rules


class TestDocument:
    def test_minimal_ok(self):
        res = compile_(base())
        assert res.ok, res.errors

    def test_yaml_input_and_str(self):
        res = compile_cwl(radiometer_bytes().decode())
        assert res.ok

    def test_parse_error(self):
        res = compile_cwl(b"a: [unclosed")
        assert codes(res.errors) == ["parse_error"]
        assert codes(compile_cwl(b"- 1\n- 2\n").errors) == ["parse_error"]

    def test_cwl_version(self):
        wf = base()
        wf["cwlVersion"] = "v1.0"
        assert_error(compile_(wf), "cwl_version")

    @pytest.mark.parametrize("cls,frag", [("CommandLineTool", "CommandLineTool"), ("ExpressionTool", "ExpressionTool")])
    def test_not_a_workflow(self, cls, frag):
        wf = base()
        wf["class"] = cls
        assert frag in assert_error(compile_(wf), "not_a_workflow")["message"]

    def test_missing_namespace(self):
        wf = base()
        wf["$namespaces"] = {"s": "https://schema.org/"}
        assert_error(compile_(wf), "missing_namespace")

    def test_namespace_any_prefix(self):
        wf = base()
        wf["$namespaces"] = {"hy": wf["$namespaces"]["hysds"], "s": "https://schema.org/"}
        text = json.dumps(wf).replace("hysds:", "hy:")
        assert compile_cwl(text).ok

    def test_hint_matched_by_uri_not_prefix(self):
        wf = base()
        wf["$namespaces"]["hysds"] = "https://example.org/other#"
        wf["$namespaces"]["real"] = "https://github.com/hysds/hysds_commons/cwl/v1#"
        res = compile_(wf)
        # the document uses 'hysds:JobType' but hysds now means another namespace: it is not a binding
        assert_error(res, "unbound_run")

    def test_missing_version(self):
        wf = base()
        del wf["s:version"]
        assert_error(compile_(wf), "missing_version")

    @pytest.mark.parametrize("v", ["main", "2026-01", "v1-rc.2"])
    def test_version_any_string(self, v):
        wf = base()
        wf["s:version"] = v
        res = compile_(wf)
        assert res.ok and res.spec["version"] == v

    def test_missing_id(self):
        wf = base()
        del wf["id"]
        assert_error(compile_(wf), "bad_id")

    def test_schemas_stripped_with_warning(self):
        wf = base()
        wf["$schemas"] = ["http://example.invalid/schema.rdf"]
        res = compile_(wf)
        assert res.ok and "schemas_ignored" in codes(res.warnings)

    def test_label_doc(self):
        wf = base()
        wf["label"], wf["doc"] = "L", "D"
        spec = compile_(wf).spec
        assert spec["label"] == "L" and spec["description"] == "D"

    def test_graph_form_with_op_refs(self):
        wf = base()
        a = wf["steps"]["a"]
        opdef = dict(a["run"])
        opdef["id"] = "#a_op"
        a["run"] = "#a_op"
        doc = {"cwlVersion": "v1.2", "$namespaces": wf["$namespaces"], "s:version": "2.0",
               "$graph": [dict(wf, id="main"), opdef]}
        for k in ("cwlVersion", "$namespaces", "s:version"):
            doc["$graph"][0].pop(k)
        res = compile_cwl(json.dumps(doc))
        assert res.ok, res.errors
        assert res.spec["steps"]["a"]["job_type"] == "job-a:v1"

    def test_graph_requires_one_workflow(self):
        doc = {"cwlVersion": "v1.2", "$graph": [op("job-a:v1") | {"id": "x"}]}
        assert_error(compile_(doc), "graph_workflows")

    def test_graph_unknown_ref(self):
        wf = base()
        wf["steps"]["a"]["run"] = "#nope"
        doc = {"cwlVersion": "v1.2", "$graph": [dict(wf, id="main")]}
        assert "unresolved_run" in codes(compile_(doc).errors)

    def test_run_resolver_inlines_file_reference(self):
        wf = base()
        wf["steps"]["a"]["run"] = "operation/job-a:v1"
        res = compile_(wf, run_resolver=lambda ref: op("job-a:v1", {"x": "string"}, {"f": "File", "ok": "boolean", "n": "int"}))
        assert res.ok, res.errors
        assert res.cwl["steps"]["a"]["run"]["class"] == "Operation"

    def test_unresolved_run_reference(self):
        wf = base()
        wf["steps"]["a"]["run"] = "tool.cwl"
        assert_error(compile_(wf), "unresolved_run")

    def test_workflow_input_defaults_and_types(self):
        wf = base()
        wf["inputs"]["v"] = {"type": "string", "default": "d", "label": "Version"}
        wf["inputs"]["n"] = "int?"
        wf["inputs"]["f"] = {"type": "File", "default": {"class": "File", "location": "s3://b/k"}}
        spec = compile_(wf).spec
        assert spec["inputs"]["v"] == {"type": "string", "default": "d", "label": "Version"}
        assert spec["inputs"]["n"] == {"type": "int?"}
        assert spec["inputs"]["f"]["default"] == "s3://b/k"
        assert spec["inputs"]["items"] == {"type": "string[]"}


# ----------------------------------------------------------------------------------- requirements/hints


class TestRequirements:
    def test_subworkflow_requirement(self):
        wf = base()
        wf["requirements"]["SubworkflowFeatureRequirement"] = {}
        assert "subworkflows are not supported" in assert_error(compile_(wf), "subworkflow_unsupported")["message"]

    def test_subworkflow_step(self):
        wf = base()
        wf["requirements"]["SubworkflowFeatureRequirement"] = {}
        wf["steps"]["a"]["run"] = {"class": "Workflow", "inputs": {}, "outputs": {}, "steps": {}}
        res = compile_(wf)
        assert "subworkflows are not supported" in assert_error(res, "subworkflow_unsupported", "steps.a.run")["message"]

    @pytest.mark.parametrize("cls", ["CommandLineTool", "ExpressionTool"])
    def test_tool_steps(self, cls):
        wf = base()
        wf["steps"]["a"]["run"] = {"class": cls, "inputs": {}, "outputs": {}}
        assert_error(compile_(wf), "tool_unsupported", "steps.a.run")

    def test_inline_javascript(self):
        wf = base()
        wf["requirements"]["InlineJavascriptRequirement"] = {}
        m = assert_error(compile_(wf), "js_unsupported")["message"]
        assert m == "JS expressions are not supported; use when: $(inputs.flag)"

    def test_inline_javascript_as_hint(self):
        wf = base()
        wf["hints"] = {"InlineJavascriptRequirement": {}}
        assert_error(compile_(wf), "js_unsupported")

    def test_step_input_expression_requirement(self):
        wf = base()
        wf["requirements"]["StepInputExpressionRequirement"] = {}
        assert_error(compile_(wf), "value_from_unsupported")

    def test_value_from(self):
        wf = base()
        wf["requirements"]["StepInputExpressionRequirement"] = {}
        wf["steps"]["a"]["in"]["x"] = {"source": "x", "valueFrom": "$(self)"}
        assert_error(compile_(wf), "value_from_unsupported", "steps.a.in.x.valueFrom")

    def test_unknown_standard_requirement(self):
        wf = base()
        wf["requirements"]["ShellCommandRequirement"] = {}
        assert_error(compile_(wf), "unsupported_requirement")

    @pytest.mark.parametrize("cls", ["WorkReuse", "NetworkAccess", "DockerRequirement", "EnvVarRequirement",
                                     "InplaceUpdateRequirement", "LoadListingRequirement", "SoftwareRequirement",
                                     "InitialWorkDirRequirement"])
    def test_ignored_with_warning(self, cls):
        wf = base()
        wf["hints"] = {cls: {}}
        res = compile_(wf)
        assert "ignored_requirement" in codes(res.warnings), res.errors

    def test_resource_requirement_gb_warning(self):
        wf = base()
        wf["hints"] = {"ResourceRequirement": {"ramMin": 8}}
        assert "resource_units" in codes(compile_(wf).warnings)
        wf["hints"] = {"ResourceRequirement": {"ramMin": 8192}}
        assert "resource_units" not in codes(compile_(wf).warnings)

    def test_unknown_hysds_hint(self):
        wf = base()
        wf["hints"] = {"hysds:Bogus": {}}
        assert_error(compile_(wf), "unknown_hysds_hint")

    def test_hysds_as_requirement(self):
        wf = base()
        wf["requirements"]["hysds:Queue"] = {"queue": "q"}
        assert_error(compile_(wf), "hysds_requirement")

    @pytest.mark.parametrize("where,hint", [
        ("wf", {"hysds:Gather": {"tolerated_failure_percentage": 5}}),
        ("wf", {"hysds:JobType": {"job_type": "j:v1"}}),
        ("step", {"hysds:OnError": {"mode": "drain"}}),
        ("step", {"hysds:JobType": {"job_type": "j:v1"}}),
        ("op", {"hysds:OnError": {"mode": "drain"}}),
    ])
    def test_misplaced_hint(self, where, hint):
        wf = base()
        target = {"wf": wf, "step": wf["steps"]["a"], "op": wf["steps"]["a"]["run"]}[where]
        target.setdefault("hints", {}).update(hint)
        assert_error(compile_(wf), "misplaced_hint")

    def test_gather_on_unscattered_step(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"hysds:Gather": {"tolerated_failure_percentage": 5}}
        assert_error(compile_(wf), "misplaced_hint")

    @pytest.mark.parametrize("hint,key", [
        ({"hysds:OnError": {"mode": "explode"}}, "OnError"),
        ({"hysds:Queue": {"queue": ""}}, "Queue"),
        ({"hysds:Queue": {"queue": 3}}, "Queue"),
        ({"hysds:Retry": {"max_retries": -1}}, "Retry"),
        ({"hysds:Retry": {"retry_on": ["bogus"]}}, "Retry"),
        ({"hysds:Retry": {}}, "Retry"),
        ({"ToolTimeLimit": {"timelimit": "$(inputs.t)"}}, "ToolTimeLimit"),
        ({"ToolTimeLimit": {"timelimit": -5}}, "ToolTimeLimit"),
        ({"hysds:Queue": {"queue": "$(inputs.q)"}}, "Queue"),
    ])
    def test_bad_hint_values(self, hint, key):
        wf = base()
        wf["hints"] = hint
        res = compile_(wf)
        assert not res.ok and any(key in e["path"] or key in e["message"] for e in res.errors), res.errors

    def test_expression_in_time_limit_message(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"ToolTimeLimit": {"timelimit": "$(inputs.t)"}}
        assert "ToolTimeLimit.timelimit" in assert_error(compile_(wf), "expression_unsupported")["message"]

    def test_literal_dollar_default_is_not_an_expression(self):
        wf = base()
        wf["inputs"]["x"] = {"type": "string", "default": "cost $(5)"}
        assert compile_(wf).ok

    @pytest.mark.parametrize("tl,expected", [(0, None), (90, 90)])
    def test_time_limit(self, tl, expected):
        wf = base()
        wf["steps"]["a"]["hints"] = {"ToolTimeLimit": {"timelimit": tl}}
        st = compile_(wf).spec["steps"]["a"]
        assert st.get("time_limit") == expected

    def test_time_limit_zero_warns_and_the_job_types_limit_applies(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"ToolTimeLimit": {"timelimit": 0}}
        res = compile_(wf)
        assert res.ok and "time_limit" not in res.spec["steps"]["a"]
        w = [x for x in res.warnings if x["code"] == "timelimit_zero"]
        assert len(w) == 1 and w[0]["message"] == (
            "ToolTimeLimit 0 (no limit) is not supported; the job type's time_limit applies")
        wf["steps"]["a"]["hints"] = {"ToolTimeLimit": {"timelimit": 5}}
        assert "timelimit_zero" not in codes(compile_(wf).warnings)

    def test_most_specific_wins(self):
        wf = base()
        wf["hints"] = {"hysds:Queue": {"queue": "wfq"}}
        assert compile_(wf).spec["steps"]["a"]["queue"] == "wfq"
        wf["steps"]["a"]["hints"] = {"hysds:Queue": {"queue": "stepq"}}
        assert compile_(wf).spec["steps"]["a"]["queue"] == "stepq"
        wf["steps"]["a"]["run"]["hints"]["hysds:Queue"] = {"queue": "opq"}
        assert compile_(wf).spec["steps"]["a"]["queue"] == "opq"

    def test_requirement_beats_hint_same_level(self):
        wf = base()
        wf["hints"] = {"ToolTimeLimit": {"timelimit": 10}}
        wf["requirements"]["ToolTimeLimit"] = {"timelimit": 20}
        assert compile_(wf).spec["steps"]["a"]["time_limit"] == 20

    @pytest.mark.parametrize("mode", ["kill", "drain", "continue"])
    def test_on_error(self, mode):
        wf = base()
        wf["hints"] = {"hysds:OnError": {"mode": mode}}
        assert compile_(wf).spec["on_error"] == mode

    def test_foreign_hint_ignored_with_warning(self):
        wf = base()
        wf["$namespaces"]["x"] = "https://example.org/x#"
        wf["hints"] = {"x:Thing": {}}
        res = compile_(wf)
        assert res.ok and "foreign_class" in codes(res.warnings)

    def test_gather_threshold(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"hysds:Gather": {"tolerated_failure_percentage": 100}}
        wf["steps"]["a"]["scatter"] = "x"
        wf["inputs"]["x"] = "string[]"
        assert not compile_(wf).ok

    def test_tag_compiled(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"hysds:Tag": {"tag": "t"}}
        res = compile_(wf)
        assert res.ok and res.spec["steps"]["a"]["tag"] == "t"


# --------------------------------------------------------------------------------------- inputs / types


class TestTypes:
    @pytest.mark.parametrize("t", ["string", "int", "long", "float", "double", "boolean", "File", "Directory",
                                   "string[]", "File[]?", "int?", "Any"])
    def test_accepted_types(self, t):
        wf = base()
        wf["inputs"]["extra"] = t
        assert compile_(wf).ok

    @pytest.mark.parametrize("t", [{"type": "enum", "symbols": ["a"]}, ["string", "int"], "null",
                                   {"type": "record", "fields": {}}, "string[][]", "Bogus"])
    def test_rejected_types(self, t):
        wf = base()
        wf["inputs"]["extra"] = t
        assert_error(compile_(wf), "unsupported_type")

    @pytest.mark.parametrize("field,val", [("secondaryFiles", [".bai"]), ("format", "edam:x"),
                                           ("loadContents", True), ("loadListing", "shallow_listing")])
    def test_rejected_param_fields(self, field, val):
        wf = base()
        wf["inputs"]["f"] = {"type": "File", field: val}
        assert_error(compile_(wf), "file_field_unsupported", "inputs.f")

    @pytest.mark.parametrize("field", ["secondaryFiles", "checksum", "format", "contents"])
    def test_rejected_file_object_fields(self, field):
        wf = base()
        wf["inputs"]["f"] = {"type": "File", "default": {"class": "File", "location": "s3://b/k", field: "x"}}
        assert_error(compile_(wf), "file_field_unsupported", "inputs.f.default")

    def test_load_contents_on_step_input(self):
        wf = base()
        wf["steps"]["a"]["in"]["x"] = {"source": "x", "loadContents": True}
        assert_error(compile_(wf), "file_field_unsupported")

    def test_record_types(self):
        wf = base()
        wf["requirements"]["SchemaDefRequirement"] = {"types": [
            {"name": "Case", "type": "record",
             "fields": [{"name": "name", "type": "string"}, {"name": "when", "type": "Any"}]}]}
        wf["steps"]["a"]["run"]["inputs"]["cs"] = {"type": {"type": "array", "items": "Case"}}
        wf["steps"]["a"]["in"]["cs"] = {"default": [{"name": "hi", "when": {"op": "ge", "right": 1}}]}
        res = compile_(wf)
        assert res.ok, res.errors
        assert res.spec["steps"]["a"]["in"]["cs"]["default"][0]["name"] == "hi"

    def test_non_record_schemadef_rejected(self):
        wf = base()
        wf["requirements"]["SchemaDefRequirement"] = {"types": [{"name": "E", "type": "enum", "symbols": ["a"]}]}
        assert_error(compile_(wf), "unsupported_type")


# ----------------------------------------------------------------------------------------- steps


class TestSteps:
    def test_unbound_run(self):
        wf = base()
        wf["steps"]["a"]["run"]["hints"] = {}
        e = assert_error(compile_(wf), "unbound_run", "steps.a")
        assert "hysds:JobType" in e["message"]

    def test_double_binding(self):
        wf = base()
        wf["steps"]["a"]["run"]["hints"]["hysds:Halt"] = {"status": "failed"}
        assert_error(compile_(wf), "double_binding")

    def test_run_not_operation(self):
        wf = base()
        wf["steps"]["a"]["run"] = {"class": "Bogus"}
        assert_error(compile_(wf), "bad_run")

    def test_halt_must_not_have_io(self):
        wf = base()
        with_step(wf, "h", {"run": {"class": "Operation", "inputs": {"z": "string"}, "outputs": {},
                                    "hints": {"hysds:Halt": {"status": "failed"}}}, "in": {}, "out": []})
        assert_error(compile_(wf), "halt_io")

    def test_halt_bad_status(self):
        wf = base()
        with_step(wf, "h", {"run": {"class": "Operation", "inputs": {}, "outputs": {},
                                    "hints": {"hysds:Halt": {"status": "meh"}}}, "in": {}, "out": []})
        assert not compile_(wf).ok

    def test_halt_compiles_without_in_out(self):
        wf = base()
        with_step(wf, "h", {"when": "$(inputs.go)", "in": {"go": "a/ok"}, "out": [],
                            "run": {"class": "Operation", "inputs": {}, "outputs": {},
                                    "hints": {"hysds:Halt": {"status": "completed", "reason": "done"}}}})
        wf["outputs"]["r"]["type"] = "File?"
        res = compile_(wf)
        assert res.ok, res.errors
        assert res.spec["steps"]["h"] == {"halt": {"status": "completed", "reason": "done"}, "deps": ["a"],
                                          "when": "$.a.ok"}

    def test_unknown_source_and_output(self):
        wf = base()
        wf["steps"]["a"]["in"]["x"] = "nope"
        assert_error(compile_(wf), "unknown_source")
        wf = base()
        with_step(wf, "b", {"run": op("job-b:v1", {"y": "File"}, {}), "in": {"y": "a/zzz"}, "out": []})
        assert_error(compile_(wf), "unknown_source")
        wf = base()
        with_step(wf, "b", {"run": op("job-b:v1", {"y": "File"}, {}), "in": {"y": "ghost/f"}, "out": []})
        assert_error(compile_(wf), "unknown_source")

    def test_missing_input(self):
        wf = base()
        wf["steps"]["a"]["in"] = {}
        assert_error(compile_(wf), "missing_input")

    def test_op_input_default_and_optional_need_no_binding(self):
        wf = base()
        wf["steps"]["a"]["run"]["inputs"] = {"x": "string", "o": "int?", "d": {"type": "string", "default": "dd"}}
        res = compile_(wf)
        assert res.ok, res.errors
        assert res.spec["steps"]["a"]["op_defaults"] == {"d": "dd"}

    def test_cycle(self):
        wf = base()
        wf["steps"]["a"]["run"]["inputs"]["y"] = "File"
        wf["steps"]["a"]["in"]["y"] = "b/g"
        with_step(wf, "b", {"run": op("job-b:v1", {"y": "File"}, {"g": "File"}), "in": {"y": "a/f"}, "out": ["g"]})
        assert_error(compile_(wf), "cycle")

    def test_step_id_rules(self):
        wf = base()
        wf["steps"]["inputs"] = wf["steps"].pop("a")
        wf["outputs"] = {}
        assert_error(compile_(wf), "bad_name")

    def test_non_param_in_entry_is_not_passed(self):
        wf = base()
        with_step(wf, "b", {"run": op("job-b:v1", {"y": "File"}, {}), "in": {"y": "a/f", "extra": "a/n"}, "out": []})
        res = compile_(wf)
        assert res.ok, res.errors
        assert list(res.spec["steps"]["b"]["in"]) == ["y"] and res.spec["steps"]["b"]["deps"] == ["a"]

    def test_default_only_entry(self):
        wf = base()
        wf["steps"]["a"]["run"]["inputs"]["p"] = "string"
        wf["steps"]["a"]["in"]["p"] = {"default": "dv"}
        assert compile_(wf).spec["steps"]["a"]["in"]["p"] == {"default": "dv"}

    def test_deps_from_sources_and_when(self):
        wf = base()
        with_step(wf, "b", {"when": "$(inputs.go)", "run": op("job-b:v1", {"y": "File"}, {}),
                            "in": {"go": "a/ok", "y": "a/f"}, "out": []})
        st = compile_(wf).spec["steps"]["b"]
        assert st["deps"] == ["a"] and st["when"] == "$.a.ok"

    def test_when_flag_from_workflow_input(self):
        wf = base()
        with_step(wf, "b", {"when": "$(inputs.go)", "run": op("job-b:v1", {"y": "File"}, {}),
                            "in": {"go": "flag", "y": "a/f"}, "out": []})
        assert compile_(wf).spec["steps"]["b"]["when"] == "$.inputs.flag"


class TestInEntries:
    def test_link_merge_nested_rejected(self):
        wf = base()
        wf["steps"]["a"]["in"]["x"] = {"source": ["x"], "linkMerge": "merge_nested"}
        assert_error(compile_(wf), "merge_nested_unsupported")

    def test_list_source_needs_multiple_input_requirement(self):
        wf = base()
        del wf["requirements"]["MultipleInputFeatureRequirement"]
        wf["steps"]["a"]["in"]["x"] = {"source": ["x"], "pickValue": "first_non_null"}
        assert_error(compile_(wf), "missing_requirement", "steps.a.in.x")

    def test_output_list_source_needs_multiple_input_requirement(self):
        wf = base()
        del wf["requirements"]["MultipleInputFeatureRequirement"]
        wf["outputs"]["r"].update(outputSource=["a/f"], pickValue="first_non_null")
        assert_error(compile_(wf), "missing_requirement", "outputs.r")

    def test_bad_pick(self):
        wf = base()
        wf["steps"]["a"]["in"]["x"] = {"source": ["x"], "pickValue": "bogus"}
        assert_error(compile_(wf), "bad_pick")

    def test_pick_default_ordering_in_spec(self):
        wf = base()
        wf["steps"]["a"]["in"]["x"] = {"source": ["x", "x"], "pickValue": "first_non_null", "default": "z"}
        e = compile_(wf).spec["steps"]["a"]["in"]["x"]
        assert e == {"source": ["$.inputs.x", "$.inputs.x"], "pick": "first_non_null", "default": "z"}


# ------------------------------------------------------------------------------------------- when


class TestWhen:
    def cond(self, when="$(inputs.go)", go="a/ok", **kw):
        wf = base()
        st = {"when": when, "run": op("job-b:v1", {"y": "File"}, {"g": "File"}),
              "in": {"go": go, "y": "a/f"}, "out": ["g"]}
        st.update(kw)
        with_step(wf, "b", st)
        return wf

    def test_ok(self):
        assert compile_(self.cond()).ok

    @pytest.mark.parametrize("expr", ["$(inputs.n > 3)", "$(inputs.go && true)", "${return true;}",
                                      "$(inputs.go ? 1 : 0)", "$(self)"])
    def test_javascript_rejected(self, expr):
        e = assert_error(compile_(self.cond(when=expr)), "js_unsupported", "steps.b.when")
        assert e["message"] == "JS expressions are not supported; use when: $(inputs.flag)"

    def test_when_not_expression(self):
        assert_error(compile_(self.cond(when="true")), "bad_when")
        assert_error(compile_(self.cond(when=True)), "bad_when")

    def test_when_unknown_input(self):
        assert_error(compile_(self.cond(when="$(inputs.nothing)")), "bad_when")

    def test_flag_must_be_boolean(self):
        wf = self.cond(go="a/n")
        assert_error(compile_(wf), "when_flag_invalid")

    def test_flag_must_not_be_optional(self):
        wf = base()
        wf["steps"]["a"]["run"]["outputs"]["ok"] = "boolean?"
        with_step(wf, "b", {"when": "$(inputs.go)", "run": op("job-b:v1", {"y": "File"}, {}),
                            "in": {"go": "a/ok", "y": "a/f"}, "out": []})
        assert_error(compile_(wf), "when_flag_invalid")

    def test_flag_input_optional_without_default_rejected_with_default_ok(self):
        wf = self.cond(go="opt")
        wf["inputs"]["opt"] = "boolean?"
        assert_error(compile_(wf), "when_flag_invalid")
        wf["inputs"]["opt"] = {"type": "boolean?", "default": True}
        assert compile_(wf).ok

    def test_flag_from_skippable_step_rejected(self):
        wf = base()
        with_step(wf, "b", {"when": "$(inputs.go)", "run": op("job-b:v1", {}, {"flag2": "boolean"}),
                            "in": {"go": "a/ok"}, "out": ["flag2"]})
        with_step(wf, "c", {"when": "$(inputs.go)", "run": op("job-c:v1", {}, {}), "in": {"go": "b/flag2"}, "out": []})
        e = assert_error(compile_(wf), "when_flag_invalid", "steps.c")
        assert "skipped" in e["message"]

    def test_flag_from_scatter_rejected(self):
        wf = base()
        with_step(wf, "s", {"scatter": "i", "run": op("job-s:v1", {"i": "string"}, {"flag2": "boolean"}),
                            "in": {"i": "items"}, "out": ["flag2"]})
        with_step(wf, "c", {"when": "$(inputs.go)", "run": op("job-c:v1", {}, {}), "in": {"go": "s/flag2"}, "out": []})
        assert_error(compile_(wf), "when_flag_invalid")

    def test_flag_with_pick_rejected(self):
        wf = self.cond()
        wf["steps"]["b"]["in"]["go"] = {"source": ["a/ok"], "pickValue": "first_non_null"}
        assert_error(compile_(wf), "when_flag_invalid")


# ------------------------------------------------------------------------------------------ scatter


class TestScatter:
    def sc(self, **kw):
        wf = base()
        st = {"scatter": "i", "run": op("job-s:v1", {"i": "string"}, {"o": "File"}), "in": {"i": "items"}, "out": ["o"]}
        st.update(kw)
        with_step(wf, "s", st)
        wf["outputs"]["r"]["outputSource"] = "s/o"
        wf["outputs"]["r"]["type"] = "File[]"
        return wf

    def test_ok_and_default_threshold(self):
        res = compile_(self.sc())
        assert res.ok, res.errors
        assert res.spec["steps"]["s"]["scatter"] == {"input": "i", "threshold": 1.0}

    def test_scatter_needs_requirement(self):
        wf = self.sc()
        del wf["requirements"]["ScatterFeatureRequirement"]
        assert_error(compile_(wf), "missing_requirement", "steps.s.scatter")

    def test_nested_scatter_rejected(self):
        wf = self.sc(scatter=["i", "j"])
        assert_error(compile_(wf), "scatter_unsupported")

    @pytest.mark.parametrize("m", ["nested_crossproduct", "flat_crossproduct"])
    def test_cross_product_rejected(self, m):
        assert_error(compile_(self.sc(scatterMethod=m)), "scatter_unsupported")

    def test_dotproduct_accepted(self):
        assert compile_(self.sc(scatterMethod="dotproduct")).ok

    def test_scatter_with_when_rejected(self):
        wf = self.sc(when="$(inputs.go)")
        wf["steps"]["s"]["in"]["go"] = "a/ok"
        wf["steps"]["s"]["scatter"] = "i"
        assert_error(compile_(wf), "scatter_with_when", "steps.s")

    def test_scatter_input_must_be_in_in(self):
        assert_error(compile_(self.sc(scatter="zzz")), "bad_scatter")

    def test_scatter_input_not_array(self):
        wf = self.sc()
        wf["steps"]["s"]["in"]["i"] = "x"
        assert_error(compile_(wf), "scatter_input_not_array")

    def test_scatter_input_null_rejected(self):
        wf = self.sc()
        wf["inputs"]["maybe"] = "string[]?"
        wf["steps"]["s"]["in"]["i"] = "maybe"
        assert_error(compile_(wf), "scatter_input_null")

    def test_scatter_input_from_conditional_step_rejected(self):
        wf = self.sc()
        with_step(wf, "p", {"when": "$(inputs.go)", "run": op("job-p:v1", {}, {"l": "string[]"}),
                            "in": {"go": "a/ok"}, "out": ["l"]})
        wf["steps"]["s"]["in"]["i"] = "p/l"
        assert_error(compile_(wf), "scatter_input_null")

    def test_scatter_input_with_default_ok(self):
        wf = self.sc()
        wf["inputs"]["maybe"] = "string[]?"
        wf["steps"]["s"]["in"]["i"] = {"source": "maybe", "default": ["z"]}
        assert compile_(wf).ok

    @pytest.mark.parametrize("hint", [{"job_type": "job-lister:v1"}, {"job_type": "job-lister:v1", "queue": "q"}])
    def test_scatter_connector_hint_rejected(self, hint):
        e = assert_error(compile_(self.sc(hints={"hysds:ScatterConnector": hint})), "scatter_connector_removed",
                         "steps.s.hints.hysds:ScatterConnector")
        assert "lister/items" in e["message"]

    def test_scatter_connector_hint_rejected_on_unscattered_step(self):
        wf = base()
        wf["steps"]["a"]["hints"] = {"hysds:ScatterConnector": {"job_type": "job-lister:v1"}}
        assert_error(compile_(wf), "scatter_connector_removed")

    def test_compile_takes_no_allowlist(self):
        with pytest.raises(TypeError):
            compile_(base(), allowed_connectors=[])

    def test_listing_step_scatter_has_no_connector(self):
        wf = self.sc()
        with_step(wf, "lister", {"run": op("job-lister:v1", {}, {"items": "string[]"}), "in": {}, "out": ["items"]})
        wf["steps"]["s"]["in"]["i"] = "lister/items"
        res = compile_(wf)
        assert res.ok, res.errors
        assert "connector" not in res.spec["steps"]["s"]["scatter"]
        assert res.spec["steps"]["s"]["deps"] == ["lister"]

    @pytest.mark.parametrize("pct,thr", [(0, 1.0), (5, 0.95), (50, 0.5), (99.5, 0.005)])
    def test_threshold(self, pct, thr):
        wf = self.sc(hints={"hysds:Gather": {"tolerated_failure_percentage": pct}})
        wf["outputs"]["r"]["type"] = "File[]"
        wf["outputs"]["r"].update(outputSource=["s/o"], linkMerge="merge_flattened", pickValue="all_non_null")
        if pct:
            res = compile_(wf)
        else:
            res = compile_(self.sc(hints={"hysds:Gather": {"tolerated_failure_percentage": pct}}))
        assert res.ok, res.errors
        assert res.spec["steps"]["s"]["scatter"]["threshold"] == thr

    def test_gather_bounds(self):
        for bad in (-1, 100, "5", True):
            wf = self.sc(hints={"hysds:Gather": {"tolerated_failure_percentage": bad}})
            assert not compile_(wf).ok, bad


# ------------------------------------------------------------------------------------ null safety


def two_step(*, pick=None, default=None, sink="File", link=None, producer_when=True, extra_in=None,
             consumer_when=None):
    """a -> p (conditional producer) -> c (consumer of p/o)."""
    wf = base()
    p = {"run": op("job-p:v1", {}, {"o": "File"}), "in": {}, "out": ["o"]}
    if producer_when:
        p["when"] = "$(inputs.go)"
        p["in"]["go"] = "a/ok"
    with_step(wf, "p", p)
    entry = {"source": ["p/o"]}
    if pick:
        entry["pickValue"] = pick
    if default is not None:
        entry["default"] = default
    if link:
        entry["linkMerge"] = link
    c = {"run": op("job-c:v1", {"y": sink}, {"z": "File"}), "in": {"y": entry}, "out": ["z"]}
    if consumer_when:
        c["when"] = "$(inputs.go)"
        c["in"]["go"] = consumer_when
    c["in"].update(extra_in or {})
    with_step(wf, "c", c)
    wf["outputs"] = {"r": {"type": "File", "outputSource": "a/f"}}
    return wf


class TestNullSafety:
    def test_conditional_into_required_rejected(self):
        e = assert_error(compile_(two_step()), "null_unsafe", "steps.c.in.y")
        assert "optional" in e["message"]

    def test_own_when_is_not_enough(self):
        wf = two_step(consumer_when="a/n")  # a different (non-boolean here) flag; own when never rescues
        assert "null_unsafe" in codes(compile_(wf).errors)

    @pytest.mark.parametrize("pick", ["first_non_null", "the_only_non_null"])
    def test_pick_makes_it_safe(self, pick):
        assert compile_(two_step(pick=pick)).ok

    def test_all_non_null_with_array_sink(self):
        assert compile_(two_step(pick="all_non_null", sink="File[]", link="merge_flattened")).ok

    def test_default_makes_it_safe(self):
        assert compile_(two_step(default="s3://b/k")).ok

    def test_optional_sink_is_safe(self):
        assert compile_(two_step(sink="File?")).ok

    def test_same_when_flag_exception(self):
        assert compile_(two_step(consumer_when="a/ok")).ok

    def test_different_flag_not_exception(self):
        wf = two_step(consumer_when="flag")
        assert "null_unsafe" in codes(compile_(wf).errors)

    def test_optional_workflow_input_into_required(self):
        wf = base()
        wf["inputs"]["opt"] = "string?"
        wf["steps"]["a"]["in"]["x"] = "opt"
        assert_error(compile_(wf), "null_unsafe")
        wf["inputs"]["opt"] = {"type": "string?", "default": "d"}
        assert compile_(wf).ok

    def test_optional_producer_output_into_required(self):
        wf = two_step(producer_when=False)
        wf["steps"]["p"]["run"]["outputs"]["o"] = "File?"
        assert_error(compile_(wf), "null_unsafe")

    def test_same_flag_does_not_cover_optional_output_type(self):
        wf = two_step(consumer_when="a/ok")
        wf["steps"]["p"]["run"]["outputs"]["o"] = "File?"
        assert_error(compile_(wf), "null_unsafe")

    def test_unconditional_is_fine(self):
        assert compile_(two_step(producer_when=False)).ok

    def tolerant(self, pick=None, sink="File[]", link=None, pct=10, out=None):
        wf = base()
        with_step(wf, "s", {"scatter": "i", "run": op("job-s:v1", {"i": "string"}, {"o": "File"}),
                            "in": {"i": "items"}, "out": ["o"],
                            "hints": {"hysds:Gather": {"tolerated_failure_percentage": pct}}})
        entry = {"source": ["s/o"]}
        if pick:
            entry["pickValue"] = pick
        if link:
            entry["linkMerge"] = link
        with_step(wf, "c", {"run": op("job-c:v1", {"y": sink}, {"z": "File"}), "in": {"y": entry}, "out": ["z"]})
        if out:
            wf["outputs"] = out
        return wf

    def test_tolerant_scatter_without_all_non_null_rejected(self):
        e = assert_error(compile_(self.tolerant(link="merge_flattened")), "null_unsafe_scatter", "steps.c.in.y")
        assert "all_non_null" in e["message"]

    def test_tolerant_scatter_with_all_non_null_ok(self):
        assert compile_(self.tolerant(pick="all_non_null", link="merge_flattened")).ok

    def test_tolerant_scatter_item_optional_sink_ok(self):
        assert compile_(self.tolerant(sink={"type": {"type": "array", "items": ["null", "File"]}})).ok

    def test_zero_tolerance_scatter_is_safe(self):
        assert compile_(self.tolerant(pct=0, link="merge_flattened")).ok

    def test_single_source_all_non_null_is_normalized(self):
        wf = self.tolerant()
        wf["steps"]["c"]["in"]["y"] = {"source": "s/o", "pickValue": "all_non_null"}
        res = compile_(wf)
        assert res.ok, res.errors
        assert res.spec["steps"]["c"]["in"]["y"] == {"source": ["$.s.o"], "link_merge": "merge_flattened",
                                                     "pick": "all_non_null"}
        assert "normalized_pick" in codes(res.warnings)

    def test_tolerant_scatter_into_workflow_output(self):
        wf = self.tolerant(pick="all_non_null", link="merge_flattened",
                           out={"r": {"type": "File[]", "outputSource": "s/o"}})
        assert_error(compile_(wf), "null_unsafe_scatter", "outputs.r")
        wf = self.tolerant(pick="all_non_null", link="merge_flattened",
                           out={"r": {"type": "File[]", "outputSource": ["s/o"], "linkMerge": "merge_flattened",
                                      "pickValue": "all_non_null"}})
        res = compile_(wf)
        assert res.ok, res.errors

    def test_output_from_conditional_must_be_optional(self):
        wf = two_step(producer_when=True, sink="File?")
        wf["outputs"] = {"r": {"type": "File", "outputSource": "p/o"}}
        assert_error(compile_(wf), "null_unsafe_output", "outputs.r")
        wf["outputs"]["r"]["type"] = "File?"
        assert compile_(wf).ok

    def test_output_pick_makes_it_safe(self):
        wf = two_step(producer_when=True, sink="File?")
        wf["outputs"] = {"r": {"type": "File", "outputSource": ["p/o", "a/f"], "pickValue": "first_non_null"}}
        assert compile_(wf).ok

    def test_halt_completed_requires_optional_outputs(self):
        wf = base()
        with_step(wf, "h", {"when": "$(inputs.go)", "in": {"go": "a/ok"}, "out": [],
                            "run": {"class": "Operation", "inputs": {}, "outputs": {},
                                    "hints": {"hysds:Halt": {"status": "completed"}}}})
        assert_error(compile_(wf), "output_not_optional", "outputs.r")
        wf["outputs"]["r"]["type"] = "File?"
        assert compile_(wf).ok

    def test_halt_failed_does_not_require_optional_outputs(self):
        wf = base()
        with_step(wf, "h", {"when": "$(inputs.go)", "in": {"go": "a/ok"}, "out": [],
                            "run": {"class": "Operation", "inputs": {}, "outputs": {},
                                    "hints": {"hysds:Halt": {"status": "failed"}}}})
        assert compile_(wf).ok

    def test_cwltool_conditional_warning_dropped_when_proved_safe(self):
        res = compile_(two_step(pick="first_non_null"))
        assert res.ok and not [w for w in res.warnings if "may produce null" in w["message"]]


# ----------------------------------------------------------------------------------- cwltool layer


class TestCwltoolLayer:
    def test_cwltool_errors_are_structured(self):
        wf = base()
        wf["steps"]["a"]["bogus_field"] = 1
        res = compile_(wf)
        assert not res.ok
        assert all(set(e) == {"path", "code", "message"} for e in res.errors)

    def test_hysds_noise_filtered(self):
        res = compile_cwl(radiometer_bytes())
        assert not any("Unknown hint" in w["message"] or "undefined reference" in w["message"] for w in res.warnings)

    def test_cwltool_output_not_printed(self, capfd):
        compile_cwl(radiometer_bytes())
        out, err = capfd.readouterr()
        assert "Unknown hint" not in out + err and "undefined reference" not in out + err

    def test_no_network_for_schemas(self):
        wf = base()
        wf["$schemas"] = ["http://127.0.0.1:9/never.rdf"]
        assert compile_(wf).ok

    def test_not_reentrant_state_restored(self):
        import logging
        before = list(logging.getLogger("cwltool").handlers)
        compile_cwl(radiometer_bytes())
        assert list(logging.getLogger("cwltool").handlers) == before


# ------------------------------------------------------------------------------------ the final hook


class TestHook:
    def test_validate_compiled_hook_is_used(self, monkeypatch):
        import hysds_commons.cwl.compile as c
        monkeypatch.setattr(c, "_validate_compiled", lambda spec: ["steps.a: boom"])
        res = compile_(base())
        assert not res.ok and res.spec is None
        assert res.errors[-1] == {"path": "", "code": "spec_invalid", "message": "steps.a: boom"}

    def test_default_hook_is_spec2_validator(self):
        from hysds_commons.cwl import _validate_compiled
        assert _validate_compiled({"anything": 1})  # rejected by validate_spec2
        res = compile_cwl(radiometer_bytes())
        assert res.ok and _validate_compiled(res.spec) == []

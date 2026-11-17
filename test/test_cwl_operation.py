import importlib
import json
import subprocess
import sys

import pytest

pytest.importorskip("cwltool")

from hysds_commons.cwl import (  # noqa: E402
    EXTRA_MESSAGE, HYSDS_NS, compile_cwl, generate_operation, operation_snippet, validate_with_cwltool)
from cwl_testlib import codes, dump  # noqa: E402

HYSDS_IO = {
    "label": "L1A",
    "params": [
        {"name": "l0b_file", "from": "submitter", "type": "text", "placeholder": "L0B granule URL"},
        {"name": "ancillary_version", "from": "submitter", "type": "text", "default": "v7"},
        {"name": "threshold", "from": "submitter", "type": "number", "optional": True},
        {"name": "dry_run", "from": "submitter", "type": "boolean", "default": False},
        {"name": "meta", "from": "submitter", "type": "object", "optional": True},
        {"name": "dataset", "from": "dataset_jpath:_source", "type": "text"},
    ],
}
JOB_SPEC = {"command": "/home/ops/run.sh"}


def workflow_with(op, binding):
    return {
        "cwlVersion": "v1.2", "class": "Workflow", "id": "gen-test", "s:version": "1",
        "$namespaces": {"hysds": HYSDS_NS, "s": "https://schema.org/"},
        "inputs": {"url": "string"},
        "outputs": {"res": {"type": "File", "outputSource": "step/l1a_file"}},
        "steps": {"step": {"run": op, "in": binding, "out": ["l1a_file"]}},
    }


class TestGenerate:
    def test_shape(self):
        op = generate_operation("job-l1a:v3.2", HYSDS_IO, JOB_SPEC, outputs=["l1a_file"])
        assert op["class"] == "Operation"
        assert op["hints"] == {"hysds:JobType": {"job_type": "job-l1a:v3.2"}}
        assert op["outputs"] == {"l1a_file": {"type": "File"}}

    def test_inputs_optional_and_defaults_marked(self):
        i = generate_operation("j:v1", HYSDS_IO)["inputs"]
        assert i["l0b_file"] == {"type": "string", "doc": "L0B granule URL"}
        assert i["ancillary_version"] == {"type": "string", "default": "v7"}
        assert i["threshold"]["type"] == "double?"
        assert i["dry_run"] == {"type": "boolean", "default": False}
        assert i["meta"]["type"] == "Any?"
        assert i["dataset"]["type"] == "string?"  # filled by the system, not by the workflow author
        assert list(i) == [p["name"] for p in HYSDS_IO["params"]]

    def test_outputs_sources_and_forms(self):
        assert generate_operation("j:v1", {"outputs": ["a"]})["outputs"] == {"a": {"type": "File"}}
        assert generate_operation("j:v1", None, {"outputs": {"n": "int"}})["outputs"] == {"n": {"type": "int"}}
        assert generate_operation("j:v1", None, None, outputs=[{"name": "x", "type": "string"}])["outputs"] == {"x": {"type": "string"}}
        assert generate_operation("j:v1")["outputs"] == {}

    def test_invalid_names_skipped(self):
        i = generate_operation("j:v1", {"params": [{"name": "bad name"}, {"name": "ok"}, {"name": "0x"}]})["inputs"]
        assert list(i) == ["ok"]

    def test_requires_job_type(self):
        with pytest.raises(ValueError):
            generate_operation("")

    def test_snippet_is_yaml_of_the_same_operation(self):
        from ruamel.yaml import YAML
        import io
        text = operation_snippet("job-l1a:v3.2", HYSDS_IO, JOB_SPEC, outputs=["l1a_file"])
        assert "class: Operation" in text and "hysds:JobType" in text
        assert YAML(typ="safe").load(io.StringIO(text)) == json.loads(json.dumps(
            generate_operation("job-l1a:v3.2", HYSDS_IO, JOB_SPEC, outputs=["l1a_file"])))


class TestRoundTrip:
    def make(self, binding=None):
        op = generate_operation("job-l1a:v3.2", HYSDS_IO, JOB_SPEC, outputs=["l1a_file"])
        return workflow_with(op, {"l0b_file": "url"} if binding is None else binding)

    def test_passes_cwltool_validation(self):
        errors, warnings = validate_with_cwltool(self.make())
        assert errors == [], errors

    def test_compiles_with_registry_derived_from_same_hysds_io(self):
        info = {"params": HYSDS_IO["params"], "outputs": ["l1a_file"]}
        res = compile_cwl(dump(self.make()), job_type_lookup=lambda jt: info if jt == "job-l1a:v3.2" else None)
        assert res.ok, res.errors
        st = res.spec["steps"]["step"]
        assert st["job_type"] == "job-l1a:v3.2" and st["in"] == {"l0b_file": {"source": ["$.inputs.url"]}}
        assert st["op_defaults"] == {"ancillary_version": "v7", "dry_run": False}

    def test_required_param_must_still_be_bound(self):
        wf = self.make(binding={})
        assert "missing_input" in codes(compile_cwl(dump(wf)).errors)

    def test_snippet_text_round_trips_through_a_file_reference(self):
        from ruamel.yaml import YAML
        import io
        text = operation_snippet("job-l1a:v3.2", HYSDS_IO, JOB_SPEC, outputs=["l1a_file"])
        op = YAML(typ="safe").load(io.StringIO(text))
        wf = workflow_with("operation/job-l1a", {"l0b_file": "url"})
        res = compile_cwl(dump(wf), run_resolver=lambda ref: op)
        assert res.ok, res.errors


class TestExtra:
    def test_missing_extra_raises_clear_error(self, monkeypatch):
        for name in [m for m in sys.modules if m == "hysds_commons.cwl" or m.startswith("hysds_commons.cwl.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "cwltool", None)  # simulates ImportError for the extra
        with pytest.raises(ImportError) as ei:
            importlib.import_module("hysds_commons.cwl")
        assert "'cwl' extra" in str(ei.value) and "hysds_commons[cwl]" in str(ei.value)
        assert str(ei.value).startswith(EXTRA_MESSAGE.split(".")[0])

    def test_missing_cwl_utils_also_reported(self, monkeypatch):
        for name in [m for m in sys.modules if m == "hysds_commons.cwl" or m.startswith("hysds_commons.cwl.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "cwl_utils", None)
        with pytest.raises(ImportError, match="cwl"):
            importlib.import_module("hysds_commons.cwl")

    def test_rest_of_hysds_commons_does_not_import_cwltool(self):
        code = ("import sys, hysds_commons, hysds_commons.workflow_spec, hysds_commons.workflow_refs, "
                "hysds_commons.workflow_examples, hysds_commons.workflow_outputs; "
                "bad = [m for m in sys.modules if m.split('.')[0] in ('cwltool', 'cwl_utils', 'schema_salad')]; "
                "assert not bad, bad")
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=root))
        assert r.returncode == 0, r.stderr

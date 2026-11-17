"""Shared helpers for the CWL compiler tests (not a test module)."""

import copy
import json
import os

NS = "https://github.com/hysds/hysds_commons/cwl/v1#"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "cwl")


def op(job_type, inputs=None, outputs=None, **extra):
    d = {"class": "Operation", "inputs": inputs or {}, "outputs": outputs or {},
         "hints": {"hysds:JobType": {"job_type": job_type}}}
    d.update(extra)
    return d


def base():
    """A small valid workflow: a (job) -> b (conditional on a/ok), with a boolean flag input."""
    return {
        "cwlVersion": "v1.2", "class": "Workflow", "id": "wf-test",
        "$namespaces": {"hysds": NS, "s": "https://schema.org/"}, "s:version": "1.0.0",
        "requirements": {"ScatterFeatureRequirement": {}, "MultipleInputFeatureRequirement": {}},
        "inputs": {"x": "string", "flag": "boolean", "items": "string[]"},
        "outputs": {"r": {"type": "File", "outputSource": "a/f"}},
        "steps": {
            "a": {"run": op("job-a:v1", {"x": "string"}, {"f": "File", "ok": "boolean", "n": "int"}),
                  "in": {"x": "x"}, "out": ["f", "ok", "n"]},
        },
    }


def with_step(wf, sid, step):
    wf["steps"][sid] = step
    return wf


def dump(wf):
    return json.dumps(wf).encode()


def lookup_for(jobs):
    """jobs: {job_type: (inputs, outputs)} -> a job_type_lookup callable."""
    def lookup(jt):
        if jt not in jobs:
            return None
        v = jobs[jt]
        if isinstance(v, dict):
            return v
        i, o = v
        return {"inputs": i, "outputs": o}
    return lookup


def codes(items):
    return sorted({i["code"] for i in items})


def mutate(fn):
    wf = base()
    fn(wf)
    return wf


def norm_spec(spec):
    """Normalisation for golden comparison: drop the source block (hash/format differ), sort deps."""
    s = copy.deepcopy(spec)
    s.pop("source", None)
    for st in s["steps"].values():
        st["deps"] = sorted(st["deps"])
    return s


def golden():
    with open(os.path.join(FIXTURES, "radiometer-compiled-v2.golden.json")) as f:
        return json.load(f)


def radiometer_bytes():
    with open(os.path.join(FIXTURES, "radiometer-orbit-l2smp-v2.cwl"), "rb") as f:
        return f.read()

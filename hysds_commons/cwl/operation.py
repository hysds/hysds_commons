"""Operation generator: a registered job type -> an inline ``class: Operation`` CWL snippet.

Backs ``GET /workflow/cwl/operation/<job_type>`` (PLAN section 10, B17). The Operation lists every
hysds-io param (optional or defaulted ones are marked), the job's declared outputs, and the mandatory
``hysds:JobType`` hint, so authors (Figaro, an LLM, a ``run:`` reference) can paste it into a step.
"""

import json
import re

from hysds_commons.cwl.loader import HYSDS_NS

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
NAMESPACES = {"hysds": HYSDS_NS, "s": "https://schema.org/"}

# hysds-io param "type" -> CWL type (unknown or absent types are strings)
_TYPE_MAP = {
    "text": "string", "textarea": "string", "enum": "string", "date": "string", "datetime": "string",
    "email": "string", "number": "double", "int": "int", "integer": "int", "float": "double",
    "boolean": "boolean", "bool": "boolean", "object": "Any", "json": "Any",
}


def _cwl_type(param):
    return _TYPE_MAP.get(str(param.get("type") or "text").lower(), "string")


def _output_names(hysds_io, job_spec, outputs):
    raw = outputs
    if raw is None:
        for src in (hysds_io, job_spec):
            if isinstance(src, dict) and src.get("outputs") is not None:
                raw = src["outputs"]
                break
    names = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            names[k] = v if isinstance(v, str) else "File"
    else:
        for o in raw or []:
            if isinstance(o, str):
                names[o] = "File"
            elif isinstance(o, dict) and isinstance(o.get("name"), str):
                names[o["name"]] = o.get("type") if isinstance(o.get("type"), str) else "File"
    return names


def generate_operation(job_type, hysds_io=None, job_spec=None, outputs=None):
    """Return the Operation as a dict (the value to put under a step's ``run``).

    ``hysds_io`` is the job's hysds-io document (its ``params`` list), ``job_spec`` the job-spec
    (consulted for ``outputs`` when hysds-io has none). ``outputs`` overrides both: a list of names or a
    ``{name: cwl_type}`` map (the job writes them in ``_workflow_outputs.json``). Params that the system
    fills in (``from`` other than ``submitter``) and params with a default or ``optional`` are optional.
    """
    if not isinstance(job_type, str) or not job_type:
        raise ValueError("job_type is required")
    inputs = {}
    for p in (hysds_io or {}).get("params") or []:
        name = p.get("name") if isinstance(p, dict) else p
        if not isinstance(name, str) or not NAME_RE.match(name):
            continue
        t = _cwl_type(p) if isinstance(p, dict) else "string"
        p = p if isinstance(p, dict) else {}
        has_default = "default" in p and p["default"] is not None
        optional = bool(p.get("optional")) or p.get("from", "submitter") not in (None, "submitter")
        entry = {"type": t if (has_default or not optional) else t + "?"}
        if has_default:
            entry["default"] = p["default"]
        label = p.get("placeholder") or p.get("label")
        if isinstance(label, str) and label:
            entry["doc"] = label
        inputs[name] = entry
    outs = {n: {"type": t} for n, t in _output_names(hysds_io, job_spec, outputs).items() if NAME_RE.match(n)}
    return {
        "class": "Operation",
        "inputs": inputs,
        "outputs": outs,
        "hints": {"hysds:JobType": {"job_type": job_type}},
    }


def operation_snippet(job_type, hysds_io=None, job_spec=None, outputs=None):
    """The Operation as YAML text. Needs the workflow to declare ``$namespaces: {hysds: <HYSDS_NS>}``."""
    from ruamel.yaml import YAML
    import io

    op = generate_operation(job_type, hysds_io, job_spec, outputs)
    y = YAML(typ="safe", pure=True)
    y.default_flow_style = False
    buf = io.StringIO()
    y.dump(json.loads(json.dumps(op)), buf)
    return buf.getvalue()

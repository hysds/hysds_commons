"""CWL -> HySDS workflow spec 2.0 (PLAN sections 7 and 8).

``compile_cwl`` is the one entry point: load, cwltool-validate, apply the profile, check the registry,
emit the 2.0 JSON. It never raises for bad input; problems come back as structured
``{path, code, message}`` entries in ``CompileResult.errors`` / ``.warnings``.
"""

import math

from hysds_commons.cwl import profile as P
from hysds_commons.cwl.loader import (
    inline_run_refs, issue, load_document, validate_with_cwltool)

SCHEMA_VERSION = "2.0"
SOURCE_FORMAT = "cwl"


class CWLCompileError(Exception):
    """Raised by ``compile_cwl_or_raise``; ``errors`` is the structured error list."""

    def __init__(self, errors):
        self.errors = errors
        Exception.__init__(self, "; ".join("%s: %s" % (e.get("path") or "$", e["message"]) for e in errors))


class CompileResult(object):
    def __init__(self):
        self.ok = False
        self.errors = []
        self.warnings = []
        self.spec = None
        self.graph = None
        self.cwl = None      # the normalized (inlined, $schemas stripped) Workflow, to store beside the spec
        self.sha256 = None

    def as_dict(self):
        """The ``POST /workflow/cwl/validate`` body: ``{ok, warnings, errors, compiled?, graph?}``."""
        d = {"ok": self.ok, "warnings": self.warnings, "errors": self.errors}
        if self.ok:
            d["compiled"] = self.spec
            d["graph"] = self.graph
        return d


# ====================================================================================================
# HOOK: wire this to S1's validator (hysds_commons.workflow_spec2.validate_spec2) when the slices merge.
# It receives the compiled 2.0 spec and returns a list of error strings (empty list == valid).
# ====================================================================================================
def _validate_compiled(spec):
    """Final check of the compiled spec against the 2.0 schema and semantic rules."""
    from hysds_commons.workflow_spec2 import validate_spec2
    return validate_spec2(spec)


# ---------------------------------------------------------------------------------------------- emit


def _ref(src):
    return "$.inputs.%s" % src[1] if src[0] == "input" else "$.%s.%s" % (src[1], src[2])


def _src_path(src):
    return src[1] if src[0] == "input" else "%s/%s" % (src[1], src[2])


def _emit_inputs(model):
    out = {}
    for name, inp in model["inputs"].items():
        d = {"type": inp["type"].render()}
        if inp["has_default"]:
            d["default"] = inp["default"]
        if inp["label"]:
            d["label"] = inp["label"]
        out[name] = d
    return out


def _entry(e, warnings, step_id):
    d = {}
    if e["sources"]:
        d["source"] = [_ref(s) for s in e["sources"]]
    lm = e["link_merge"]
    if (e["pick"] == "all_non_null" and not e["list_source"] and lm is None and len(e["sources"]) == 1):
        lm = "merge_flattened"
        warnings.append(issue("steps.%s.in.%s" % (step_id, e["id"]), "normalized_pick",
                              "single-source pickValue: all_non_null was normalized to source: [..], "
                              "linkMerge: merge_flattened, pickValue: all_non_null (B23)"))
    if lm:
        d["link_merge"] = lm
    if e["pick"]:
        d["pick"] = e["pick"]
    if e["has_default"]:
        d["default"] = e["default"]
    return d


def _emit_step(model, st, warnings):
    sid = st["id"]
    levels = st["levels"]
    deps = []
    for e in st["in_entries"]:
        for s in e["sources"]:
            if s[0] == "step" and s[1] not in deps:
                deps.append(s[1])
    out = {}
    if st["kind"] == "halt":
        h = {"status": st["halt"]["status"]}
        if st["halt"]["reason"]:
            h["reason"] = st["halt"]["reason"]
        out["halt"] = h
        out["deps"] = deps
        if st["when_flag"]:
            out["when"] = _ref(P._flag_source(st))
        return out
    out["job_type"] = st["job_type"]
    q = P.effective(levels, "Queue")
    if q:
        out["queue"] = q["queue"]
    r = P.effective(levels, "Retry")
    if r:
        if "max_retries" in r:
            out["max_retries"] = r["max_retries"]
        if "retry_on" in r:
            out["retry_on"] = list(r["retry_on"])
    ttl = P.effective_std(levels, "ToolTimeLimit")
    if ttl and ttl.get("timelimit"):
        out["time_limit"] = int(math.ceil(ttl["timelimit"]))
    out["deps"] = deps
    if st["when_flag"]:
        out["when"] = _ref(P._flag_source(st))
    ins = {}
    for e in st["in_entries"]:
        if e["id"] not in st["op_inputs"]:
            continue  # the when flag (or any non-parameter input) is not passed to the job
        d = _entry(e, warnings, sid)
        if d:
            ins[e["id"]] = d
    out["in"] = ins
    defaults = {n: op["default"] for n, op in st["op_inputs"].items() if op["has_default"]}
    if defaults:
        out["op_defaults"] = defaults
    if st["scatter"]:
        out["scatter"] = {"input": st["scatter"], "threshold": round(1 - st["tolerance"] / 100.0, 6)}
        sc = P.effective(levels, "ScatterConnector")
        if sc:
            out["scatter"]["connector"] = {k: sc[k] for k in ("job_type", "queue") if sc.get(k)}
    tag = P.effective(levels, "Tag")
    if tag and tag.get("tag"):
        out["tag"] = tag["tag"]
    out["out"] = list(st["out"])
    return out


def emit_spec(model, sha256, warnings):
    spec = {"schema_version": SCHEMA_VERSION, "id": model["id"], "version": model["version"]}
    if model["label"]:
        spec["label"] = model["label"]
    if model["description"]:
        spec["description"] = model["description"]
    spec["on_error"] = model["on_error"]
    spec["inputs"] = _emit_inputs(model)
    spec["steps"] = {sid: _emit_step(model, st, warnings) for sid, st in model["steps"].items()}
    outs = {}
    for name, o in model["outputs"].items():
        d = {"type": o["type"].render(), "source": [_ref(s) for s in o["sources"]]}
        if o["link_merge"]:
            d["link_merge"] = o["link_merge"]
        if o["pick"]:
            d["pick"] = o["pick"]
        outs[name] = d
    spec["outputs"] = outs
    pm = {"inputs.%s" % n: "/inputs/%s" % n for n in model["inputs"]}
    for sid, st in model["steps"].items():
        pm["steps.%s" % sid] = "/steps/%s" % sid
        for e in st["in_entries"]:
            pm["steps.%s.in.%s" % (sid, e["id"])] = "/steps/%s/in/%s" % (sid, e["id"])
        if st["has_when"]:
            pm["steps.%s.when" % sid] = "/steps/%s/when" % sid
    for n in model["outputs"]:
        pm["outputs.%s" % n] = "/outputs/%s" % n
    spec["source"] = {"format": SOURCE_FORMAT, "sha256": sha256, "path_map": pm}
    return spec


def _graph(spec):
    nodes, edges = [], []
    for sid, st in spec["steps"].items():
        nodes.append({"id": sid, "kind": "halt" if "halt" in st else "job", "job_type": st.get("job_type"),
                      "when": st.get("when"), "scatter": "scatter" in st})
        for d in st["deps"]:
            edges.append({"from": d, "to": sid})
    return {"nodes": nodes, "edges": edges}


# ------------------------------------------------------------------------------------------- registry


def _norm_job_info(info):
    """Accept ``{inputs|params: [...], outputs: [...]}``; entries are names or hysds-io param dicts.
    Returns ``(inputs, outputs)``: ``inputs`` maps name -> required (bool); ``outputs`` is a set or None."""
    raw = info.get("inputs", info.get("params"))
    inputs = {}
    if isinstance(raw, dict):
        items = [(k, v if isinstance(v, dict) else {}) for k, v in raw.items()]
    else:
        items = []
        for it in raw or []:
            if isinstance(it, str):
                items.append((it, {}))
            elif isinstance(it, dict) and isinstance(it.get("name"), str):
                items.append((it["name"], it))
    for name, it in items:
        user = it.get("from", "submitter") in (None, "submitter")
        inputs[name] = bool(user and not it.get("optional") and "default" not in it and it.get("required", True))
    outs = info.get("outputs")
    if isinstance(outs, dict):
        outs = set(outs)
    elif isinstance(outs, (list, tuple, set)):
        outs = {o if isinstance(o, str) else o.get("name") for o in outs}
    else:
        outs = None
    return inputs, outs


def check_registry(model, lookup, errors):
    for sid, st in model["steps"].items():
        if st["kind"] != "job" or not st["job_type"]:
            continue
        jt = st["job_type"]
        base = "steps.%s.run.hints.hysds:JobType.job_type" % sid
        info = lookup(jt)
        if not info:
            errors.append(issue(base, "unknown_job_type", "job type '%s' is not registered" % jt))
            continue
        inputs, outs = _norm_job_info(info)
        decide = P.is_decide_job(jt)
        if decide:
            inputs = inputs or {"value": False, "cases": True, "otherwise": False}
        known = sorted(inputs)
        bound = {e["id"] for e in st["in_entries"] if e["sources"] or e["has_default"]}
        for name in st["op_inputs"]:
            if name not in inputs:
                errors.append(issue("steps.%s.run.inputs.%s" % (sid, name), "unknown_param",
                                    "Operation input '%s' is not a parameter of job type '%s' (parameters: %s)" % (
                                        name, jt, ", ".join(known) or "none")))
        for name, required in inputs.items():
            if required:
                declared = name in st["op_inputs"]
                satisfied = declared and (name in bound or st["op_inputs"][name]["has_default"])
                if not satisfied:
                    errors.append(issue("steps.%s.run.inputs.%s" % (sid, name), "missing_param",
                                        "job type '%s' requires parameter '%s'; declare it on the Operation and bind it in 'in'" % (jt, name)))
        if outs is not None and not decide:
            for name in sorted(set(st["op_outputs"]) | set(st["out"])):
                if name not in outs:
                    errors.append(issue("steps.%s.out" % sid, "unknown_output",
                                        "'%s' is not an output of job type '%s' (outputs: %s)" % (name, jt, ", ".join(sorted(outs)) or "none")))


def _allowed_connector_map(allowed):
    """Normalize ``allowed_connectors`` to ``{job_type: queue_or_None}``, or None (no check).

    Accepts the deployment's ``WORKFLOW_CONNECTORS`` shape (PLAN 11.2: ``{kind: {"default", "allowed":
    {job_type: queue}}}``; only the ``scatter`` kind applies to ``hysds:ScatterConnector``), a plain
    ``{job_type: queue}`` mapping, or an iterable of job types."""
    if allowed is None:
        return None
    if isinstance(allowed, dict):
        sc = allowed.get("scatter")
        if isinstance(sc, dict) and "allowed" in sc:  # the WORKFLOW_CONNECTORS shape
            allowed = sc["allowed"] or {}
        return {k: (v if isinstance(v, str) and v else None) for k, v in allowed.items()}
    return {jt: None for jt in allowed}


def check_connectors(model, allowed, errors):
    """``hysds:ScatterConnector`` job types must be in the deployment allowlist (code ``connector_not_allowed``)."""
    amap = _allowed_connector_map(allowed)
    if amap is None:
        return
    for sid, st in model["steps"].items():
        sc = P.effective(st["levels"], "ScatterConnector") if st.get("scatter") else None
        if sc and sc.get("job_type") not in amap:
            errors.append(issue("steps.%s.hints.hysds:ScatterConnector.job_type" % sid, "connector_not_allowed",
                                "scatter connector '%s' is not allowed on this deployment (allowed: %s)" % (
                                    sc.get("job_type"), ", ".join(sorted(amap)) or "none")))


def _fill_connector_queues(spec, allowed):
    """A connector hint without a queue gets the allowlist's queue for its job type (as v1 did)."""
    amap = _allowed_connector_map(allowed) or {}
    for st in spec["steps"].values():
        conn = (st.get("scatter") or {}).get("connector")
        if conn and not conn.get("queue") and amap.get(conn["job_type"]):
            conn["queue"] = amap[conn["job_type"]]


# --------------------------------------------------------------------------------------------- driver


def _split(items):
    errs, warns = [], []
    for i in items:
        d = {"path": i["path"], "code": i["code"], "message": i["message"]}
        (errs if i.get("severity") == "error" else warns).append(d)
    return errs, warns


def compile_cwl(source, job_type_lookup=None, run_resolver=None, allowed_connectors=None):
    """Compile CWL ``source`` (bytes or str) to a 2.0 spec.

    ``job_type_lookup(job_type) -> {"inputs"|"params": [...], "outputs": [...]} | None`` enables the
    registry checks (omit to skip them). ``run_resolver(ref) -> Operation dict | None`` inlines
    registry-generated ``run:`` references (B17). ``allowed_connectors`` is the deployment allowlist of
    ``hysds:ScatterConnector`` job types (mozart's ``WORKFLOW_CONNECTORS`` setting, or a list of job types);
    a job type outside it fails with ``connector_not_allowed``; ``None`` means no check. Returns a ``CompileResult``.
    """
    res = CompileResult()
    loaded = load_document(source)
    res.sha256 = loaded.sha256
    res.errors.extend(loaded.errors)
    res.warnings.extend(loaded.warnings)
    if loaded.doc is None:
        return res
    doc = inline_run_refs(loaded.doc, run_resolver)
    res.cwl = doc

    model, items = P.analyze(doc, None)
    perrs, pwarns = _split(items)
    res.errors.extend(perrs)
    res.warnings.extend(pwarns)

    cerrs, cwarns = validate_with_cwltool(doc)
    res.errors.extend(cerrs)
    null_errors = any(e["code"].startswith("null_unsafe") for e in perrs)
    for w in cwarns:
        if "may produce null" in w["message"] and not null_errors:
            continue  # the null-safety check proved every such edge safe
        res.warnings.append(w)

    if model is not None and job_type_lookup is not None:
        check_registry(model, job_type_lookup, res.errors)
    if model is not None:
        check_connectors(model, allowed_connectors, res.errors)

    if res.errors or model is None:
        return res

    spec = emit_spec(model, loaded.sha256, res.warnings)
    _fill_connector_queues(spec, allowed_connectors)
    for msg in _validate_compiled(spec):
        res.errors.append(issue("", "spec_invalid", str(msg)))
    if res.errors:
        return res
    res.spec = spec
    res.graph = _graph(spec)
    res.ok = True
    return res


def compile_cwl_or_raise(source, job_type_lookup=None, run_resolver=None, allowed_connectors=None):
    """Like ``compile_cwl`` but returns the spec dict, raising ``CWLCompileError`` on any error."""
    res = compile_cwl(source, job_type_lookup, run_resolver, allowed_connectors)
    if not res.ok:
        raise CWLCompileError(res.errors)
    return res.spec

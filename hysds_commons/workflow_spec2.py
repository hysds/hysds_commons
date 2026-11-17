"""Validator and graph helpers for ``workflow_spec`` documents, schema 2.0 (PLAN §7).

``validate_spec2`` returns *all* errors as strings (same shape as v1's ``validate_spec``: an empty
list means valid). It runs the JSON Schema in ``schemas/workflow-spec-2.0-schema.json`` and then the
semantic checks a schema cannot express (deps vs sources/``when``, acyclic graph, refs to declared
outputs, scatter, halts, workflow outputs, B20/B21).

Pure module: no OpenSearch, no celery, no I/O besides loading the schema file once.
"""

import json
import os
import re

import jsonschema

from hysds_commons.workflow_spec import WorkflowSpecError

SCHEMA_VERSION = "2.0"
SCHEMA_FILE = os.path.join(os.path.dirname(os.path.realpath(__file__)), "schemas", "workflow-spec-2.0-schema.json")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_STEP_IDS = ("inputs", "scatter", "finalize")
ON_ERROR_MODES = ("kill", "drain", "continue")
DEFAULT_ON_ERROR = "kill"
LINK_MERGE = ("merge_nested", "merge_flattened")
PICK = ("first_non_null", "the_only_non_null", "all_non_null")

_schema_cache = []


def load_schema():
    """The parsed 2.0 JSON Schema (cached)."""
    if not _schema_cache:
        with open(SCHEMA_FILE) as f:
            _schema_cache.append(json.load(f))
    return _schema_cache[0]


# --------------------------------------------------------------------------- small helpers


def is_halt(step):
    return isinstance(step, dict) and "halt" in step


def split_ref(ref):
    """``'$.a.b'`` -> ``('a', 'b')``; ``None`` if malformed. ``a == 'inputs'`` is a workflow input."""
    if not isinstance(ref, str) or not ref.startswith("$."):
        return None
    parts = ref[2:].split(".")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def step_source_refs(step):
    """Every ``$.`` ref in a step's ``in`` sources, in order."""
    refs = []
    for entry in ((step or {}).get("in") or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("source"), list):
            refs.extend(entry["source"])
    return refs


def compute_deps(step):
    """Sorted distinct step ids named in a step's ``in`` sources plus its ``when`` flag
    (workflow inputs are not deps, PLAN §6.2). This is what ``step["deps"]`` must equal."""
    deps = set()
    refs = step_source_refs(step)
    if isinstance(step, dict) and step.get("when"):
        refs = refs + [step["when"]]
    for r in refs:
        p = split_ref(r)
        if p and p[0] != "inputs":
            deps.add(p[0])
    return sorted(deps)


def dependents_index(spec):
    """Reverse of ``deps``: ``{step_id: sorted [steps that list it in deps]}``. Every step id is a key.
    Uses the stored ``deps`` (the consumer never recomputes them)."""
    steps = (spec or {}).get("steps") or {}
    rev = {sid: [] for sid in steps}
    for sid, step in steps.items():
        for d in step.get("deps") or []:
            if d in rev and sid not in rev[d]:
                rev[d].append(sid)
    return {k: sorted(v) for k, v in rev.items()}


def topo_order(spec):
    """Step ids in a dependency-respecting order (Kahn, ties broken by id). Raises ``ValueError`` on a cycle."""
    steps = spec["steps"]
    indeg = {sid: len([d for d in (s.get("deps") or []) if d in steps]) for sid, s in steps.items()}
    rev = dependents_index(spec)
    ready = sorted(s for s, n in indeg.items() if n == 0)
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in rev[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                ready.append(m)
        ready.sort()
    if len(order) != len(steps):
        raise ValueError("cycle among steps %s" % sorted(set(steps) - set(order)))
    return order


def spec_doc_id(spec):
    """OpenSearch doc id of a spec: ``<id>:<version>`` (same as v1)."""
    return "%s:%s" % (spec["id"], spec["version"])


def step_queue(spec, step_id, recommended_queue=None):
    """Queue for a step: the step's own, else the job-spec's recommended queue (B11)."""
    return (spec["steps"][step_id].get("queue")) or recommended_queue


def retry_policy2(spec, step_id):
    """Effective ``{"max_retries": int, "retry_on": [str]}`` for a job step (0 / [] when unset)."""
    step = spec["steps"][step_id]
    return {"max_retries": step.get("max_retries", 0), "retry_on": list(step.get("retry_on") or [])}


# --------------------------------------------------------------------------- schema errors


def _where(error):
    path = ".".join(str(p) for p in error.absolute_path)
    return path or "spec"


def _schema_errors(spec, errors):
    validator = jsonschema.Draft7Validator(load_schema())
    found = sorted(validator.iter_errors(spec), key=lambda e: [str(p) for p in e.absolute_path])
    for e in found:
        errors.append("%s: %s" % (_where(e), e.message))


# --------------------------------------------------------------------------- semantic checks


def _check_ref(spec, ref, where, owner, errors, need_dep=True):
    """Validate one ref. ``owner`` is the owning step id (None for workflow outputs)."""
    p = split_ref(ref)
    if p is None:
        return  # the schema already reported it
    head, name = p
    steps = spec["steps"]
    if head == "inputs":
        if name not in (spec.get("inputs") or {}):
            errors.append("%s: %s refers to undeclared workflow input '%s'" % (where, ref, name))
        return
    if head not in steps:
        errors.append("%s: %s refers to unknown step '%s'" % (where, ref, head))
        return
    target = steps[head]
    if is_halt(target):
        errors.append("%s: %s refers to halt step '%s', which has no outputs" % (where, ref, head))
    elif name not in (target.get("out") or []):
        errors.append("%s: %s: step '%s' does not declare output '%s' (declared: %s)"
                      % (where, ref, head, name, sorted(target.get("out") or [])))


def _check_entry_shape(entry, where, errors):
    # pick/link_merge without a source is meaningless
    if isinstance(entry, dict) and "source" not in entry and ("pick" in entry or "link_merge" in entry):
        errors.append("%s: 'pick'/'link_merge' require a 'source'" % where)


def _check_step(spec, sid, step, errors):
    where = "steps.%s" % sid
    steps = spec["steps"]
    if sid in RESERVED_STEP_IDS:
        errors.append("%s: step id '%s' is reserved" % (where, sid))
    halt = is_halt(step)
    deps = step.get("deps")
    if halt and "job_type" in step:
        errors.append("%s: a halt step must not have a job_type" % where)
    if isinstance(deps, list):
        for d in deps:
            if d not in steps:
                errors.append("%s.deps: unknown step '%s'" % (where, d))
            elif is_halt(steps[d]):
                errors.append("%s.deps: '%s' is a halt step and cannot be depended on" % (where, d))
        expected = compute_deps(step)
        if sorted(set(deps)) != expected and all(isinstance(d, str) for d in deps):
            missing = sorted(set(expected) - set(deps))
            extra = sorted(set(deps) - set(expected))
            msg = "deps do not match the steps referenced in 'in' sources and 'when'"
            if missing:
                msg += "; missing %s" % missing
            if extra:
                msg += "; unreferenced %s" % extra
            errors.append("%s.deps: %s" % (where, msg))
    ins = step.get("in") if isinstance(step.get("in"), dict) else {}
    for name, entry in ins.items():
        ewhere = "%s.in.%s" % (where, name)
        _check_entry_shape(entry, ewhere, errors)
        if isinstance(entry, dict) and isinstance(entry.get("source"), list):
            for ref in entry["source"]:
                _check_ref(spec, ref, ewhere, sid, errors)
    if step.get("when") is not None:
        _check_ref(spec, step["when"], "%s.when" % where, sid, errors)
    if halt:
        return
    scatter = step.get("scatter")
    if isinstance(scatter, dict):
        if scatter.get("input") not in ins:
            errors.append("%s.scatter: input '%s' is not an input of the step (inputs: %s)"
                          % (where, scatter.get("input"), sorted(ins)))
        if "when" in step:
            errors.append("%s: 'scatter' and 'when' cannot be combined on one step (B20)" % where)
        thr = scatter.get("threshold")
        if thr is not None and isinstance(thr, (int, float)) and not isinstance(thr, bool) and not (0 < thr <= 1):
            errors.append("%s.scatter.threshold: must be in (0, 1]" % where)


def _check_cycles(spec, errors):
    steps = spec["steps"]
    try:
        topo_order({"steps": {k: {"deps": [d for d in (v.get("deps") or []) if isinstance(d, str)]}
                              for k, v in steps.items()}})
    except ValueError as e:
        errors.append("steps: dependency graph has a %s" % e)


def _check_outputs(spec, errors):
    outs = spec.get("outputs") or {}
    for name, decl in outs.items():
        where = "outputs.%s" % name
        _check_entry_shape(decl, where, errors)
        if isinstance(decl, dict) and isinstance(decl.get("source"), list):
            for ref in decl["source"]:
                _check_ref(spec, ref, where, None, errors)
    # B21: a halt with status completed makes every workflow output optional
    if any(is_halt(s) and (s["halt"] or {}).get("status") == "completed" for s in spec["steps"].values()):
        for name, decl in outs.items():
            t = decl.get("type") if isinstance(decl, dict) else None
            if not (isinstance(t, str) and t.endswith("?")):
                errors.append("outputs.%s: must have an optional type ('?') because a halt has status 'completed' (B21)" % name)


def validate_spec2(spec):
    """Return the list of ALL validation errors for a 2.0 ``spec`` (empty list == valid)."""
    if not isinstance(spec, dict):
        return ["spec: must be a JSON object"]
    errors = []
    if spec.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version: must be %r, got %r" % (SCHEMA_VERSION, spec.get("schema_version")))
    _schema_errors(spec, errors)
    steps = spec.get("steps")
    if isinstance(steps, dict) and steps and all(isinstance(s, dict) for s in steps.values()):
        for sid, step in steps.items():
            _check_step(spec, sid, step, errors)
        _check_cycles(spec, errors)
        if isinstance(spec.get("outputs"), dict):
            _check_outputs(spec, errors)
    # de-duplicate (schema const and explicit check both flag schema_version) keeping order
    seen, out = set(), []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def check_spec2(spec):
    """Raise :class:`WorkflowSpecError` if ``spec`` is invalid."""
    errors = validate_spec2(spec)
    if errors:
        raise WorkflowSpecError(errors)
    return spec

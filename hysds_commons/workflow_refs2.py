"""Schema 2.0 input resolution (PLAN §6.4, pull model) as pure functions.

``settled`` is ``{step_id: {"state": "completed" | "skipped", "outputs": {name: value}}}`` (the shape of
the run's recorded step results). The three cases are kept apart:

* step *absent* from ``settled``: not settled yet, a ``WorkflowRefError`` (engine bug or a bad ref);
* step ``skipped``: every output is ``None`` (B3, no cascade);
* step ``completed``: ``outputs[name]`` is the value (may legitimately be ``None``, e.g. a failed
  scatter item); a missing ``name`` is a ``WorkflowRefError`` (the job did not write it).

Order per input (§6.4): link_merge -> pick -> step ``default`` -> ``op_defaults``. A step default
never rescues ``first_non_null`` / ``the_only_non_null`` errors; ``all_non_null`` never yields null.
A value still null after the chain stays ``None`` in the result (the job-spec default, applied at
submit, is the last link). All failures raise ``WorkflowRefError`` (run error_class ``input_resolution``).
"""

import copy
import re

from hysds_commons.workflow_refs import WorkflowRefError

_WHEN_INPUT_RE = re.compile(r"^\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_-]*)\s*\)$")
_REF_RE = re.compile(r"^\$\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")
_PRIMITIVES = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "long": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "double": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "File": lambda v: isinstance(v, (str, dict)),
    "Directory": lambda v: isinstance(v, (str, dict)),
    "Any": lambda v: True,
}



def flatten_files(d):
    """A File/Directory object (also inside arrays) flattened to its location (or path); other values unchanged.

    Kept here (not in ``hysds_commons.cwl``) so the runtime never needs the optional ``cwl`` extra;
    the CWL compiler imports it from this module.
    """
    if isinstance(d, list):
        return [flatten_files(x) for x in d]
    if isinstance(d, dict) and d.get("class") in ("File", "Directory"):
        return d.get("location") or d.get("path")
    return d

def lookup_ref(ref, settled, run_inputs):
    """Value of one ``$.inputs.x`` / ``$.step.out`` ref (deep-copied)."""
    m = _REF_RE.match(ref) if isinstance(ref, str) else None
    if not m:
        raise WorkflowRefError("malformed ref %r: expected '$.inputs.<name>' or '$.<step>.<output>'" % (ref,))
    head, name = m.groups()
    if head == "inputs":
        if name not in (run_inputs or {}):
            raise WorkflowRefError("cannot resolve %s: unknown run input '%s'" % (ref, name))
        return copy.deepcopy(run_inputs[name])
    rec = (settled or {}).get(head)
    if rec is None:
        raise WorkflowRefError("cannot resolve %s: step '%s' has not settled" % (ref, head))
    state = rec.get("state")
    if state == "skipped":
        return None
    if state != "completed":
        raise WorkflowRefError("cannot resolve %s: step '%s' is in state %r, not completed or skipped" % (ref, head, state))
    outs = rec.get("outputs") or {}
    if name not in outs:
        raise WorkflowRefError("cannot resolve %s: step '%s' has no output '%s' (has: %s)" % (ref, head, name, sorted(outs)))
    return copy.deepcopy(outs[name])


def link_merge(values, mode):
    """CWL linkMerge over the list of source values.

    ``mode`` None: one source -> the value itself, several -> nested (list of values).
    ``merge_nested``: always a list of the values. ``merge_flattened``: list values are concatenated,
    other values (None included) are appended as single elements."""
    if mode is None:
        mode = "merge_nested" if len(values) > 1 else None
    if mode is None:
        return values[0]
    if mode == "merge_nested":
        return list(values)
    if mode == "merge_flattened":
        flat = []
        for v in values:
            if isinstance(v, list):
                flat.extend(v)
            else:
                flat.append(v)
        return flat
    raise WorkflowRefError("unknown link_merge %r" % (mode,))


def pick_value(value, mode, what="input", null_ok=False):
    """CWL pickValue over a list ``value``; CWL error semantics. With ``null_ok`` (an optional run output,
    B21) an all-null ``first_non_null`` / ``the_only_non_null`` gives ``None`` instead of an error; more
    than one non-null value for ``the_only_non_null`` is still an error."""
    if not isinstance(value, list):
        raise WorkflowRefError("%s: pick %r needs a list, got %s" % (what, mode, type(value).__name__))
    non_null = [v for v in value if v is not None]
    if mode == "first_non_null":
        if not non_null and null_ok:
            return None
        if not non_null:
            raise WorkflowRefError("%s: first_non_null found only null values" % what)
        return non_null[0]
    if mode == "the_only_non_null":
        if not non_null and null_ok:
            return None
        if len(non_null) != 1:
            raise WorkflowRefError("%s: the_only_non_null needs exactly one non-null value, found %d" % (what, len(non_null)))
        return non_null[0]
    if mode == "all_non_null":
        return non_null
    raise WorkflowRefError("%s: unknown pick %r" % (what, mode))


def resolve_entry(entry, settled, run_inputs, what="input", null_ok=False):
    """Resolve one ``in`` / workflow-output entry through link_merge -> pick -> ``default``.
    (``op_defaults`` is applied by :func:`resolve_step_inputs`.) Returns ``None`` for a null result.
    ``null_ok`` is only passed for optional run outputs (see :func:`pick_value`); step inputs never set it."""
    sources = entry.get("source")
    value = None
    if sources:
        value = link_merge([lookup_ref(r, settled, run_inputs) for r in sources], entry.get("link_merge"))
        if entry.get("pick"):
            value = pick_value(value, entry["pick"], what, null_ok=null_ok)
    if value is None and "default" in entry:
        value = copy.deepcopy(entry["default"])
    return value


def resolve_step_inputs(step, settled, run_inputs):
    """Resolved ``{input: value}`` of a job step. Operation defaults (``op_defaults``) fill nulls and
    inputs absent from ``in``. Raises ``WorkflowRefError`` on any resolution error."""
    out = {}
    for name, entry in (step.get("in") or {}).items():
        out[name] = resolve_entry(entry, settled, run_inputs, what="input '%s'" % name)
    for name, default in (step.get("op_defaults") or {}).items():
        if out.get(name) is None:
            out[name] = copy.deepcopy(default)
    return out


def _is_optional(t):
    return isinstance(t, str) and t.endswith("?")


def resolve_outputs(spec, settled, run_inputs):
    """Finalize (§6.7): workflow ``outputs`` resolved from ``settled``. A null in a non-optional
    output (declared type without ``?``) raises ``WorkflowRefError`` (error_class ``output_resolution``)."""
    result = {}
    for name, decl in (spec.get("outputs") or {}).items():
        value = resolve_entry(decl, settled, run_inputs, what="output '%s'" % name,
                              null_ok=_is_optional(decl.get("type")))
        if value is None and decl.get("type") is not None and not _is_optional(decl["type"]):
            raise WorkflowRefError("output '%s' is null but its type %r is not optional" % (name, decl["type"]))
        result[name] = value
    return result


def evaluate_when(expr, resolved_inputs, settled=None, run_inputs=None):
    """Evaluate a ``when`` to ``True``/``False``.

    * ``$(inputs.x)``: the boolean form, read from ``resolved_inputs``;
    * ``$.step.out`` / ``$.inputs.x`` (the compiled 2.0 form): read through :func:`lookup_ref`
      from ``settled`` / ``run_inputs`` (a skipped step's flag is null).

    Anything that is not exactly a boolean, ``None`` included, raises ``WorkflowRefError`` (CWL:
    ``when`` must evaluate to a boolean). No JS, no operators."""
    if not isinstance(expr, str):
        raise WorkflowRefError("when must be a string, got %r" % (expr,))
    m = _WHEN_INPUT_RE.match(expr.strip())
    if m:
        name = m.group(1)
        if name not in (resolved_inputs or {}):
            raise WorkflowRefError("when %s: no input '%s'" % (expr, name))
        value = resolved_inputs[name]
    elif _REF_RE.match(expr.strip()):
        value = lookup_ref(expr.strip(), settled, run_inputs)
    else:
        raise WorkflowRefError("unsupported when expression %r: only '$(inputs.<name>)' or a '$.<step>.<output>' ref" % expr)
    if not isinstance(value, bool):
        raise WorkflowRefError("when %s must be a boolean, got %s" % (expr, "null" if value is None else repr(value)))
    return value


def resolve_run_inputs(spec, inputs):
    """Apply defaults and type-check run inputs against the 2.0 ``inputs`` declarations (CWL type
    strings: ``string``, ``int``, ``long``, ``float``, ``double``, ``boolean``, ``File``, ``Directory``,
    ``Any``, optional ``?``, arrays ``[]``). Returns ``(resolved_inputs, errors)``."""
    declared = spec.get("inputs") or {}
    inputs = {} if inputs is None else inputs
    if not isinstance(inputs, dict):
        return {}, ["inputs: must be an object"]
    errors, resolved = [], {}
    for name in sorted(set(inputs) - set(declared)):
        errors.append("input '%s': unknown input (declared: %s)" % (name, sorted(declared)))
    for name, decl in declared.items():
        typ = decl["type"]
        if name in inputs:
            value = inputs[name]
        elif "default" in decl:
            value = copy.deepcopy(decl["default"])
        elif _is_optional(typ):
            value = None
        else:
            errors.append("input '%s': required but not provided" % name)
            continue
        err = _check_type(value, typ)
        if err:
            errors.append("input '%s': %s" % (name, err))
        else:
            # §4: File / Directory objects ({class, location|path}) reach jobs as URL strings
            flat = flatten_files(value)
            if _has_file_object(value) and _contains_none(flat) and not _contains_none(value):
                errors.append("input '%s': File/Directory object needs a 'location' (or 'path')" % name)
            else:
                resolved[name] = flat
    return resolved, errors


def _contains_none(v):
    return v is None or (isinstance(v, list) and any(_contains_none(x) for x in v))


def _has_file_object(v):
    if isinstance(v, list):
        return any(_has_file_object(x) for x in v)
    return isinstance(v, dict) and v.get("class") in ("File", "Directory")


def _check_type(value, typ):
    optional = _is_optional(typ)
    base = typ[:-1] if optional else typ
    if value is None:
        return None if optional else "null is not allowed for type %s" % typ
    if base.endswith("[]"):
        if not isinstance(value, list):
            return "expected %s, got %s" % (typ, type(value).__name__)
        for i, item in enumerate(value):
            err = _check_type(item, base[:-2])
            if err:
                return "item %d: %s" % (i, err)
        return None
    check = _PRIMITIVES.get(base)
    if check is None:
        return "unsupported type %s" % typ
    return None if check(value) else "expected %s, got %r" % (typ, value)

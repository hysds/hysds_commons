"""Pure ``$``-reference resolver for workflow specs (contract §1 rule 7, §5).

A ref is a string starting with ``$.``; three forms exist::

    $.inputs.<name>        -> ("inputs", name)
    $.<node>.<output>      -> ("node", node, output)
    $.scatter.item         -> ("scatter_item",)

Any other string is a literal. Refs may sit anywhere inside nested dicts/lists.

first_of (D5): ``{"first_of": ["$.a.x", "$.b.y", ..., "$.inputs.z"]}`` is a value (allowed in a job's
``in``, a pass ``bind`` value and spec ``outputs``) that resolves to the first entry whose node is
``completed`` (``context["completed"]``); ``$.inputs.*`` entries always resolve. Skipped/pending/other
nodes fall through. A completed node whose output is missing or null is an error (no fall-through).
If nothing resolves: ``WorkflowRefError``, or ``None`` with ``allow_skipped=True`` (spec outputs).

Skipped nodes (routing): ``build_context`` records nodes whose status state is ``skipped`` under
``context["skipped"]``; ``resolve(value, context, allow_skipped=True)`` then resolves a ref to such a
node as ``None``. Only the finalize path (spec ``outputs``) passes ``allow_skipped=True``; node
inputs/binds keep the strict behaviour.

This module is deliberately pure: no OpenSearch, no celery, no I/O.
"""

import copy

REF_PREFIX = "$."

# sentinel: "no scatter item supplied" (None is a legitimate item value)
_MISSING = object()

_VALUE_SLOTS = ("value_s", "value_d", "value_b")


class WorkflowRefError(Exception):
    """Raised for a malformed ref, a malformed outputs list, or an unresolvable ref."""


class WorkflowRoutingError(Exception):
    """A switch/gate could not be routed: a type/operator error while evaluating a ``when`` tree
    (e.g. ``lt`` over a string and a number), a malformed switch node, or an inconsistent
    ``nodes_to_skip`` request. Lives here, next to :class:`WorkflowRefError`, so consumers get
    both routing failure types from one import. A missing/null ref is a ``WorkflowRefError``
    (run error_class ``input_resolution``); this one maps to error_class ``routing``."""


def is_ref(v):
    """True if ``v`` is a string starting with ``$.``."""
    return isinstance(v, str) and v.startswith(REF_PREFIX)


def parse_ref(s):
    """Parse a ref string into a tuple; raises WorkflowRefError if malformed."""
    if not is_ref(s):
        raise WorkflowRefError("not a $-ref (must be a string starting with '$.'): %r" % (s,))
    parts = s[len(REF_PREFIX):].split(".")
    if len(parts) != 2 or not all(parts):
        raise WorkflowRefError(
            "malformed ref %r: expected '$.inputs.<name>', '$.<node>.<output>' or '$.scatter.item'" % s
        )
    first, second = parts
    if first == "scatter":
        if second != "item":
            raise WorkflowRefError("malformed ref %r: only '$.scatter.item' is valid under 'scatter'" % s)
        return ("scatter_item",)
    if first == "inputs":
        return ("inputs", second)
    return ("node", first, second)


def outputs_to_dict(typed_kv_list):
    """Convert an ADR-003 typed KV list to ``{name: value}`` (value from the set slot)."""
    if typed_kv_list is None:
        return {}
    if not isinstance(typed_kv_list, list):
        raise WorkflowRefError("outputs must be a list of typed entries, got %s" % type(typed_kv_list).__name__)
    result = {}
    for i, entry in enumerate(typed_kv_list):
        if not isinstance(entry, dict):
            raise WorkflowRefError("outputs[%d] must be an object, got %s" % (i, type(entry).__name__))
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise WorkflowRefError("outputs[%d] has no valid 'name'" % i)
        slots = [s for s in _VALUE_SLOTS if s in entry]
        if len(slots) != 1:
            raise WorkflowRefError(
                "outputs[%d] ('%s') must set exactly one of %s, found %s" % (i, name, list(_VALUE_SLOTS), slots)
            )
        result[name] = copy.deepcopy(entry[slots[0]])
    return result


def _event_outputs(event):
    wo = event.get("workflow_outputs")
    if wo is None:
        return None
    if isinstance(wo, dict):
        # whole _workflow_outputs.json document: identified by its "version" key, so
        # an already-converted dict with an output literally named "outputs" isn't misread
        if "version" in wo and isinstance(wo.get("outputs"), list):
            return outputs_to_dict(wo["outputs"])
        return copy.deepcopy(wo)  # already converted
    return outputs_to_dict(wo)


def build_context(status_doc, event=None):
    """Build the §5 resolution context.

    ``{"inputs": {...}, "nodes": {node: {output: value}}}``. Only nodes whose status
    entry has an ``outputs`` dict appear. The event's ``workflow_outputs`` overlay (win over)
    the status outputs for ``event["step_id"]``. ``context["completed"]`` (when non-empty) holds the ids of
    ``completed`` nodes plus the event's node (for ``first_of``).

    Skipped nodes (``state == "skipped"``, written by a switch for untaken arms): when at least
    one exists the context gains ``"skipped": {node_id, ...}`` (key absent otherwise, so the
    shape is unchanged for runs without routing). Node-input resolution is unaffected (a ref to a
    skipped node still raises "has no outputs yet"); only ``resolve(..., allow_skipped=True)``,
    used for spec ``outputs`` at finalize, turns such a ref into ``None``.
    """
    status_doc = status_doc or {}
    nodes, skipped, completed = {}, set(), set()
    for nid, n in (status_doc.get("nodes") or {}).items():
        if isinstance(n, dict) and isinstance(n.get("outputs"), dict):
            nodes[nid] = copy.deepcopy(n["outputs"])
        if isinstance(n, dict) and n.get("state") == "skipped":
            skipped.add(nid)
        if isinstance(n, dict) and n.get("state") == "completed":
            completed.add(nid)
    if event:
        step = event.get("step_id")
        ev_out = _event_outputs(event)
        if step and ev_out is not None:
            merged = nodes.get(step, {})
            merged.update(ev_out)
            nodes[step] = merged
        if step and event.get("status") in (None, "job-completed", "completed"):
            completed.add(step)  # the event's node has just completed (status doc may lag behind)
    ctx = {"inputs": copy.deepcopy(status_doc.get("inputs") or {}), "nodes": nodes}
    if skipped:
        ctx["skipped"] = skipped
    if completed:
        ctx["completed"] = completed
    return ctx


def _lookup(ref, context, scatter_item, allow_skipped=False):
    parsed = parse_ref(ref)
    if parsed[0] == "scatter_item":
        if scatter_item is _MISSING:
            raise WorkflowRefError("cannot resolve %s: '$.scatter.item' is only valid inside a scatter child" % ref)
        return scatter_item
    if parsed[0] == "inputs":
        inputs = context.get("inputs") or {}
        if parsed[1] not in inputs:
            raise WorkflowRefError("cannot resolve %s: unknown run input '%s'" % (ref, parsed[1]))
        return inputs[parsed[1]]
    _, node, output = parsed
    if allow_skipped and node in (context.get("skipped") or ()):
        return None
    nodes = context.get("nodes") or {}
    if node not in nodes:
        raise WorkflowRefError("cannot resolve %s: node '%s' has no outputs yet" % (ref, node))
    if output not in nodes[node]:
        raise WorkflowRefError(
            "cannot resolve %s: node '%s' has no output '%s' (has: %s)" % (ref, node, output, sorted(nodes[node]))
        )
    return nodes[node][output]


def is_first_of(v):
    """True for a dict with a ``first_of`` key (a well-formed one is ``{"first_of": [ref, ref, ...]}``)."""
    return isinstance(v, dict) and "first_of" in v


def _resolve_first_of(value, context, allow_skipped):
    entries = value.get("first_of")
    if len(value) != 1 or not isinstance(entries, list) or not entries:
        raise WorkflowRefError("malformed first_of %r: expected {\"first_of\": [ref, ...]}" % (value,))
    completed = context.get("completed")
    nodes = context.get("nodes") or {}
    for ref in entries:
        parsed = parse_ref(ref)  # raises WorkflowRefError for a literal / malformed entry
        if parsed[0] == "inputs":
            return copy.deepcopy(_lookup(ref, context, _MISSING))
        if parsed[0] != "node":
            raise WorkflowRefError("first_of entry %r must be '$.<node>.<output>' or '$.inputs.<name>'" % (ref,))
        node, output = parsed[1], parsed[2]
        done = (node in completed) if completed is not None else (node in nodes)
        if not done:
            continue  # skipped / pending / running: fall through
        out = (nodes.get(node) or {}).get(output)
        if out is None:
            raise WorkflowRefError(
                "cannot resolve first_of entry %s: node '%s' completed but has no (or a null) output '%s'"
                % (ref, node, output))
        return copy.deepcopy(out)
    if allow_skipped:
        return None
    raise WorkflowRefError("cannot resolve first_of %s: none of its nodes completed" % (entries,))


def resolve(value, context, scatter_item=_MISSING, allow_skipped=False):
    """Recursively resolve refs in a literal / ref / dict / list. Returns a fresh copy.

    ``allow_skipped=True`` (spec ``outputs`` at finalize only): a ref to a node listed in
    ``context["skipped"]`` resolves to ``None`` instead of raising."""
    if is_ref(value):
        return copy.deepcopy(_lookup(value, context, scatter_item, allow_skipped))
    if is_first_of(value):
        return _resolve_first_of(value, context, allow_skipped)
    if isinstance(value, dict):
        return {k: resolve(v, context, scatter_item, allow_skipped) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, context, scatter_item, allow_skipped) for v in value]
    return copy.deepcopy(value)


def resolve_inputs(node, context, scatter_item=_MISSING):
    """Resolved ``in`` dict of a job node."""
    return resolve(node.get("in") or {}, context, scatter_item)


def resolve_binds(pass_node, context):
    """``{param: value}`` for a pass node; bind key ``"<next>.<param>"`` -> ``param``."""
    result = {}
    for key, ref in (pass_node.get("bind") or {}).items():
        if not isinstance(key, str) or "." not in key or not key.split(".", 1)[0] or not key.split(".", 1)[1]:
            raise WorkflowRefError("malformed bind key %r: expected '<next_node>.<param>'" % (key,))
        result[key.split(".", 1)[1]] = resolve(ref, context)
    return result

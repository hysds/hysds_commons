"""Validator and graph helpers for ``workflow_spec`` documents (contract §1).

``validate_spec`` enforces validation rules 1-9 and returns *all* errors. Specs are plain
dicts; the "schema" is the Python code below (no JSON-schema file, no extra dependency).

v1.2 routing: ``switch`` / ``gate`` / ``halt`` kinds (see PLAN-workflow-switch-v1.2.md). Refs are
checked against strict *dominators* (a ref'd node must lie on every path from the entry), fan-in is
allowed only as an exclusive rejoin of different arms of one switch, and the pure helpers
``successors``, ``switch_arms``, ``dominators``, ``evaluate_switch`` and ``nodes_to_skip`` serve the
switch connector and the consumer. ``WorkflowRoutingError`` lives in ``workflow_refs`` (next to
``WorkflowRefError``) and is re-exported here.

Pure module: no OpenSearch, no celery, no I/O.
"""

import copy
import re

from hysds_commons.workflow_refs import WorkflowRefError, WorkflowRoutingError, is_ref, parse_ref, resolve

SCHEMA_VERSION = "1.0"
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_RE = NODE_ID_RE  # input / output / param names

# Optional ``tag`` on job nodes: the Figaro tag of the job(s) the node submits (see ``job_tag``).
# Scatter children are job nodes, so a child node's ``tag`` is applied to every child job.
# Pass/scatter/gather connector nodes take no tag; their jobs are tagged with the node id.
TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

SUPPORTED_KINDS = ("job", "pass", "scatter", "gather", "switch", "gate", "halt")
UNSUPPORTED_KINDS = ()  # kept for importers; every kind is supported since v1.2
ARM_KINDS = ("switch", "gate")  # kinds that route through arms instead of ``next``
HALT_STATUSES = ("failed", "completed")
HALT_REASON_MAX = 512
WHEN_MAX_DEPTH = 8
WHEN_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in")
DEFAULT_LABEL = "default"
# node ids that may not be authored: ref namespaces and internal connectors
RESERVED_NODE_IDS = ("inputs", "scatter", "finalize", "halt", "_finalize", "_halt")

INPUT_TYPES = ("string", "int", "float", "bool", "string[]", "int[]", "float[]")

TOP_FIELDS = {"id", "version", "schema_version", "label", "description", "defaults", "inputs", "entry", "nodes", "outputs"}
DEFAULT_FIELDS = {"queue", "timeout_hours", "max_retries", "retry_on"}
JOB_OPTIONAL = {"queue", "max_retries", "retry_on", "timeout_hours"}  # timeout_hours is rejected with a specific message
# free-text documentation fields allowed on every node kind
NODE_DOC_FIELDS = {"label", "description"}
# pluggable connector fields (contract §11); not allowed on job nodes
CONNECTOR_FIELDS = {"connector", "connector_queue"}
PLUGGABLE_KINDS = ("pass", "scatter", "gather", "switch", "gate")
# connector config entry used per pluggable kind when it differs from the kind (gate uses switch's)
CONNECTOR_CONFIG_KIND = {"gate": "switch"}
NODE_FIELDS = {
    "job": {"kind", "run", "in", "out", "next", "tag"} | JOB_OPTIONAL | NODE_DOC_FIELDS,
    "pass": {"kind", "bind", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "scatter": {"kind", "child", "over", "completion", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "gather": {"kind", "collect", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "switch": {"kind", "cases", "on", "choices", "default"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "gate": {"kind", "when", "on_true", "on_false"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "halt": {"kind", "status", "reason"} | NODE_DOC_FIELDS,
}

# Deployment allowlist default (contract §11.2); mozart's WORKFLOW_CONNECTORS setting mirrors it.
DEFAULT_WORKFLOW_CONNECTORS = {
    "pass": {"default": "job-workflow-pass:v1.0.0",
             "allowed": {"job-workflow-pass:v1.0.0": "workflow-connector-pass"}},
    "scatter": {"default": "job-workflow-scatter:v1.0.0",
                "allowed": {"job-workflow-scatter:v1.0.0": "workflow-connector-scatter"}},
    "gather": {"default": "job-workflow-gather:v1.0.0",
               "allowed": {"job-workflow-gather:v1.0.0": "workflow-connector-gather"}},
    "switch": {"default": "job-workflow-switch:v1.0.0",
               "allowed": {"job-workflow-switch:v1.0.0": "workflow-connector-switch"}},
}
# kinds allowed as the successor, per source kind (switch/gate: every arm target)
_ARM_TARGETS = ("job", "pass", "scatter", "switch", "gate", "halt")
NEXT_KINDS = {
    "job": ("pass", "scatter", "switch", "gate", "halt"),
    "pass": ("job",),
    "scatter": ("gather",),
    "gather": ("pass", "switch", "gate", "halt"),
    "switch": _ARM_TARGETS,
    "gate": _ARM_TARGETS,
}


class WorkflowSpecError(Exception):
    """Raised by :func:`check_spec`; ``.errors`` holds every validation error."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("invalid workflow spec: " + "; ".join(self.errors))


# --------------------------------------------------------------------------- helpers


# contract 12.1/12.2: error classes a failed attempt can carry, and the retry_on patterns over them
RETRY_ON_RE = re.compile(
    r"^(\*|timeout|signal|offline|worker|exit_code:(-?\d+|\*)|pge:([A-Za-z0-9_:.-]{1,64}|\*))$"
)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nodes(spec):
    n = spec.get("nodes") if isinstance(spec, dict) else None
    return {k: v for k, v in n.items() if isinstance(v, dict)} if isinstance(n, dict) else {}


def _next(node):
    n = node.get("next")
    return [t for t in n if isinstance(t, str)] if isinstance(n, list) else []


def _arm_list(v):
    """The single target of a well-formed arm (``[id]``) or None."""
    return v[0] if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str) else None


def switch_arms(node):
    """Ordered ``[(label, target)]`` of a switch/gate node; ``[]`` for other kinds.

    gate: ``("true", on_true)``, ``("false", on_false)``. switch: each case (label = its ``name``,
    default ``case_<i>``) then, for ``on``/``choices``, each choice (label = the key), and finally
    ``("default", default)``. Malformed arms (not a one-id list) are skipped; the validator reports them."""
    if not isinstance(node, dict):
        return []
    kind = node.get("kind")
    arms = []
    if kind == "gate":
        for label, key in (("true", "on_true"), ("false", "on_false")):
            t = _arm_list(node.get(key))
            if t is not None:
                arms.append((label, t))
    elif kind == "switch":
        cases, choices = node.get("cases"), node.get("choices")
        if isinstance(cases, list):
            for i, c in enumerate(cases):
                if not isinstance(c, dict):
                    continue
                name = c.get("name") if isinstance(c.get("name"), str) else "case_%d" % i
                t = _arm_list(c.get("next"))
                if t is not None:
                    arms.append((name, t))
        elif isinstance(choices, dict):
            for label, v in choices.items():
                t = _arm_list(v)
                if isinstance(label, str) and t is not None:
                    arms.append((label, t))
        t = _arm_list(node.get("default"))
        if t is not None:
            arms.append((DEFAULT_LABEL, t))
    return arms


def successors(node):
    """Deduplicated successor ids of a node: ``next`` targets plus every switch/gate arm target.
    (A scatter's child is not a successor here; ``_edges`` adds that edge separately.)"""
    out = []
    if not isinstance(node, dict):
        return out
    for t in _next(node) + [t for _, t in switch_arms(node)]:
        if t not in out:
            out.append(t)
    return out


def _children(spec):
    """{scatter_id: child_id} for well-formed scatter nodes."""
    return {
        nid: n["child"]
        for nid, n in _nodes(spec).items()
        if n.get("kind") == "scatter" and isinstance(n.get("child"), str)
    }


def _edges(spec):
    """[(src, dst)] over existing nodes: ``next`` and arm edges plus scatter -> child."""
    nodes = _nodes(spec)
    edges = [(nid, t) for nid, n in nodes.items() for t in successors(n) if t in nodes]
    edges += [(s, c) for s, c in _children(spec).items() if c in nodes]
    return edges


def predecessors(spec):
    """``{node_id: [predecessor ids]}`` (``next`` and arm edges plus scatter -> child)."""
    preds = {nid: [] for nid in _nodes(spec)}
    for s, d in _edges(spec):
        if s not in preds[d]:
            preds[d].append(s)
    return preds


def ancestors(spec, node_id):
    """Strict ancestors of ``node_id`` (a set). Cycle-safe."""
    preds = predecessors(spec)
    seen, stack = set(), list(preds.get(node_id, []))
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(preds.get(n, []))
    seen.discard(node_id)  # strict: a node on a cycle is not its own ancestor here (the cycle is reported separately)
    return seen


def dominators(spec):
    """``{node_id: set of dominators}`` for every node reachable from ``entry`` (a node dominates
    itself; strict dominators are ``dom[n] - {n}``). Arm edges and scatter -> child edges count.
    Unreachable nodes are absent; ``{}`` when the entry is missing. Cycle-safe (iterative dataflow)."""
    nodes = _nodes(spec)
    entry = spec.get("entry") if isinstance(spec, dict) else None
    if not isinstance(entry, str) or entry not in nodes:
        return {}
    succ = {nid: [] for nid in nodes}
    for s_, d in _edges(spec):
        succ[s_].append(d)
    reach, stack = set(), [entry]
    while stack:
        n = stack.pop()
        if n not in reach:
            reach.add(n)
            stack.extend(succ[n])
    preds = {n: [] for n in reach}
    for s_ in reach:
        for d in succ[s_]:
            preds[d].append(s_)
    order = [n for n in nodes if n in reach]
    dom = {n: set(reach) for n in reach}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for n in order:
            if n == entry:
                continue
            new = set.intersection(*(dom[p] for p in preds[n])) | {n} if preds[n] else {n}
            if new != dom[n]:
                dom[n] = new
                changed = True
    return dom


def _reachable_from(spec, starts):
    succ = {}
    for s_, d in _edges(spec):
        succ.setdefault(s_, []).append(d)
    seen, stack = set(), list(starts)
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(succ.get(n, []))
    return seen


def nodes_to_skip(spec, switch_id, chosen_target):
    """Node ids to mark ``skipped`` when ``switch_id`` routes to ``chosen_target``: everything
    reachable from the untaken arm targets (scatter children included) that is not reachable from
    ``chosen_target`` (so an exclusive rejoin and everything after it is kept). Returned in spec
    declaration order; the switch itself is never included. The caller only flips nodes still
    ``pending``. Raises ``WorkflowRoutingError`` if ``switch_id`` is not a switch/gate or
    ``chosen_target`` is not one of its arm targets."""
    node = _nodes(spec).get(switch_id)
    arms = switch_arms(node)
    if not arms:
        raise WorkflowRoutingError("node '%s' is not a switch or gate" % switch_id)
    targets = [t for _, t in arms]
    if chosen_target not in targets:
        raise WorkflowRoutingError(
            "node '%s': '%s' is not one of its arm targets %s" % (switch_id, chosen_target, sorted(set(targets))))
    untaken = [t for t in targets if t != chosen_target]
    skip = _reachable_from(spec, untaken) - _reachable_from(spec, [chosen_target]) - {switch_id}
    return [n for n in _nodes(spec) if n in skip]


# ------------------------------------------------------------------ switch evaluation


def _is_json_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _json_eq(a, b):
    """JSON equality: like ``==`` but a bool never equals a number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_json_eq(a[k], b[k]) for k in a)
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return False
    return a == b


def _operand(v, context):
    """Resolve a ``left``/``right``: a top-level ref must resolve to a non-null value (D3, else
    ``WorkflowRefError``); anything else is a JSON literal (nested refs inside it are resolved)."""
    if is_ref(v):
        val = resolve(v, context)
        if val is None:
            raise WorkflowRefError("cannot resolve %s: value is null" % v)
        return val
    return resolve(v, context)


def _compare(op, left, right, where):
    if op == "eq":
        return _json_eq(left, right)
    if op == "ne":
        return not _json_eq(left, right)
    if op == "in":
        if not isinstance(right, list):
            raise WorkflowRoutingError("%s: 'in' needs a list on the right, got %s" % (where, type(right).__name__))
        return any(_json_eq(left, x) for x in right)
    if op in ("lt", "le", "gt", "ge"):
        both_num = _is_json_num(left) and _is_json_num(right)
        both_str = isinstance(left, str) and isinstance(right, str)
        if not (both_num or both_str):
            raise WorkflowRoutingError(
                "%s: '%s' needs two numbers or two strings, got %s and %s (a bool is not a number)"
                % (where, op, type(left).__name__, type(right).__name__))
        return {"lt": left < right, "le": left <= right, "gt": left > right, "ge": left >= right}[op]
    raise WorkflowRoutingError("%s: unknown op %r" % (where, op))


def _eval_when(tree, context, where, depth=1):
    if depth > WHEN_MAX_DEPTH:
        raise WorkflowRoutingError("%s: 'when' nesting is deeper than %d" % (where, WHEN_MAX_DEPTH))
    if not isinstance(tree, dict):
        raise WorkflowRoutingError("%s: 'when' must be an object" % where)
    if "all" in tree or "any" in tree:
        key = "all" if "all" in tree else "any"
        items = tree[key]
        if not isinstance(items, list) or not items:
            raise WorkflowRoutingError("%s: '%s' must be a non-empty list" % (where, key))
        for it in items:  # short-circuit: later branches (and their refs) are not touched
            r = _eval_when(it, context, where, depth + 1)
            if key == "all" and not r:
                return False
            if key == "any" and r:
                return True
        return key == "all"
    if "not" in tree:
        return not _eval_when(tree["not"], context, where, depth + 1)
    if not {"left", "op", "right"} <= set(tree):
        raise WorkflowRoutingError("%s: malformed 'when' clause %r" % (where, tree))
    left = _operand(tree["left"], context)
    right = _operand(tree["right"], context)
    return _compare(tree["op"], left, right, where)


def evaluate_switch(node, context, node_id=None):
    """Evaluate a switch/gate ``node`` against ``context`` (``workflow_refs.build_context``) and
    return ``(label, target)``.

    switch/cases: first matching case wins, else ``("default", default_target)``. switch/on: the
    resolved value is mapped with ``str()`` (``True`` -> ``"True"``, ``1.0`` -> ``"1.0"``) and looked
    up in ``choices``; a miss gives ``("default", ...)``. gate: ``("true"|"false", target)``.
    Raises ``WorkflowRefError`` for a missing/null ref, ``WorkflowRoutingError`` for a type/operator
    error. ``node_id`` only qualifies error messages."""
    where = "node '%s'" % node_id if node_id else "switch"
    arms = dict(switch_arms(node))
    kind = (node or {}).get("kind")
    if kind == "gate":
        label = "true" if _eval_when(node.get("when"), context, where) else "false"
        return label, arms[label]
    if kind != "switch":
        raise WorkflowRoutingError("%s: not a switch or gate (kind %r)" % (where, kind))
    if "cases" in node:
        for i, c in enumerate(node["cases"]):
            label = c.get("name") if isinstance(c.get("name"), str) else "case_%d" % i
            if _eval_when(c.get("when"), context, where):
                return label, arms[label]
        return DEFAULT_LABEL, arms[DEFAULT_LABEL]
    val = _operand(node.get("on"), context)
    label = val if isinstance(val, str) else str(val)
    if label != DEFAULT_LABEL and label in (node.get("choices") or {}) and label in arms:
        return label, arms[label]
    return DEFAULT_LABEL, arms[DEFAULT_LABEL]


def topo_order(spec):
    """Node ids in topological order (ties broken by declaration order). Raises on a cycle."""
    nodes = _nodes(spec)
    indeg = {nid: 0 for nid in nodes}
    succ = {nid: [] for nid in nodes}
    for s, d in _edges(spec):
        succ[s].append(d)
        indeg[d] += 1
    ready = [n for n in nodes if indeg[n] == 0]
    order = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for d in succ[n]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    if len(order) != len(nodes):
        raise WorkflowSpecError(["workflow graph has a cycle"])
    return order


def spec_doc_id(spec):
    """The ``workflow_specs`` document id: ``"<id>:<version>"``."""
    return "%s:%s" % (spec["id"], spec["version"])


def _gather_outputs(node):
    """Output names a gather declares (the <output> of each collect ref)."""
    c = node.get("collect")
    items = c if isinstance(c, list) else [c]
    names = []
    for it in items:
        try:
            p = parse_ref(it) if is_ref(it) else None
        except WorkflowRefError:
            p = None
        if p and p[0] == "node":
            names.append(p[2])
    return names


ROUTE_OUTPUTS = ("arm", "target")  # written by the switch connector; referenceable as $.qa_route.arm
GATHER_COUNT_OUTPUTS = ("n_completed", "n_failed")  # written by the gather connector; referenceable as $.G.n_completed


def job_tag(node, node_id):
    """The tag of the job(s) submitted for a node: the job node's ``tag`` if set, else the node
    id (the step name). Connector nodes have no ``tag`` so they always get their node id."""
    return (node or {}).get("tag") or node_id


def resolve_node_queue(spec, node, recommended_queue):
    """Queue for a job node: node.queue, then defaults.queue, then ``recommended_queue(node['run'])`` (or None)."""
    return node.get("queue") or (spec.get("defaults") or {}).get("queue") or recommended_queue(node["run"])


def _declared_outputs(node):
    kind = node.get("kind")
    if kind == "job":
        out = node.get("out")
        return {o for o in out if isinstance(o, str)} if isinstance(out, list) else set()
    if kind == "pass":
        b = node.get("bind")
        return {k.split(".", 1)[1] for k in b if isinstance(k, str) and "." in k} if isinstance(b, dict) else set()
    if kind == "gather":
        return set(_gather_outputs(node)) | set(GATHER_COUNT_OUTPUTS)
    if kind in ARM_KINDS:
        return set(ROUTE_OUTPUTS)
    return set()


def _walk_strings(value, path):
    """Yield (path, str) for every string inside nested dict/list ``value``."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings(v, "%s.%s" % (path, k) if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings(v, "%s[%d]" % (path, i))


# --------------------------------------------------------------------------- validation


def _check_input_value(value, typ):
    """Return None if ``value`` matches input type ``typ`` else a description of the problem."""
    if typ.endswith("[]"):
        if not isinstance(value, list):
            return "expected a list for %s, got %s" % (typ, type(value).__name__)
        for i, v in enumerate(value):
            err = _check_input_value(v, typ[:-2])
            if err:
                return "element %d: %s" % (i, err)
        return None
    ok = {
        "string": isinstance(value, str),
        "int": _is_int(value),
        "float": _is_num(value),  # bool excluded; ints are acceptable floats
        "bool": isinstance(value, bool),
    }[typ]
    return None if ok else "expected %s, got %s (%r)" % (typ, type(value).__name__, value)


def _validate_optional_job_fields(obj, where, errors):
    if "queue" in obj and not (isinstance(obj["queue"], str) and obj["queue"]):
        errors.append("%s: 'queue' must be a non-empty string" % where)
    if "max_retries" in obj and not (_is_int(obj["max_retries"]) and obj["max_retries"] >= 0):
        errors.append("%s: 'max_retries' must be an integer >= 0" % where)
    if "retry_on" in obj and not (
        isinstance(obj["retry_on"], list) and all(isinstance(x, str) for x in obj["retry_on"])
    ):
        errors.append("%s: 'retry_on' must be a list of strings" % where)
    elif "retry_on" in obj:
        for x in obj["retry_on"]:
            if not RETRY_ON_RE.match(x):
                errors.append(
                    "%s: 'retry_on' entry %r is not a valid error class pattern "
                    "(exit_code:<n>|exit_code:*|timeout|signal|offline|worker|pge:<token>|*)" % (where, x))
    if "timeout_hours" in obj and not (_is_num(obj["timeout_hours"]) and obj["timeout_hours"] > 0):
        errors.append("%s: 'timeout_hours' must be a number > 0" % where)


def _validate_schema(spec, errors):
    """Rule 1 (+ per-kind required fields)."""
    for f in ("id", "version"):
        if not (isinstance(spec.get(f), str) and spec[f].strip()):
            errors.append("spec: '%s' is required and must be a non-empty string" % f)
    if spec.get("schema_version") != SCHEMA_VERSION:
        errors.append("spec: 'schema_version' must be %r, got %r" % (SCHEMA_VERSION, spec.get("schema_version")))
    for f in sorted(set(spec) - TOP_FIELDS):
        errors.append("spec: unknown field '%s'" % f)
    if "label" in spec and not isinstance(spec["label"], str):
        errors.append("spec: 'label' must be a string")

    if "defaults" in spec:
        d = spec["defaults"]
        if not isinstance(d, dict):
            errors.append("spec: 'defaults' must be an object")
        else:
            for f in sorted(set(d) - DEFAULT_FIELDS):
                errors.append("spec defaults: unknown field '%s'" % f)
            _validate_optional_job_fields(d, "spec defaults", errors)

    inputs = spec.get("inputs", {})
    if not isinstance(inputs, dict):
        errors.append("spec: 'inputs' must be an object")
    else:
        for name, decl in inputs.items():
            where = "input '%s'" % name
            if not (isinstance(name, str) and NAME_RE.match(name)):
                errors.append("%s: name must match %s" % (where, NAME_RE.pattern))
            if not isinstance(decl, dict):
                errors.append("%s: declaration must be an object" % where)
                continue
            for f in sorted(set(decl) - {"type", "default"}):
                errors.append("%s: unknown field '%s'" % (where, f))
            typ = decl.get("type")
            if typ not in INPUT_TYPES:
                errors.append("%s: 'type' must be one of %s, got %r" % (where, list(INPUT_TYPES), typ))
            elif "default" in decl:
                err = _check_input_value(decl["default"], typ)
                if err:
                    errors.append("%s: default does not match type %s: %s" % (where, typ, err))

    nodes = spec.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        errors.append("spec: 'nodes' is required and must be a non-empty object")
        return
    for nid, node in nodes.items():
        where = "node '%s'" % nid
        if not (isinstance(nid, str) and NODE_ID_RE.match(nid)):
            errors.append("%s: node id must match %s" % (where, NODE_ID_RE.pattern))
        elif nid in RESERVED_NODE_IDS:
            errors.append("%s: node id is reserved (%s)" % (where, ", ".join(RESERVED_NODE_IDS)))
        if not isinstance(node, dict):
            errors.append("%s: must be an object" % where)
            continue
        kind = node.get("kind")
        if kind in UNSUPPORTED_KINDS:
            errors.append("%s: kind '%s' is not supported" % (where, kind))
            continue
        if kind not in SUPPORTED_KINDS:
            errors.append("%s: unknown or missing 'kind' %r (expected one of %s)" % (where, kind, list(SUPPORTED_KINDS)))
            continue
        if "timeout_hours" in node:
            errors.append(
                "node %s: timeout_hours is not supported per node in v1; use defaults.timeout_hours (run timeout) "
                "or the job-spec time_limit" % nid)
        for f in sorted(set(node) - NODE_FIELDS[kind] - {"timeout_hours"}):
            errors.append("%s: unknown field '%s' for kind '%s'" % (where, f, kind))
        if "next" in node and not (isinstance(node["next"], list) and all(isinstance(t, str) for t in node["next"])):
            errors.append("%s: 'next' must be a list of node ids" % where)
        _validate_kind_fields(nid, node, kind, where, errors)

    outs = spec.get("outputs", {})
    if not isinstance(outs, dict):
        errors.append("spec: 'outputs' must be an object")
    else:
        for name, ref in outs.items():
            if not (isinstance(name, str) and NAME_RE.match(name)):
                errors.append("spec outputs: name %r must match %s" % (name, NAME_RE.pattern))
            if not is_ref(ref):
                errors.append("spec outputs '%s': value must be a '$.' ref, got %r" % (name, ref))


def _validate_arm(value, what, where, errors):
    if _arm_list(value) is None:
        errors.append("%s: %s must be a list with exactly one node id, got %r" % (where, what, value))


def _validate_when(tree, path, where, errors, depth=1):
    """A ``when`` tree: leaf {left, op, right} | {all: [..]} | {any: [..]} | {not: {..}}; depth <= 8."""
    if depth > WHEN_MAX_DEPTH:
        errors.append("%s: %s nests deeper than %d levels" % (where, path, WHEN_MAX_DEPTH))
        return
    if not isinstance(tree, dict):
        errors.append("%s: %s must be an object (a comparison, or all/any/not)" % (where, path))
        return
    combos = [k for k in ("all", "any", "not") if k in tree]
    if combos:
        if len(tree) != 1:
            errors.append("%s: %s must have exactly one of all/any/not and nothing else, has %s" % (where, path, sorted(tree)))
            return
        k = combos[0]
        if k == "not":
            _validate_when(tree["not"], path + ".not", where, errors, depth + 1)
        elif not isinstance(tree[k], list) or not tree[k]:
            errors.append("%s: %s.%s must be a non-empty list" % (where, path, k))
        else:
            for i, t in enumerate(tree[k]):
                _validate_when(t, "%s.%s[%d]" % (path, k, i), where, errors, depth + 1)
        return
    for f in sorted(set(tree) - {"left", "op", "right"}):
        errors.append("%s: %s has unknown field '%s'" % (where, path, f))
    for f in ("left", "op", "right"):
        if f not in tree:
            errors.append("%s: %s is missing '%s'" % (where, path, f))
    if "op" in tree and tree["op"] not in WHEN_OPS:
        errors.append("%s: %s.op must be one of %s, got %r" % (where, path, list(WHEN_OPS), tree["op"]))
    elif tree.get("op") == "in" and "right" in tree and not (is_ref(tree["right"]) or isinstance(tree["right"], list)):
        errors.append("%s: %s: 'in' needs a list literal or a $-ref on the right, got %r" % (where, path, tree["right"]))


def _validate_route_fields(nid, node, kind, where, errors):
    if kind == "gate":
        _validate_when(node.get("when"), "when", where, errors) if "when" in node else errors.append(
            "%s: 'when' is required" % where)
        for k in ("on_true", "on_false"):
            if k not in node:
                errors.append("%s: '%s' is required" % (where, k))
            else:
                _validate_arm(node[k], "'%s'" % k, where, errors)
        return
    if kind == "halt":
        if node.get("status") not in HALT_STATUSES:
            errors.append("%s: 'status' must be one of %s, got %r" % (where, list(HALT_STATUSES), node.get("status")))
        r = node.get("reason")
        if not (isinstance(r, str) and 1 <= len(r) <= HALT_REASON_MAX):
            errors.append("%s: 'reason' is required: a string of 1..%d characters" % (where, HALT_REASON_MAX))
        return
    # switch
    has_cases, has_on, has_choices = "cases" in node, "on" in node, "choices" in node
    if has_cases == (has_on or has_choices) or (has_on != has_choices):
        errors.append("%s: a switch needs exactly one of 'cases' or 'on' + 'choices'" % where)
    labels = []
    if has_cases:
        cases = node["cases"]
        if not isinstance(cases, list) or not cases:
            errors.append("%s: 'cases' must be a non-empty list" % where)
        else:
            for i, c in enumerate(cases):
                cw = "cases[%d]" % i
                if not isinstance(c, dict):
                    errors.append("%s: %s must be an object" % (where, cw))
                    continue
                for f in sorted(set(c) - {"name", "when", "next"}):
                    errors.append("%s: %s has unknown field '%s'" % (where, cw, f))
                if "name" in c and not (isinstance(c["name"], str) and TAG_RE.match(c["name"])):
                    errors.append("%s: %s.name must be a string matching %s" % (where, cw, TAG_RE.pattern))
                labels.append(c["name"] if isinstance(c.get("name"), str) else "case_%d" % i)
                if "when" not in c:
                    errors.append("%s: %s.when is required" % (where, cw))
                else:
                    _validate_when(c["when"], cw + ".when", where, errors)
                if "next" not in c:
                    errors.append("%s: %s.next is required" % (where, cw))
                else:
                    _validate_arm(c["next"], cw + ".next", where, errors)
    if has_on and not is_ref(node["on"]):
        errors.append("%s: 'on' must be a '$.' ref, got %r" % (where, node["on"]))
    if has_choices:
        ch = node["choices"]
        if not isinstance(ch, dict) or not ch:
            errors.append("%s: 'choices' must be a non-empty object" % where)
        else:
            for k, v in ch.items():
                if not (isinstance(k, str) and TAG_RE.match(k)):
                    errors.append("%s: choices key %r must match %s" % (where, k, TAG_RE.pattern))
                labels.append(k)
                _validate_arm(v, "choices['%s']" % k, where, errors)
    if "default" not in node:
        errors.append("%s: 'default' is required" % where)
    else:
        _validate_arm(node["default"], "'default'", where, errors)
    if DEFAULT_LABEL in labels:
        errors.append("%s: the arm label '%s' is reserved" % (where, DEFAULT_LABEL))
    dups = sorted({l for l in labels if labels.count(l) > 1})
    if dups:
        errors.append("%s: duplicate arm names %s" % (where, dups))


def _validate_kind_fields(nid, node, kind, where, errors):
    if kind in ARM_KINDS or kind == "halt":
        _validate_route_fields(nid, node, kind, where, errors)
    if kind == "job":
        if not (isinstance(node.get("run"), str) and node["run"]):
            errors.append("%s: 'run' is required and must be a non-empty string" % where)
        if not isinstance(node.get("in"), dict):
            errors.append("%s: 'in' is required and must be an object" % where)
        out = node.get("out")
        if not (isinstance(out, list) and all(isinstance(o, str) and NAME_RE.match(o) for o in out)):
            errors.append("%s: 'out' is required and must be a list of names matching %s" % (where, NAME_RE.pattern))
        elif len(set(out)) != len(out):
            errors.append("%s: 'out' has duplicate names" % where)
        if "tag" in node and not (isinstance(node["tag"], str) and TAG_RE.match(node["tag"])):
            errors.append("%s: 'tag' must be a string matching %s" % (where, TAG_RE.pattern))
        _validate_optional_job_fields({k: v for k, v in node.items() if k != "timeout_hours"}, where, errors)
    if kind == "pass":
        b = node.get("bind")
        if b is None:  # omitted == {}: a pure sequencing pass (declares no outputs)
            b = {}
        if not isinstance(b, dict):
            errors.append("%s: 'bind' must be an object (or omitted/{} for a pure sequencing pass)" % where)
            return
        for key, ref in b.items():
            parts = key.split(".", 1) if isinstance(key, str) else []
            if len(parts) != 2 or not parts[0] or not NAME_RE.match(parts[1]):
                errors.append("%s: bind key %r must be '<next_node>.<param>'" % (where, key))
            if not is_ref(ref):
                errors.append("%s: bind '%s' value must be a '$.' ref, got %r" % (where, key, ref))
    elif kind == "scatter":
        if not (isinstance(node.get("child"), str) and node["child"]):
            errors.append("%s: 'child' is required and must be a node id" % where)
        if "over" not in node:  # any $-ref or literal (§11.5); its meaning belongs to the connector
            errors.append("%s: 'over' is required" % where)
        if "completion" in node:
            c = node["completion"]
            if not isinstance(c, dict):
                errors.append("%s: 'completion' must be an object" % where)
            else:
                for f in sorted(set(c) - {"threshold"}):
                    errors.append("%s: completion has unknown field '%s'" % (where, f))
                if "threshold" in c and not (_is_num(c["threshold"]) and 0 < c["threshold"] <= 1):
                    errors.append("%s: completion.threshold must be a number in (0, 1], got %r" % (where, c["threshold"]))
    if kind in PLUGGABLE_KINDS:
        if "connector" in node and not (isinstance(node["connector"], str) and node["connector"]):
            errors.append("%s: 'connector' must be a non-empty string" % where)
        if "connector_queue" in node and not isinstance(node["connector_queue"], str):
            errors.append("%s: 'connector_queue' must be a string" % where)
    if kind == "gather":
        c = node.get("collect")
        items = c if isinstance(c, list) else [c]
        if c is None or not items or not all(is_ref(i) for i in items):
            errors.append("%s: 'collect' is required: a '$.<child>.<output>' ref or a non-empty list of them" % where)
            return
        for i in items:
            try:
                if parse_ref(i)[0] != "node":
                    errors.append("%s: collect %r must be '$.<child>.<output>'" % (where, i))
            except WorkflowRefError as e:
                errors.append("%s: collect: %s" % (where, e))
        names = _gather_outputs(node)
        if len(set(names)) != len(names):
            errors.append("%s: collect declares duplicate output names" % where)


def _exclusive_rejoin(spec, t, preds, doms):
    """Rule D1: ``t`` (with >1 incoming edges from ``preds``) is a valid rejoin iff some switch/gate S
    strictly dominates it, every incoming edge maps to an arm target of S (``t`` itself for P == S, else
    the arm target dominating P) and those arm targets are pairwise distinct."""
    nodes = _nodes(spec)
    for s_ in sorted(doms.get(t, set()) - {t}):
        if nodes[s_].get("kind") not in ARM_KINDS:
            continue
        arm_targets = {tg for _, tg in switch_arms(nodes[s_])}
        mapped = []
        for p in preds:
            if p == s_:
                mapped.append(t)
                continue
            cands = (doms.get(p, set()) & arm_targets) - {s_}
            if len(cands) != 1:
                break
            mapped.append(next(iter(cands)))
        else:
            if len(set(mapped)) == len(mapped):
                return True
    return False


def _validate_graph(spec, errors):
    """Rules 2-6."""
    nodes = _nodes(spec)
    entry = spec.get("entry")
    if not isinstance(entry, str) or entry not in nodes:
        errors.append("spec: 'entry' %r is not a node" % (entry,))
    elif nodes[entry].get("kind") != "job":
        errors.append("spec: entry node '%s' must be a job, is %r" % (entry, nodes[entry].get("kind")))

    # rule 3 + 4: next targets
    for nid, node in nodes.items():
        kind = node.get("kind")
        if kind not in NEXT_KINDS:
            continue
        nxt = _next(node)
        where = "node '%s'" % nid
        if len(nxt) > 1:
            errors.append("%s: 'next' has %d targets; v1 allows at most 1" % (where, len(nxt)))
        for t in nxt:
            if t not in nodes:
                errors.append("%s: next target '%s' does not exist" % (where, t))
            elif nodes[t].get("kind") not in NEXT_KINDS[kind]:
                errors.append(
                    "%s: a %s node's next must be a %s node, but '%s' is %r"
                    % (where, kind, " or ".join(NEXT_KINDS[kind]), t, nodes[t].get("kind"))
                )
        if kind in ("pass", "scatter") and len(nxt) != 1:
            errors.append("%s: a %s node needs exactly one next target, has %d" % (where, kind, len(nxt)))
        if kind == "pass" and len(nxt) == 1 and isinstance(node.get("bind"), dict):
            for key in node["bind"]:
                if isinstance(key, str) and "." in key and key.split(".", 1)[0] != nxt[0]:
                    errors.append(
                        "%s: bind key %r targets '%s' but next is '%s'" % (where, key, key.split(".", 1)[0], nxt[0])
                    )

    # arm targets (switch/gate)
    for nid, node in nodes.items():
        if node.get("kind") not in ARM_KINDS:
            continue
        for label, t in switch_arms(node):
            where = "node '%s'" % nid
            if t not in nodes:
                errors.append("%s: arm '%s' target '%s' does not exist" % (where, label, t))
            elif nodes[t].get("kind") not in NEXT_KINDS[node["kind"]]:
                errors.append("%s: arm '%s' target must be a %s node, but '%s' is %r"
                              % (where, label, " or ".join(NEXT_KINDS[node["kind"]]), t, nodes[t].get("kind")))

    # rule 5: scatter children
    owners = {}
    for sid, child in _children(spec).items():
        owners.setdefault(child, []).append(sid)
        where = "node '%s'" % sid
        if child not in nodes:
            errors.append("%s: child '%s' does not exist" % (where, child))
        elif nodes[child].get("kind") != "job":
            errors.append("%s: child '%s' must be a job, is %r" % (where, child, nodes[child].get("kind")))
        elif _next(nodes[child]):
            errors.append("node '%s': scatter child must have next: [], has %s" % (child, _next(nodes[child])))
        if child == entry:
            errors.append("%s: child '%s' cannot be the entry node" % (where, child))
    for child, sids in owners.items():
        if len(sids) > 1:
            errors.append("node '%s': scatter child referenced by multiple scatters %s" % (child, sorted(sids)))
        for nid, node in nodes.items():
            if child in successors(node):
                errors.append("node '%s': scatter child '%s' must not appear in any next (found in '%s')" % (child, child, nid))

    # fan-in over `next` and arm edges (scatter -> child edges are covered above)
    in_preds = {}
    for nid, node in nodes.items():
        for t in successors(node):
            if t in nodes:
                in_preds.setdefault(t, []).append(nid)
    doms = dominators(spec)
    for t, ps in in_preds.items():
        if len(ps) > 1 and not _exclusive_rejoin(spec, t, ps, doms):
            errors.append(
                "node '%s': fan-in is only allowed where the incoming edges come from different arms of one switch; "
                "has predecessors %s" % (t, sorted(ps)))
    for nid in nodes:
        if nid == entry and nid in in_preds:
            errors.append("node '%s': entry must have no predecessors (found %s)" % (nid, sorted(in_preds[nid])))

    # rule 6: cycles + reachability
    edges = _edges(spec)
    succ = {nid: [] for nid in nodes}
    for s, d in edges:
        succ[s].append(d)
    state, cyc_nodes = {}, set()

    def dfs(n, stack):
        state[n] = 1
        stack.append(n)
        for d in succ[n]:
            if state.get(d) == 1:
                cyc_nodes.update(stack[stack.index(d):])
            elif d not in state:
                dfs(d, stack)
        stack.pop()
        state[n] = 2

    import sys

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, len(nodes) * 2 + 100))
    try:
        for n in nodes:
            if n not in state:
                dfs(n, [])
    finally:
        sys.setrecursionlimit(old)
    if cyc_nodes:
        errors.append("workflow graph has a cycle through %s" % sorted(cyc_nodes))
    if isinstance(entry, str) and entry in nodes:
        seen, stack = set(), [entry]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(succ[n])
        for nid in nodes:
            if nid not in seen:
                errors.append("node '%s': not reachable from entry '%s'" % (nid, entry))


def _check_ref(spec, ref, where, owner, errors, dominated_by=None, child_ids=frozenset()):
    """Rule 7/8 for one ref. ``owner`` is the referencing node id, or None for spec.outputs.
    ``dominated_by`` is the set of strict dominators of ``owner`` (the only nodes it may reference)."""
    nodes = _nodes(spec)
    try:
        p = parse_ref(ref)
    except WorkflowRefError as e:
        errors.append("%s: %s" % (where, e))
        return
    if p[0] == "inputs":
        inputs = spec.get("inputs") if isinstance(spec.get("inputs"), dict) else {}
        if p[1] not in inputs:
            errors.append("%s: %s refers to undeclared input '%s'" % (where, ref, p[1]))
    elif p[0] == "scatter_item":
        if owner is None or owner not in child_ids or "field 'in" not in where:
            errors.append("%s: '$.scatter.item' is only allowed in a scatter child's 'in'" % where)
    else:
        _, node, output = p
        if node not in nodes:
            errors.append("%s: %s refers to unknown node '%s'" % (where, ref, node))
            return
        if owner is None:
            if node in child_ids:
                errors.append("%s: %s refers to scatter child '%s', whose outputs are not stored" % (where, ref, node))
                return
        elif node == owner or node not in dominated_by:
            errors.append("%s: %s refers to '%s', which is not on every path to '%s' (it may be skipped)"
                          % (where, ref, node, owner))
            return
        if output not in _declared_outputs(nodes[node]):
            errors.append("%s: %s refers to output '%s' not declared by node '%s'" % (where, ref, output, node))


def _validate_refs(spec, errors):
    """Rules 7-8."""
    nodes = _nodes(spec)
    child_ids = frozenset(_children(spec).values())
    doms = dominators(spec)
    for nid, node in nodes.items():
        kind = node.get("kind")
        anc = (doms[nid] - {nid}) if nid in doms else ancestors(spec, nid)  # unreachable: report only the unreachability
        base = "node '%s'" % nid

        def chk(ref, field):
            _check_ref(spec, ref, "%s field '%s'" % (base, field), nid, errors, anc, child_ids)

        if kind == "job" and isinstance(node.get("in"), dict):
            for path, s in _walk_strings(node["in"], "in"):
                if is_ref(s):
                    chk(s, path)
        elif kind == "pass" and isinstance(node.get("bind"), dict):
            for key, ref in node["bind"].items():
                if is_ref(ref):
                    chk(ref, "bind.%s" % key)
        elif kind == "switch":
            for path, s_ in _walk_strings(node.get("cases"), "cases"):
                if is_ref(s_):
                    chk(s_, path)
            if is_ref(node.get("on")):
                chk(node["on"], "on")
        elif kind == "gate":
            for path, s_ in _walk_strings(node.get("when"), "when"):
                if is_ref(s_):
                    chk(s_, path)
        elif kind == "scatter" and is_ref(node.get("over")):
            chk(node["over"], "over")
        elif kind == "gather":
            c = node.get("collect")
            owner_scatters = [s for s, d in _children(spec).items() if nodes.get(s, {}).get("next") == [nid]]
            if not owner_scatters:
                errors.append("%s: a gather must directly follow a scatter" % base)
                continue
            child = _children(spec)[owner_scatters[0]]
            for ref in c if isinstance(c, list) else [c]:
                try:
                    p = parse_ref(ref) if is_ref(ref) else None
                except WorkflowRefError:
                    p = None
                if not p or p[0] != "node":
                    continue
                if p[1] != child:
                    errors.append(
                        "%s field 'collect': %s must refer to the scatter child '%s'" % (base, ref, child)
                    )
                elif child in nodes and p[2] not in _declared_outputs(nodes[child]):
                    errors.append("%s field 'collect': %s refers to output '%s' not declared by node '%s'" % (base, ref, p[2], child))

    outs = spec.get("outputs")
    if isinstance(outs, dict):
        for name, ref in outs.items():
            if is_ref(ref):
                _check_ref(spec, ref, "spec outputs field '%s'" % name, None, errors, None, child_ids)


def resolve_connectors(spec, config):
    """Contract §11.3 rules 1 and 3. Returns ``(resolved_spec, errors)``; ``spec`` is not mutated.

    For each pass/scatter/gather/switch/gate node (a gate uses the ``switch`` entry): an absent ``connector`` becomes the kind's ``default``;
    a ``connector`` outside the kind's ``allowed`` is an error; ``connector_queue`` is always
    (over)written from ``allowed``. ``config`` is the ``WORKFLOW_CONNECTORS`` shape (§11.2).
    """
    resolved = copy.deepcopy(spec)
    errors = []
    nodes = resolved.get("nodes") if isinstance(resolved, dict) else None
    if not isinstance(nodes, dict):
        return resolved, errors
    config = config or {}
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") not in PLUGGABLE_KINDS:
            continue
        kind = node["kind"]
        ckind = CONNECTOR_CONFIG_KIND.get(kind, kind)  # a gate uses the switch entry
        entry = config.get(ckind)
        allowed = entry.get("allowed") if isinstance(entry, dict) else None
        if not isinstance(allowed, dict) or not allowed:
            errors.append("node '%s' (%s): no connectors configured for kind '%s'" % (nid, kind, ckind))
            continue
        conn = node.get("connector")
        if conn is None:
            conn = entry.get("default")
        if conn not in allowed:
            errors.append("node '%s' (%s): connector %r is not allowed for kind '%s'; allowed: %s"
                          % (nid, kind, conn, ckind, sorted(allowed)))
            continue
        node["connector"] = conn
        node["connector_queue"] = allowed[conn]
    return resolved, errors


def retry_policy(spec, node_id):
    """Effective ``{"max_retries": int, "retry_on": [str]}`` for a job node (contract 12.2):
    node value, else ``spec.defaults``, else ``0`` / ``[]``. Pure."""
    node = ((spec or {}).get("nodes") or {}).get(node_id) or {}
    defaults = (spec or {}).get("defaults") or {}
    max_retries = node["max_retries"] if "max_retries" in node else defaults.get("max_retries", 0)
    retry_on = node["retry_on"] if "retry_on" in node else defaults.get("retry_on", [])
    return {"max_retries": max_retries, "retry_on": list(retry_on or [])}


def error_class_matches(retry_on, error_class):
    """True if ``error_class`` matches any ``retry_on`` pattern (``*``, ``exit_code:*``, ``pge:*``
    are wildcards; everything else is an exact match). A missing class matches only ``*``."""
    if not isinstance(error_class, str) or not error_class:
        return "*" in (retry_on or [])  # an event with no class (old producer) only matches "*"
    for pat in retry_on or []:
        if pat == "*" or pat == error_class:
            return True
        if pat.endswith(":*") and error_class.startswith(pat[:-1]):
            return True
    return False


def is_retryable(policy, error_class, retry_count):
    """Contract 12.2: retryable iff ``retry_count < max_retries`` and the class matches ``retry_on``."""
    count = retry_count if _is_int(retry_count) else 0
    return count < policy.get("max_retries", 0) and error_class_matches(policy.get("retry_on"), error_class)


def validate_spec(spec):
    """Return the list of ALL validation errors for ``spec`` (empty list == valid)."""
    if not isinstance(spec, dict):
        return ["spec: must be a JSON object"]
    errors = []
    _validate_schema(spec, errors)
    if isinstance(spec.get("nodes"), dict) and spec["nodes"]:
        _validate_graph(spec, errors)
        _validate_refs(spec, errors)
    return errors


def check_spec(spec):
    """Raise :class:`WorkflowSpecError` if ``spec`` is invalid."""
    errors = validate_spec(spec)
    if errors:
        raise WorkflowSpecError(errors)


def validate_run_inputs(spec, inputs):
    """Apply defaults and type-check run inputs. Returns ``(resolved_inputs, errors)``."""
    declared = spec.get("inputs") or {}
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        return {}, ["inputs: must be an object"]
    errors, resolved = [], {}
    for name in sorted(set(inputs) - set(declared)):
        errors.append("input '%s': unknown input (declared: %s)" % (name, sorted(declared)))
    for name, decl in declared.items():
        if name in inputs:
            value = inputs[name]
        elif "default" in decl:
            value = copy.deepcopy(decl["default"])
        else:
            errors.append("input '%s': required but not provided" % name)
            continue
        err = _check_input_value(value, decl["type"])
        if err:
            errors.append("input '%s': %s" % (name, err))
        else:
            resolved[name] = value
    return resolved, errors

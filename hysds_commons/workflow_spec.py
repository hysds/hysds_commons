"""Validator and graph helpers for ``workflow_spec`` documents (contract §1).

``validate_spec`` enforces validation rules 1-9 and returns *all* errors. Specs are plain
dicts; the "schema" is the Python code below (no JSON-schema file, no extra dependency).

Pure module: no OpenSearch, no celery, no I/O.
"""

import copy
import re

from hysds_commons.workflow_refs import WorkflowRefError, is_ref, parse_ref

SCHEMA_VERSION = "1.0"
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_RE = NODE_ID_RE  # input / output / param names

SUPPORTED_KINDS = ("job", "pass", "scatter", "gather")
UNSUPPORTED_KINDS = ("gate", "switch", "halt")
# node ids that may not be authored: ref namespaces and internal connectors
RESERVED_NODE_IDS = ("inputs", "scatter", "finalize", "halt", "_finalize", "_halt")

INPUT_TYPES = ("string", "int", "float", "bool", "string[]", "int[]", "float[]")

TOP_FIELDS = {"id", "version", "schema_version", "label", "description", "defaults", "inputs", "entry", "nodes", "outputs"}
DEFAULT_FIELDS = {"queue", "timeout_hours", "max_retries", "retry_on"}
JOB_OPTIONAL = {"queue", "max_retries", "retry_on", "timeout_hours"}
# free-text documentation fields allowed on every node kind
NODE_DOC_FIELDS = {"label", "description"}
# pluggable connector fields (contract §11); not allowed on job nodes
CONNECTOR_FIELDS = {"connector", "connector_queue"}
PLUGGABLE_KINDS = ("pass", "scatter", "gather")
NODE_FIELDS = {
    "job": {"kind", "run", "in", "out", "next"} | JOB_OPTIONAL | NODE_DOC_FIELDS,
    "pass": {"kind", "bind", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "scatter": {"kind", "child", "over", "completion", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
    "gather": {"kind", "collect", "next"} | NODE_DOC_FIELDS | CONNECTOR_FIELDS,
}

# Deployment allowlist default (contract §11.2); mozart's WORKFLOW_CONNECTORS setting mirrors it.
DEFAULT_WORKFLOW_CONNECTORS = {
    "pass": {"default": "job-workflow-pass:v1.0.0",
             "allowed": {"job-workflow-pass:v1.0.0": "workflow-connector-pass"}},
    "scatter": {"default": "job-workflow-scatter:v1.0.0",
                "allowed": {"job-workflow-scatter:v1.0.0": "workflow-connector-scatter"}},
    "gather": {"default": "job-workflow-gather:v1.0.0",
               "allowed": {"job-workflow-gather:v1.0.0": "workflow-connector-gather"}},
}
# kinds allowed as the single `next` target, per source kind
NEXT_KINDS = {"job": ("pass", "scatter"), "pass": ("job",), "scatter": ("gather",), "gather": ("pass",)}


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


def _children(spec):
    """{scatter_id: child_id} for well-formed scatter nodes."""
    return {
        nid: n["child"]
        for nid, n in _nodes(spec).items()
        if n.get("kind") == "scatter" and isinstance(n.get("child"), str)
    }


def _edges(spec):
    """[(src, dst)] over existing nodes: ``next`` edges plus scatter -> child."""
    nodes = _nodes(spec)
    edges = [(nid, t) for nid, n in nodes.items() for t in _next(n) if t in nodes]
    edges += [(s, c) for s, c in _children(spec).items() if c in nodes]
    return edges


def predecessors(spec):
    """``{node_id: [predecessor ids]}`` (``next`` edges plus scatter -> child)."""
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


def _declared_outputs(node):
    kind = node.get("kind")
    if kind == "job":
        out = node.get("out")
        return {o for o in out if isinstance(o, str)} if isinstance(out, list) else set()
    if kind == "pass":
        b = node.get("bind")
        return {k.split(".", 1)[1] for k in b if isinstance(k, str) and "." in k} if isinstance(b, dict) else set()
    if kind == "gather":
        return set(_gather_outputs(node))
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
            errors.append("%s: kind '%s' is not supported in v1" % (where, kind))
            continue
        if kind not in SUPPORTED_KINDS:
            errors.append("%s: unknown or missing 'kind' %r (expected one of %s)" % (where, kind, list(SUPPORTED_KINDS)))
            continue
        for f in sorted(set(node) - NODE_FIELDS[kind]):
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


def _validate_kind_fields(nid, node, kind, where, errors):
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
        _validate_optional_job_fields(node, where, errors)
    elif kind == "pass":
        b = node.get("bind")
        if not (isinstance(b, dict) and b):
            errors.append("%s: 'bind' is required and must be a non-empty object" % where)
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
            if child in _next(node):
                errors.append("node '%s': scatter child '%s' must not appear in any next (found in '%s')" % (child, child, nid))

    # fan-in over `next` edges (child edges are covered above)
    next_preds = {}
    for nid, node in nodes.items():
        for t in _next(node):
            if t in nodes:
                next_preds.setdefault(t, []).append(nid)
    for t, ps in next_preds.items():
        if len(ps) > 1:
            errors.append("node '%s': fan-in is not allowed; has predecessors %s" % (t, sorted(ps)))
    for nid in nodes:
        if nid == entry and nid in next_preds:
            errors.append("node '%s': entry must have no predecessors (found %s)" % (nid, sorted(next_preds[nid])))

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


def _check_ref(spec, ref, where, owner, errors, ancestors_of=None, child_ids=frozenset()):
    """Rule 7/8 for one ref. ``owner`` is the referencing node id, or None for spec.outputs."""
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
        elif node == owner or node not in ancestors_of:
            errors.append("%s: %s refers to '%s', which is not a strict ancestor of '%s'" % (where, ref, node, owner))
            return
        if output not in _declared_outputs(nodes[node]):
            errors.append("%s: %s refers to output '%s' not declared by node '%s'" % (where, ref, output, node))


def _validate_refs(spec, errors):
    """Rules 7-8."""
    nodes = _nodes(spec)
    child_ids = frozenset(_children(spec).values())
    for nid, node in nodes.items():
        kind = node.get("kind")
        anc = ancestors(spec, nid)
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

    For each pass/scatter/gather node: an absent ``connector`` becomes the kind's ``default``;
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
        entry = config.get(kind)
        allowed = entry.get("allowed") if isinstance(entry, dict) else None
        if not isinstance(allowed, dict) or not allowed:
            errors.append("node '%s' (%s): no connectors configured for kind '%s'" % (nid, kind, kind))
            continue
        conn = node.get("connector")
        if conn is None:
            conn = entry.get("default")
        if conn not in allowed:
            errors.append("node '%s' (%s): connector %r is not allowed for kind '%s'; allowed: %s"
                          % (nid, kind, conn, kind, sorted(allowed)))
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

"""The HySDS CWL profile (PLAN sections 4, 5, 8, 9): accept a documented subset, reject the rest.

``analyze(doc, run_resolver)`` walks the Workflow dict (see ``loader.load_document``) and returns
``(model, issues)``. ``issues`` is a list of ``{path, code, message, severity}`` where severity is
``"error"`` or ``"warning"``. ``model`` is a plain dict consumed by ``compile.py``; it is ``None`` only
when the document is too malformed to read. Hints are matched by their *expanded* URI, never by the
prefix text (B15). Paths are CWL paths such as ``steps.deliver.in.product.source``.
"""

import re

from hysds_commons.cwl.loader import HYSDS_NS, SCHEMA_NS

DECIDE_JOB = "job-workflow-decide"
DECIDE_OTHERWISE_DEFAULT = "otherwise"
CASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
CASE_RESERVED = ("value", "cases")
WHEN_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in")
WHEN_MAX_DEPTH = 8
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RESERVED_STEP_IDS = ("inputs", "scatter", "finalize")
RETRY_ON_RE = re.compile(r"^(\*|timeout|signal|offline|worker|exit_code:(-?\d+|\*)|pge:([A-Za-z0-9_:.-]{1,64}|\*))$")
WHEN_RE = re.compile(r"^\$\(\s*inputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\)$")
PICKS = ("first_non_null", "the_only_non_null", "all_non_null")
JS_MESSAGE = "JS expressions are not supported; use when: $(inputs.flag)"

SCALARS = ("string", "int", "long", "float", "double", "boolean", "File", "Directory", "Any")
REJECTED_REQUIREMENTS = {
    "SubworkflowFeatureRequirement": ("subworkflow_unsupported", "subworkflows are not supported"),
    "StepInputExpressionRequirement": (
        "value_from_unsupported", "StepInputExpressionRequirement is not supported (no valueFrom)"),
    "InlineJavascriptRequirement": ("js_unsupported", JS_MESSAGE),
}
ACCEPTED_REQUIREMENTS = ("ScatterFeatureRequirement", "MultipleInputFeatureRequirement", "SchemaDefRequirement",
                         "ToolTimeLimit")
IGNORED_REQUIREMENTS = ("WorkReuse", "NetworkAccess", "InplaceUpdateRequirement", "LoadListingRequirement",
                        "EnvVarRequirement", "SoftwareRequirement", "InitialWorkDirRequirement", "DockerRequirement")
FILE_FIELDS = ("secondaryFiles", "checksum", "format", "contents")
PARAM_REJECTED = ("secondaryFiles", "format", "loadContents", "loadListing")

HYSDS_HINTS = {
    # name: levels where it may appear
    "JobType": ("op",), "Halt": ("op",), "OnError": ("wf",), "Queue": ("wf", "step", "op"),
    "Retry": ("wf", "step", "op"), "Gather": ("step",), "ScatterConnector": ("step",), "Tag": ("step",),
}


class Issues(object):
    def __init__(self):
        self.items = []

    def error(self, path, code, message):
        self.items.append({"path": path, "code": code, "message": message, "severity": "error"})

    def warn(self, path, code, message):
        self.items.append({"path": path, "code": code, "message": message, "severity": "warning"})

    @property
    def has_errors(self):
        return any(i["severity"] == "error" for i in self.items)


# ---------------------------------------------------------------------------------------------- types


class TypeInfo(object):
    """A parsed CWL type in the profile's vocabulary."""

    def __init__(self, base=None, array=False, optional=False, item_optional=False, bad=None):
        self.base = base
        self.array = array
        self.optional = optional
        self.item_optional = item_optional
        self.bad = bad

    def render(self):
        s = self.base or "Any"
        if self.array:
            s += "[]"
        if self.optional:
            s += "?"
        return s


def _strip_ref(name):
    return name[1:] if isinstance(name, str) and name.startswith("#") else name


def parse_type(t, records):
    """Parse a raw CWL type. ``bad`` carries the reason when the type is outside the profile."""
    if isinstance(t, str):
        s = t.strip()
        optional = s.endswith("?")
        if optional:
            s = s[:-1]
        if s.endswith("[]"):
            inner = parse_type(s[:-2], records)
            if inner.bad or inner.array:
                return inner if inner.bad else TypeInfo(bad="nested arrays are not supported")
            return TypeInfo(inner.base, True, optional, inner.optional)
        s = _strip_ref(s)
        if s in SCALARS:
            return TypeInfo(s, False, optional)
        if s in records:
            return TypeInfo(s, False, optional)
        if s == "null":
            return TypeInfo(bad="the type 'null' alone is not supported")
        return TypeInfo(bad="unsupported type '%s'" % s)
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) != 1 or len(t) != len(non_null) + 1:
            return TypeInfo(bad="type unions are only supported as [null, T] (optional)")
        inner = parse_type(non_null[0], records)
        if not inner.bad:
            if inner.optional:
                return inner
            inner.optional = True
        return inner
    if isinstance(t, dict):
        kind = t.get("type")
        if kind == "array":
            inner = parse_type(t.get("items"), records)
            if inner.bad:
                return inner
            if inner.array:
                return TypeInfo(bad="nested arrays are not supported")
            return TypeInfo(inner.base, True, False, inner.optional)
        if kind in ("record", "enum"):
            return TypeInfo(bad="inline %s types are not supported; declare records in SchemaDefRequirement" % kind)
        if isinstance(kind, (str, list)):
            return parse_type(kind, records)
    return TypeInfo(bad="unsupported type %r" % (t,))


def _is_type_object(v):
    return isinstance(v, dict) and v.get("type") in ("array", "record", "enum") and "id" not in v and (
        "items" in v or "fields" in v or "symbols" in v)


def params(x):
    """CWL inputs/outputs (map or list) -> list of param dicts with ``id`` and ``type``."""
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, dict) and not _is_type_object(v):
                d = dict(v)
            else:
                d = {"type": v}
            d["id"] = _strip_ref(k)
            out.append(d)
    elif isinstance(x, list):
        for v in x:
            if isinstance(v, dict):
                d = dict(v)
                d["id"] = _strip_ref(d.get("id"))
                out.append(d)
    return out


def _norm_in(x):
    """Step ``in`` (map or list) -> ordered list of entry dicts with ``id``."""
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, dict):
                d = dict(v)
            elif isinstance(v, (str, list)):
                d = {"source": v}
            else:
                d = {"default": v}
            d["id"] = _strip_ref(k)
            out.append(d)
    elif isinstance(x, list):
        for v in x:
            if isinstance(v, dict):
                d = dict(v)
                d["id"] = _strip_ref(d.get("id"))
                out.append(d)
    return out


def _name_list(x):
    out = []
    for v in x or []:
        n = v.get("id") if isinstance(v, dict) else v
        if isinstance(n, str):
            out.append(_strip_ref(n).rsplit("/", 1)[-1])
    return out


def _items(x):
    """steps (map or list) -> list of (id, dict)."""
    if isinstance(x, dict):
        return [(k, v) for k, v in x.items()]
    return [(_strip_ref(v.get("id")), v) for v in (x or []) if isinstance(v, dict)]


# ----------------------------------------------------------------------------------- hints/requirements


def expand_name(name, namespaces):
    """Expand ``prefix:Name`` using ``$namespaces``; leave bare names and full URIs alone."""
    if not isinstance(name, str):
        return name
    if "://" in name:
        return name
    if ":" in name:
        prefix, rest = name.split(":", 1)
        if prefix in namespaces:
            return namespaces[prefix] + rest
    return name


def _class_entries(x):
    """requirements/hints (map or list) -> list of dicts with ``class``."""
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            d = dict(v) if isinstance(v, dict) else {}
            d["class"] = k
            out.append(d)
    elif isinstance(x, list):
        out.extend(dict(v) for v in x if isinstance(v, dict))
    return out


def _has_expr(v):
    return isinstance(v, str) and ("$(" in v or "${" in v)


class Level(object):
    """The requirements and hints of one level (workflow, step or Operation)."""

    def __init__(self, kind, path):
        self.kind = kind
        self.path = path
        self.std = {}      # standard class -> dict (requirement beats hint at the same level)
        self.hysds = {}    # hysds hint name -> dict
        self.requirement_classes = set()


def _check_resource(path, val, issues):
    for key in ("ramMin", "ramMax", "tmpdirMin", "tmpdirMax", "outdirMin", "outdirMax"):
        v = val.get(key)
        if _has_expr(v):
            issues.error("%s.%s" % (path, key), "expression_unsupported",
                         "expressions are not supported in ResourceRequirement.%s" % key)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 64:
            issues.warn("%s.%s" % (path, key), "resource_units",
                        "ResourceRequirement is read in MiB; %s looks like GB (HySDS ignores it)" % v)


def collect_level(kind, path, req, hints, namespaces, issues):
    lv = Level(kind, path)
    for field, items in (("hints", hints), ("requirements", req)):
        for e in _class_entries(items):
            cls = e.get("class")
            where = "%s.%s.%s" % (path, field, cls)
            if not isinstance(cls, str):
                issues.error(where, "bad_class", "a requirement/hint needs a 'class'")
                continue
            exp = expand_name(cls, namespaces)
            if exp.startswith(HYSDS_NS):
                name = exp[len(HYSDS_NS):]
                if field == "requirements":
                    issues.error(where, "hysds_requirement", "hysds: classes are hints, not requirements: move %s to hints" % cls)
                    continue
                if name not in HYSDS_HINTS:
                    issues.error(where, "unknown_hysds_hint", "unknown hysds hint '%s' (known: %s)" % (name, ", ".join(sorted(HYSDS_HINTS))))
                    continue
                if kind not in HYSDS_HINTS[name]:
                    allowed = "/".join({"wf": "workflow", "step": "step", "op": "Operation"}[k] for k in HYSDS_HINTS[name])
                    issues.error(where, "misplaced_hint", "hysds:%s is only allowed on the %s, not on the %s" % (
                        name, allowed, {"wf": "workflow", "step": "step", "op": "Operation"}[kind]))
                    continue
                body = {k: v for k, v in e.items() if k != "class"}
                for k, v in body.items():
                    if _has_expr(v):
                        issues.error("%s.%s" % (where, k), "expression_unsupported",
                                     "expressions are not supported in hysds:%s.%s" % (name, k))
                lv.hysds[name] = body
                continue
            if ":" in exp or "://" in exp:
                issues.warn(where, "foreign_class", "ignoring %s '%s' (not a CWL or hysds class)" % (field[:-1], cls))
                continue
            if cls in REJECTED_REQUIREMENTS:
                code, msg = REJECTED_REQUIREMENTS[cls]
                issues.error(where, code, msg)
                continue
            if cls in ACCEPTED_REQUIREMENTS:
                lv.std[cls] = {k: v for k, v in e.items() if k != "class"}
                if field == "requirements":
                    lv.requirement_classes.add(cls)
                continue
            if cls in IGNORED_REQUIREMENTS:
                issues.warn(where, "ignored_requirement", "%s is ignored by HySDS (the job type defines its own runtime)" % cls)
                continue
            if cls == "ResourceRequirement":
                _check_resource(where, e, issues)
                continue
            issues.error(where, "unsupported_requirement", "%s is not supported by the HySDS CWL profile" % cls)
    return lv


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_hysds_fields(lv, issues):
    """Validate the field values of the hysds hints (and ToolTimeLimit) at one level."""
    base = lv.path
    for name, body in lv.hysds.items():
        where = "%s.hints.hysds:%s" % (base, name)
        if name == "OnError":
            if body.get("mode") not in ("kill", "drain", "continue"):
                issues.error(where + ".mode", "bad_hint", "hysds:OnError.mode must be kill, drain or continue")
        elif name == "Queue":
            if not isinstance(body.get("queue"), str) or not body["queue"]:
                issues.error(where + ".queue", "bad_hint", "hysds:Queue.queue must be a non-empty string")
        elif name == "Retry":
            mr = body.get("max_retries")
            if mr is not None and (not isinstance(mr, int) or isinstance(mr, bool) or mr < 0):
                issues.error(where + ".max_retries", "bad_hint", "hysds:Retry.max_retries must be an integer >= 0")
            ro = body.get("retry_on")
            if ro is not None:
                if not isinstance(ro, list):
                    issues.error(where + ".retry_on", "bad_hint", "hysds:Retry.retry_on must be a list")
                else:
                    for r in ro:
                        if not isinstance(r, str) or not RETRY_ON_RE.match(r):
                            issues.error(where + ".retry_on", "bad_hint", "invalid retry_on entry %r" % (r,))
            if mr is None and ro is None:
                issues.error(where, "bad_hint", "hysds:Retry needs max_retries and/or retry_on")
        elif name == "Gather":
            p = body.get("tolerated_failure_percentage")
            if not _num(p) or not (0 <= p < 100):
                issues.error(where + ".tolerated_failure_percentage", "bad_hint",
                             "hysds:Gather.tolerated_failure_percentage must be a number from 0 up to (not including) 100")
        elif name == "ScatterConnector":
            if not isinstance(body.get("job_type"), str) or not body["job_type"]:
                issues.error(where + ".job_type", "bad_hint", "hysds:ScatterConnector.job_type must be a string")
        elif name == "Tag":
            if not isinstance(body.get("tag"), str) or not body["tag"]:
                issues.error(where + ".tag", "bad_hint", "hysds:Tag.tag must be a non-empty string")
        elif name == "JobType":
            if not isinstance(body.get("job_type"), str) or not body["job_type"].strip():
                issues.error(where + ".job_type", "bad_hint", "hysds:JobType.job_type must be a non-empty string")
        elif name == "Halt":
            if body.get("status") not in ("failed", "completed"):
                issues.error(where + ".status", "bad_hint", "hysds:Halt.status must be failed or completed")
            r = body.get("reason")
            if r is not None and (not isinstance(r, str) or len(r) > 512):
                issues.error(where + ".reason", "bad_hint", "hysds:Halt.reason must be a string of at most 512 characters")
    ttl = lv.std.get("ToolTimeLimit")
    if ttl is not None:
        where = "%s.hints.ToolTimeLimit.timelimit" % base
        t = ttl.get("timelimit")
        if _has_expr(t):
            issues.error(where, "expression_unsupported", "expressions are not supported in ToolTimeLimit.timelimit; use a literal number of seconds")
        elif not _num(t) or t < 0:
            issues.error(where, "bad_hint", "ToolTimeLimit.timelimit must be a literal number of seconds (0 means no limit)")


def effective(levels, name):
    """The most specific level's hysds hint ``name`` (B11: objects are replaced whole, not merged)."""
    for lv in levels:
        if name in lv.hysds:
            return lv.hysds[name]
    return None


def effective_std(levels, cls):
    for lv in levels:
        if cls in lv.std:
            return lv.std[cls]
    return None


# --------------------------------------------------------------------------------- decide-job (section 9)


def is_decide_job(job_type):
    return isinstance(job_type, str) and job_type.split(":", 1)[0] == DECIDE_JOB


def check_when_tree(tree, path, issues, depth=1):
    if depth > WHEN_MAX_DEPTH:
        issues.error(path, "decide_cases", "condition nests deeper than %d levels" % WHEN_MAX_DEPTH)
        return
    if isinstance(tree, bool):
        return
    if not isinstance(tree, dict):
        issues.error(path, "decide_cases", "a condition must be a boolean or an object (op/right, all, any or not)")
        return
    keys = set(tree)
    if keys == {"all"} or keys == {"any"}:
        k = "all" if "all" in keys else "any"
        subs = tree[k]
        if not isinstance(subs, list) or not subs:
            issues.error(path + "." + k, "decide_cases", "'%s' must be a non-empty list of conditions" % k)
            return
        for i, s in enumerate(subs):
            check_when_tree(s, "%s.%s[%d]" % (path, k, i), issues, depth + 1)
    elif keys == {"not"}:
        check_when_tree(tree["not"], path + ".not", issues, depth + 1)
    elif "op" in keys and keys <= {"op", "left", "right"}:
        if tree["op"] not in WHEN_OPS:
            issues.error(path + ".op", "decide_cases", "op must be one of %s" % ", ".join(WHEN_OPS))
        if "right" not in tree:
            issues.error(path, "decide_cases", "a comparison needs 'right'")
        elif tree["op"] == "in" and not isinstance(tree["right"], list):
            issues.error(path + ".right", "decide_cases", "'right' of op 'in' must be a list")
    else:
        issues.error(path, "decide_cases", "unrecognised condition %s (expected op/right, all, any or not)" % sorted(keys))


def check_decide_cases(cases, path, issues):
    """Validate the ``cases`` value of ``job-workflow-decide:v1``. Returns the list of case names."""
    names = []
    if not isinstance(cases, list):
        issues.error(path, "decide_cases", "cases must be a list of {name, when}")
        return names
    for i, c in enumerate(cases):
        w = "%s[%d]" % (path, i)
        if not isinstance(c, dict) or set(c) - {"name", "when"} or "name" not in c or "when" not in c:
            issues.error(w, "decide_cases", "each case must be an object with exactly 'name' and 'when'")
            continue
        n = c["name"]
        if not isinstance(n, str) or not CASE_NAME_RE.match(n):
            issues.error(w + ".name", "decide_cases", "case name %r must match %s" % (n, CASE_NAME_RE.pattern))
        elif n in CASE_RESERVED:
            issues.error(w + ".name", "decide_cases", "case name '%s' is reserved" % n)
        elif n in names:
            issues.error(w + ".name", "decide_cases", "duplicate case name '%s'" % n)
        else:
            names.append(n)
        check_when_tree(c["when"], w + ".when", issues)
    return names


# ------------------------------------------------------------------------------------------------ analysis


def _parse_source(src, path, issues):
    """'name' or 'step/out' -> ('input', name) / ('step', step, out); None on a malformed value."""
    if not isinstance(src, str) or not src.strip():
        issues.error(path, "bad_source", "a source must be a non-empty string")
        return None
    s = _strip_ref(src.strip())
    if "/" in s:
        step, out = s.split("/", 1)
        if not step or not out or "/" in out:
            issues.error(path, "bad_source", "malformed source '%s'" % src)
            return None
        return ("step", step, out)
    return ("input", s)


def _schema_version(doc, namespaces):
    for k, v in doc.items():
        if expand_name(k, namespaces) == SCHEMA_NS + "version":
            return v
    return None


def analyze(doc, run_resolver=None):
    issues = Issues()
    model = _analyze(doc, run_resolver, issues)
    return model, issues.items


def _check_param_decl(p, path, issues):
    for k in PARAM_REJECTED:
        if k in p:
            issues.error("%s.%s" % (path, k), "file_field_unsupported",
                         "'%s' is not supported (File/Directory values are passed to jobs as URL strings)" % k)
    d = p.get("default")
    if isinstance(d, dict) and d.get("class") in ("File", "Directory"):
        for k in FILE_FIELDS:
            if k in d:
                issues.error("%s.default.%s" % (path, k), "file_field_unsupported",
                             "'%s' is not supported on File objects" % k)


def _default_value(d):
    """A File/Directory object (also inside arrays) flattened to its location (or path); other values unchanged."""
    if isinstance(d, list):
        return [_default_value(x) for x in d]
    if isinstance(d, dict) and d.get("class") in ("File", "Directory"):
        return d.get("location") or d.get("path")
    return d


def _analyze(doc, run_resolver, issues):
    if not isinstance(doc, dict):
        issues.error("", "not_a_workflow", "the document must be a CWL Workflow")
        return None
    if doc.get("cwlVersion") != "v1.2":
        issues.error("cwlVersion", "cwl_version", "cwlVersion must be v1.2 (got %r)" % (doc.get("cwlVersion"),))
    cls = doc.get("class")
    if cls != "Workflow":
        msg = {"CommandLineTool": "a CommandLineTool cannot be registered as a workflow",
               "ExpressionTool": "an ExpressionTool cannot be registered as a workflow"}.get(cls, "class must be Workflow")
        issues.error("class", "not_a_workflow", "%s (the document class is %r)" % (msg, cls))
        return None
    ns = doc.get("$namespaces") if isinstance(doc.get("$namespaces"), dict) else {}
    if HYSDS_NS not in ns.values():
        issues.error("$namespaces", "missing_namespace",
                     "$namespaces must declare the hysds namespace %s (any prefix)" % HYSDS_NS)
    version = _schema_version(doc, ns)
    if version is None or (isinstance(version, str) and not version.strip()):
        issues.error("s:version", "missing_version", "s:version is required (declare s: %s and set s:version)" % SCHEMA_NS)
    version = str(version) if version is not None else None
    wf_id = doc.get("id")
    if not isinstance(wf_id, str) or not DOC_ID_RE.match(_strip_ref(wf_id) or ""):
        issues.error("id", "bad_id", "the workflow id is required and must match %s" % DOC_ID_RE.pattern)
        wf_id = None
    else:
        wf_id = _strip_ref(wf_id)

    wf_level = collect_level("wf", "", doc.get("requirements"), doc.get("hints"), ns, issues)
    check_hysds_fields(wf_level, issues)

    # records
    records = set()
    sd = wf_level.std.get("SchemaDefRequirement")
    for i, t in enumerate((sd or {}).get("types") or []):
        if isinstance(t, dict) and t.get("type") == "record" and isinstance(t.get("name"), str):
            records.add(_strip_ref(t["name"]))
        else:
            issues.error("requirements.SchemaDefRequirement.types[%d]" % i, "unsupported_type",
                         "SchemaDefRequirement supports record types only")
    for i, t in enumerate((sd or {}).get("types") or []):
        if isinstance(t, dict) and t.get("type") == "record":
            for f in params(t.get("fields")):
                ft = parse_type(f.get("type"), records)
                if ft.bad:
                    issues.error("requirements.SchemaDefRequirement.types[%d].fields.%s" % (i, f.get("id")), "unsupported_type", ft.bad)

    # inputs
    inputs = {}
    for p in params(doc.get("inputs")):
        pid = p["id"]
        path = "inputs.%s" % pid
        if not isinstance(pid, str) or not NAME_RE.match(pid):
            issues.error(path, "bad_name", "input names must match %s" % NAME_RE.pattern)
            continue
        ti = parse_type(p.get("type"), records)
        if ti.bad:
            issues.error(path + ".type", "unsupported_type", ti.bad)
            ti = TypeInfo("Any")
        _check_param_decl(p, path, issues)
        inputs[pid] = {"type": ti, "has_default": "default" in p, "default": _default_value(p.get("default")),
                       "label": p.get("label") if isinstance(p.get("label"), str) else None}

    steps = {}
    step_items = _items(doc.get("steps"))
    if not step_items:
        issues.error("steps", "no_steps", "a workflow needs at least one step")
    for sid, sd_ in step_items:
        if not isinstance(sd_, dict) or not isinstance(sid, str):
            issues.error("steps", "bad_step", "malformed step")
            continue
        steps[sid] = _analyze_step(sid, sd_, wf_level, ns, records, run_resolver, issues)

    # scatter/multi-source declarations needed at workflow or step level
    def declared(cls_name, step=None):
        if cls_name in wf_level.requirement_classes:
            return True
        return step is not None and cls_name in step["level"].requirement_classes

    for sid, st in steps.items():
        if st.get("scatter") and not declared("ScatterFeatureRequirement", st):
            issues.error("steps.%s.scatter" % sid, "missing_requirement",
                         "scatter requires ScatterFeatureRequirement in requirements")
        for e in st["in_entries"]:
            if e["list_source"] and not declared("MultipleInputFeatureRequirement", st):
                issues.error("steps.%s.in.%s.source" % (sid, e["id"]), "missing_requirement",
                             "a list source requires MultipleInputFeatureRequirement in requirements")

    # outputs
    outputs = {}
    for p in params(doc.get("outputs")):
        pid = p["id"]
        path = "outputs.%s" % pid
        if not isinstance(pid, str) or not NAME_RE.match(pid):
            issues.error(path, "bad_name", "output names must match %s" % NAME_RE.pattern)
            continue
        ti = parse_type(p.get("type"), records)
        if ti.bad:
            issues.error(path + ".type", "unsupported_type", ti.bad)
            ti = TypeInfo("Any")
        _check_param_decl(p, path, issues)
        osrc = p.get("outputSource")
        srcs = osrc if isinstance(osrc, list) else ([osrc] if osrc is not None else [])
        if not srcs:
            issues.error(path + ".outputSource", "missing_output_source", "output '%s' needs an outputSource" % pid)
        parsed = []
        for s in srcs:
            r = _parse_source(s, path + ".outputSource", issues)
            if r:
                parsed.append(r)
        lm = p.get("linkMerge")
        pick = p.get("pickValue")
        if lm == "merge_nested":
            issues.error(path + ".linkMerge", "merge_nested_unsupported", "linkMerge: merge_nested is not supported; use merge_flattened")
        if pick is not None and pick not in PICKS:
            issues.error(path + ".pickValue", "bad_pick", "pickValue must be one of %s" % ", ".join(PICKS))
        listed = isinstance(osrc, list)
        if listed and not declared("MultipleInputFeatureRequirement"):
            issues.error(path + ".outputSource", "missing_requirement",
                         "a list outputSource requires MultipleInputFeatureRequirement in requirements")
        outputs[pid] = {"type": ti, "sources": parsed, "pick": pick, "link_merge": lm}

    on_error = effective([wf_level], "OnError")
    model = {
        "id": wf_id, "version": version,
        "label": doc.get("label") if isinstance(doc.get("label"), str) else None,
        "description": doc.get("doc") if isinstance(doc.get("doc"), str) else None,
        "on_error": (on_error or {}).get("mode", "kill"),
        "inputs": inputs, "steps": steps, "outputs": outputs, "wf_level": wf_level, "records": records,
    }
    _check_graph(model, issues)
    return model


def _analyze_step(sid, st, wf_level, ns, records, run_resolver, issues):
    path = "steps.%s" % sid
    if not NAME_RE.match(sid) or sid in RESERVED_STEP_IDS:
        issues.error(path, "bad_name", "step id '%s' must match %s and not be one of %s" % (
            sid, NAME_RE.pattern, ", ".join(RESERVED_STEP_IDS)))
    step = {"id": sid, "path": path, "kind": None, "job_type": None, "halt": None, "when": None, "when_flag": None,
            "scatter": None, "scatter_method": None, "in_entries": [], "out": _name_list(st.get("out")),
            "op_inputs": {}, "op_outputs": {}, "levels": [], "level": None, "ok": True}
    run = st.get("run")
    if isinstance(run, str):
        resolved = run_resolver(run) if run_resolver else None
        if not isinstance(resolved, dict):
            issues.error(path + ".run", "unresolved_run",
                         "run reference '%s' cannot be resolved; inline a class: Operation with a hysds:JobType hint" % run)
            step["ok"] = False
            run = {}
        else:
            run = resolved
    if not isinstance(run, dict):
        issues.error(path + ".run", "bad_run", "run must be an inline class: Operation")
        step["ok"] = False
        run = {}
    rcls = run.get("class")
    if rcls == "Operation" or not run:
        pass
    elif rcls == "Workflow":
        issues.error(path + ".run", "subworkflow_unsupported", "subworkflows are not supported")
        step["ok"] = False
    elif rcls in ("CommandLineTool", "ExpressionTool"):
        issues.error(path + ".run", "tool_unsupported",
                     "%s steps are not supported; bind the step to a registered job type with an Operation and a hysds:JobType hint" % rcls)
        step["ok"] = False
    else:
        issues.error(path + ".run", "bad_run", "run must be class: Operation (got %r)" % (rcls,))
        step["ok"] = False

    step_lv = collect_level("step", path, st.get("requirements"), st.get("hints"), ns, issues)
    op_lv = collect_level("op", path + ".run", run.get("requirements"), run.get("hints"), ns, issues)
    check_hysds_fields(step_lv, issues)
    check_hysds_fields(op_lv, issues)
    step["level"] = step_lv
    step["levels"] = [op_lv, step_lv, wf_level]  # most specific first (B11)
    step["op_level"] = op_lv

    # binding
    jt, halt = op_lv.hysds.get("JobType"), op_lv.hysds.get("Halt")
    if step["ok"]:
        if jt and halt:
            issues.error(path + ".run.hints", "double_binding", "the Operation has both hysds:JobType and hysds:Halt; use exactly one")
        elif jt is None and halt is None:
            issues.error(path + ".run.hints", "unbound_run",
                         "step '%s' is not bound: add hysds:JobType {job_type: ...} (or hysds:Halt) to the Operation's hints" % sid)
        elif jt:
            step["kind"] = "job"
            step["job_type"] = jt.get("job_type") if isinstance(jt.get("job_type"), str) else None
        else:
            step["kind"] = "halt"
            step["halt"] = {"status": halt.get("status"), "reason": halt.get("reason") or ""}

    # operation inputs/outputs
    for p in params(run.get("inputs")):
        pid = p["id"]
        ti = parse_type(p.get("type"), records)
        if ti.bad:
            issues.error("%s.run.inputs.%s.type" % (path, pid), "unsupported_type", ti.bad)
            ti = TypeInfo("Any")
        _check_param_decl(p, "%s.run.inputs.%s" % (path, pid), issues)
        step["op_inputs"][pid] = {"type": ti, "has_default": "default" in p, "default": _default_value(p.get("default"))}
    for p in params(run.get("outputs")):
        pid = p["id"]
        ti = parse_type(p.get("type"), records)
        if ti.bad:
            issues.error("%s.run.outputs.%s.type" % (path, pid), "unsupported_type", ti.bad)
            ti = TypeInfo("Any")
        _check_param_decl(p, "%s.run.outputs.%s" % (path, pid), issues)
        step["op_outputs"][pid] = ti
    if step["kind"] == "halt":
        if step["op_inputs"] or step["op_outputs"]:
            issues.error(path + ".run", "halt_io", "a hysds:Halt Operation must declare no inputs and no outputs")
        if step["out"]:
            issues.error(path + ".out", "halt_io", "a halt step has no outputs (out must be empty)")
    else:
        for o in step["out"]:
            if step["op_outputs"] and o not in step["op_outputs"] and not (is_decide_job(step["job_type"])):
                issues.error(path + ".out", "unknown_output", "step output '%s' is not declared by the Operation" % o)

    # in entries
    for e in _norm_in(st.get("in")):
        eid = e["id"]
        epath = "%s.in.%s" % (path, eid)
        if "valueFrom" in e:
            issues.error(epath + ".valueFrom", "value_from_unsupported",
                         "valueFrom is not supported (expressions are not allowed); compute the value in a job")
        for k in ("loadContents", "loadListing"):
            if k in e:
                issues.error("%s.%s" % (epath, k), "file_field_unsupported", "'%s' is not supported" % k)
        if e.get("linkMerge") == "merge_nested":
            issues.error(epath + ".linkMerge", "merge_nested_unsupported", "linkMerge: merge_nested is not supported; use merge_flattened")
        elif e.get("linkMerge") not in (None, "merge_flattened"):
            issues.error(epath + ".linkMerge", "bad_link_merge", "linkMerge must be merge_flattened")
        pick = e.get("pickValue")
        if pick is not None and pick not in PICKS:
            issues.error(epath + ".pickValue", "bad_pick", "pickValue must be one of %s" % ", ".join(PICKS))
        _check_param_decl(e, epath, issues)
        raw = e.get("source")
        listed = isinstance(raw, list)
        srcs = raw if listed else ([raw] if raw is not None else [])
        parsed = []
        for s in srcs:
            r = _parse_source(s, epath + ".source", issues)
            if r:
                parsed.append(r)
        step["in_entries"].append({
            "id": eid, "path": epath, "sources": parsed, "list_source": listed, "pick": pick,
            "link_merge": e.get("linkMerge"), "has_default": "default" in e, "default": _default_value(e.get("default")),
        })

    # when
    when = st.get("when")
    if when is not None:
        if not isinstance(when, str):
            issues.error(path + ".when", "bad_when", "when must be the string $(inputs.<flag>)")
        else:
            m = WHEN_RE.match(when.strip())
            if not m:
                if _has_expr(when):
                    issues.error(path + ".when", "js_unsupported", JS_MESSAGE)
                else:
                    issues.error(path + ".when", "bad_when", "when must be the string $(inputs.<flag>)")
            else:
                step["when_flag"] = m.group(1)
                if not any(e["id"] == step["when_flag"] for e in step["in_entries"]):
                    issues.error(path + ".when", "bad_when", "when refers to inputs.%s, which is not an 'in' entry of the step" % m.group(1))
                    step["when_flag"] = None
        step["has_when"] = True
    else:
        step["has_when"] = False

    # scatter
    sc = st.get("scatter")
    if sc is not None:
        if isinstance(sc, list):
            if len(sc) != 1:
                issues.error(path + ".scatter", "scatter_unsupported",
                             "nested and cross-product scatters are not supported: scatter exactly one input")
                sc = None
            else:
                sc = sc[0]
        if isinstance(sc, str):
            step["scatter"] = _strip_ref(sc).rsplit("/", 1)[-1]
        elif sc is not None:
            issues.error(path + ".scatter", "bad_scatter", "scatter must name one input")
        sm = st.get("scatterMethod")
        if sm not in (None, "dotproduct"):
            issues.error(path + ".scatterMethod", "scatter_unsupported",
                         "scatterMethod %s is not supported (only dotproduct, or omit it)" % sm)
        if step["scatter"]:
            if not any(e["id"] == step["scatter"] for e in step["in_entries"]):
                issues.error(path + ".scatter", "bad_scatter", "scatter input '%s' is not an 'in' entry of the step" % step["scatter"])
            if step["kind"] == "halt":
                issues.error(path + ".scatter", "bad_scatter", "a halt step cannot scatter")
            if when is not None:
                issues.error(path + ".when", "scatter_with_when",
                             "scatter and when cannot be combined on one step (B20); put the condition upstream")
    elif st.get("scatterMethod") is not None:
        issues.error(path + ".scatterMethod", "scatter_unsupported", "scatterMethod without scatter")

    # step-level scatter-only hints
    for h in ("Gather", "ScatterConnector"):
        if h in step_lv.hysds and not step["scatter"]:
            issues.error("%s.hints.hysds:%s" % (path, h), "misplaced_hint", "hysds:%s is only allowed on a scattered step" % h)
    gather = effective(step["levels"], "Gather") or {}
    step["tolerance"] = gather.get("tolerated_failure_percentage", 0) if _num(gather.get("tolerated_failure_percentage", 0)) else 0

    # missing required Operation inputs
    if step["kind"] == "job":
        bound = {e["id"]: e for e in step["in_entries"]}
        for name, op in step["op_inputs"].items():
            e = bound.get(name)
            has = e is not None and (e["sources"] or e["has_default"])
            if not has and not op["type"].optional and not op["has_default"]:
                if not is_decide_job(step["job_type"]) or name in ("cases",):
                    issues.error("%s.in.%s" % (path, name), "missing_input",
                                 "required Operation input '%s' is not bound (add it to 'in', give it a default, or make its type optional)" % name)
        for e in step["in_entries"]:
            if e["id"] in step["op_inputs"] and not (e["sources"] or e["has_default"]):
                issues.error(e["path"], "bad_in", "'in' entry '%s' has neither a source nor a default" % e["id"])

    if step["kind"] == "job" and is_decide_job(step["job_type"]):
        _check_decide(step, issues)
    return step


def _check_decide(step, issues):
    path = step["path"]
    cases_entry = next((e for e in step["in_entries"] if e["id"] == "cases"), None)
    names = None
    if cases_entry is not None and cases_entry["has_default"] and not cases_entry["sources"]:
        names = check_decide_cases(cases_entry["default"], path + ".in.cases.default", issues)
    elif cases_entry is None and step["op_inputs"].get("cases", {}).get("has_default"):
        names = check_decide_cases(step["op_inputs"]["cases"]["default"], path + ".run.inputs.cases.default", issues)
    otherwise = DECIDE_OTHERWISE_DEFAULT
    oe = next((e for e in step["in_entries"] if e["id"] == "otherwise"), None)
    if oe is not None and oe["has_default"] and isinstance(oe["default"], str):
        otherwise = oe["default"]
    elif "otherwise" in step["op_inputs"] and isinstance(step["op_inputs"]["otherwise"]["default"], str):
        otherwise = step["op_inputs"]["otherwise"]["default"]
    if not CASE_NAME_RE.match(otherwise or ""):
        issues.error(path + ".in.otherwise", "decide_cases", "the otherwise name %r must match %s" % (otherwise, CASE_NAME_RE.pattern))
    if names is not None:
        if otherwise in names:
            issues.error(path + ".in.otherwise", "decide_cases", "the otherwise name '%s' collides with a case name" % otherwise)
        allowed = set(names) | {otherwise}
        for o in step["out"]:
            if o not in allowed:
                issues.error(path + ".out", "decide_out", "decide output '%s' is neither a case name nor the otherwise name (%s)" % (o, ", ".join(sorted(allowed))))
    for o, ti in step["op_outputs"].items():
        if not (ti.base == "boolean" and not ti.array and not ti.optional):
            issues.error("%s.run.outputs.%s" % (path, o), "decide_out", "decide outputs must be non-optional boolean")
    step["decide"] = {"cases": names, "otherwise": otherwise}


# ------------------------------------------------------------------------------------- graph/null-safety


def _source_info(model, src):
    """Describe a parsed source: type, whether the whole value may be null, items null, skip flag."""
    if src[0] == "input":
        inp = model["inputs"].get(src[1])
        if inp is None:
            return None
        return {"type": inp["type"], "null": inp["type"].optional and not inp["has_default"], "items_null": False,
                "skip_flag": None, "is_array": inp["type"].array, "producer": None}
    st = model["steps"].get(src[1])
    if st is None or src[2] not in st["out"] or st["kind"] == "halt":
        return None
    ti = st["op_outputs"].get(src[2], TypeInfo("Any"))
    skipped = st["has_when"]
    flag_src = _flag_source(st) if skipped else None
    return {"type": ti, "null": skipped or ti.optional, "items_null": bool(st["scatter"]) and st["tolerance"] > 0,
            "skip_flag": flag_src if skipped and not ti.optional else None,
            "is_array": bool(st["scatter"]) or ti.array, "producer": st["id"], "skipped": skipped}


def _flag_source(step):
    if not step["when_flag"]:
        return None
    e = next((e for e in step["in_entries"] if e["id"] == step["when_flag"]), None)
    if e and len(e["sources"]) == 1 and not e["list_source"]:
        return e["sources"][0]
    return None


def _check_graph(model, issues):
    steps = model["steps"]
    # resolve every source
    for st in steps.values():
        for e in st["in_entries"]:
            for s in e["sources"]:
                if s[0] == "input":
                    if s[1] not in model["inputs"]:
                        issues.error(e["path"] + ".source", "unknown_source", "'%s' is not a workflow input" % s[1])
                else:
                    tgt = steps.get(s[1])
                    if tgt is None:
                        issues.error(e["path"] + ".source", "unknown_source", "'%s' names no step" % s[1])
                    elif tgt["kind"] == "halt":
                        issues.error(e["path"] + ".source", "unknown_source", "'%s' is a halt step and has no outputs" % s[1])
                    elif s[2] not in tgt["out"]:
                        issues.error(e["path"] + ".source", "unknown_source", "step '%s' does not declare output '%s'" % (s[1], s[2]))
    for name, o in model["outputs"].items():
        for s in o["sources"]:
            if s[0] == "input":
                if s[1] not in model["inputs"]:
                    issues.error("outputs.%s.outputSource" % name, "unknown_source", "'%s' is not a workflow input" % s[1])
            else:
                tgt = steps.get(s[1])
                if tgt is None or tgt["kind"] == "halt" or s[2] not in tgt["out"]:
                    issues.error("outputs.%s.outputSource" % name, "unknown_source", "'%s/%s' is not a step output" % (s[1], s[2]))

    _check_cycles(model, issues)
    if issues.has_errors and any(i["code"] in ("unknown_source", "bad_source") for i in issues.items):
        return

    for st in steps.values():
        _check_when_flag(model, st, issues)
        _check_scatter_input(model, st, issues)
        _check_sinks(model, st, issues)
    _check_outputs(model, issues)


def _check_cycles(model, issues):
    deps = {sid: [] for sid in model["steps"]}
    for sid, st in model["steps"].items():
        for e in st["in_entries"]:
            for s in e["sources"]:
                if s[0] == "step" and s[1] in deps and s[1] not in deps[sid]:
                    deps[sid].append(s[1])
    state = {}

    def visit(n, stack):
        state[n] = 1
        for d in deps[n]:
            if state.get(d) == 1:
                issues.error("steps.%s" % n, "cycle", "dependency cycle: %s" % " -> ".join(stack + [n, d]))
            elif d not in state:
                visit(d, stack + [n])
        state[n] = 2

    for n in deps:
        if n not in state:
            visit(n, [])


def _check_when_flag(model, st, issues):
    flag = st["when_flag"]
    if not flag:
        return
    e = next(e for e in st["in_entries"] if e["id"] == flag)
    path = "steps.%s.when" % st["id"]
    if e["list_source"] or len(e["sources"]) != 1 or e["pick"]:
        issues.error(path, "when_flag_invalid", "the when flag '%s' must come from exactly one source (no list, no pickValue)" % flag)
        return
    s = e["sources"][0]
    if s[0] == "input":
        inp = model["inputs"][s[1]]
        t = inp["type"]
        if not (t.base == "boolean" and not t.array) or (t.optional and not inp["has_default"]):
            issues.error(path, "when_flag_invalid",
                         "the when flag '%s' must be a non-optional boolean workflow input or have a default" % flag)
    else:
        prod = model["steps"][s[1]]
        t = prod["op_outputs"].get(s[2])
        if t is None or not (t.base == "boolean" and not t.array and not t.optional):
            issues.error(path, "when_flag_invalid", "the when flag '%s' must be a non-optional boolean output of '%s'" % (flag, s[1]))
        elif prod["has_when"]:
            issues.error(path, "when_flag_invalid",
                         "the when flag '%s' comes from step '%s', which can itself be skipped" % (flag, s[1]))
        elif prod["scatter"]:
            issues.error(path, "when_flag_invalid", "the when flag '%s' comes from scattered step '%s' (an array)" % (flag, s[1]))


def _check_scatter_input(model, st, issues):
    if not st["scatter"]:
        return
    e = next((e for e in st["in_entries"] if e["id"] == st["scatter"]), None)
    if e is None:
        return
    path = e["path"] + ".source"
    makes_array = e["pick"] == "all_non_null" or (e["list_source"] and e["link_merge"] == "merge_flattened")
    for s in e["sources"]:
        info = _source_info(model, s)
        if info is None:
            continue
        if info["null"] and not (e["has_default"] or e["pick"] in PICKS):
            issues.error(path, "scatter_input_null",
                         "a scatter input must be a non-null array; '%s' can be null (use pickValue or a default, or make the producer unconditional)" % _src_str(s))
        elif not info["is_array"] and not makes_array and len(e["sources"]) == 1 and not e["list_source"]:
            issues.error(path, "scatter_input_not_array", "scatter input '%s' must be an array; '%s' is %s" % (
                st["scatter"], _src_str(s), info["type"].render()))


def _src_str(s):
    return s[1] if s[0] == "input" else "%s/%s" % (s[1], s[2])


def _check_sinks(model, st, issues):
    if st["kind"] != "job":
        return
    when_src = _flag_source(st)
    for e in st["in_entries"]:
        op = st["op_inputs"].get(e["id"])
        if op is None:
            continue  # not a job parameter: used by 'when' only, not passed (section 4)
        sink = op["type"]
        scattered = st["scatter"] == e["id"]
        continue_whole = not scattered  # a scatter input's own null-ness is the scatter-input rule
        safe_pick = e["pick"] in PICKS
        for s in e["sources"]:
            info = _source_info(model, s)
            if info is None:
                continue
            path = e["path"] + ".source"
            if info["null"] and continue_whole:
                same_flag = info["skip_flag"] is not None and info["skip_flag"] == when_src
                if not (safe_pick or e["has_default"] or sink.optional or same_flag):
                    issues.error(path, "null_unsafe",
                                 "'%s' can be null (its step is conditional or its type is optional) but input '%s' is not optional; "
                                 "use pickValue: first_non_null or the_only_non_null, give the input a default, or make its type optional" % (
                                     _src_str(s), e["id"]))
            if info["items_null"]:
                items_ok = e["pick"] == "all_non_null" or (sink.array and sink.item_optional) or (scattered and sink.optional)
                if not items_ok:
                    issues.error(path, "null_unsafe_scatter",
                                 "'%s' comes from a scatter that tolerates failures, so its items can be null; use "
                                 "source: [%s], linkMerge: merge_flattened, pickValue: all_non_null or make the item type optional" % (
                                     _src_str(s), _src_str(s)))


def _check_outputs(model, issues):
    halts_completed = any(st["kind"] == "halt" and (st["halt"] or {}).get("status") == "completed"
                          for st in model["steps"].values())
    for name, o in model["outputs"].items():
        path = "outputs.%s" % name
        if halts_completed and not o["type"].optional:
            issues.error(path + ".type", "output_not_optional",
                         "output '%s' must be optional (?) because a hysds:Halt has status: completed (B21)" % name)
        safe_pick = o["pick"] in PICKS
        for s in o["sources"]:
            info = _source_info(model, s)
            if info is None:
                continue
            if info["null"] and not (safe_pick or o["type"].optional):
                issues.error(path + ".outputSource", "null_unsafe_output",
                             "output '%s' is fed from '%s', which can be null, so the output type must be optional (?) or use pickValue" % (name, _src_str(s)))
            if info["items_null"] and not (o["pick"] == "all_non_null" or (o["type"].array and o["type"].item_optional)):
                issues.error(path + ".outputSource", "null_unsafe_scatter",
                             "output '%s' is fed from a scatter that tolerates failures (items can be null); use pickValue: all_non_null" % name)

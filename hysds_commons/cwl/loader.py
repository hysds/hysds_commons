"""Load a CWL document and validate it with cwltool, as a library (no subprocess, no network).

``load_document`` parses the YAML/JSON bytes, strips ``$schemas`` (registration never fetches anything),
selects the single Workflow of a ``$graph`` and inlines ``run: "#id"`` Operation references (the
``cwltool --pack`` form). ``validate_with_cwltool`` runs cwltool's loader and static checker on the
result and returns structured errors and warnings. ``hysds:`` "Unknown hint" / "undefined reference"
noise is filtered (PLAN section 8, step 1).
"""

import copy
import hashlib
import io
import json
import logging
import re
import threading

HYSDS_NS = "https://github.com/hysds/hysds_commons/cwl/v1#"
SCHEMA_NS = "https://schema.org/"

_CAPTURE_LOCK = threading.Lock()
_WS = re.compile(r"\s+")
_LOCATION = re.compile(r"^(?:.*?):(\d+):(\d+):")


def issue(path, code, message):
    return {"path": path, "code": code, "message": message}


class LoadedDocument(object):
    """The parsed document: ``doc`` is the Workflow as a plain dict, ``sha256`` is of the raw bytes."""

    def __init__(self, doc, sha256, errors, warnings):
        self.doc = doc
        self.sha256 = sha256
        self.errors = errors
        self.warnings = warnings


def _to_plain(obj):
    """ruamel CommentedMap/Seq -> plain dict/list (JSON round trip keeps only JSON types)."""
    return json.loads(json.dumps(obj, default=str))


def _parse(raw_text):
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe", pure=True)
    return _to_plain(yaml.load(io.StringIO(raw_text)))


def _strip_hash(ident):
    return ident[1:] if isinstance(ident, str) and ident.startswith("#") else ident


def _map_items(x, key="id"):
    """CWL map-or-list -> list of ``(id, value)`` where list entries carry their own ``id``."""
    if isinstance(x, dict):
        return list(x.items())
    out = []
    for item in x or []:
        if isinstance(item, dict):
            out.append((_strip_hash(item.get(key)), item))
    return out


def _select_workflow(doc, errors):
    graph = doc.get("$graph")
    if not isinstance(graph, list):
        return doc
    workflows = [g for g in graph if isinstance(g, dict) and g.get("class") == "Workflow"]
    if len(workflows) != 1:
        errors.append(issue("$graph", "graph_workflows",
                            "a $graph document must contain exactly one Workflow (found %d)" % len(workflows)))
        return None
    by_id = {}
    for g in graph:
        if isinstance(g, dict) and g.get("id") is not None:
            by_id[_strip_hash(g["id"])] = g
    wf = copy.deepcopy(workflows[0])
    for k in ("cwlVersion", "$namespaces", "$schemas"):
        if k in doc and k not in wf:
            wf[k] = doc[k]
    # keep s:version and friends declared at the top of the graph document
    for k, v in doc.items():
        if k not in wf and k != "$graph":
            wf[k] = v
    if isinstance(wf.get("id"), str):
        wf["id"] = _strip_hash(wf["id"])
    for sid, step in _map_items(wf.get("steps")):
        run = step.get("run") if isinstance(step, dict) else None
        if isinstance(run, str) and run.startswith("#"):
            target = by_id.get(run[1:])
            if target is None:
                errors.append(issue("steps.%s.run" % sid, "unresolved_run",
                                    "run reference '%s' does not name an entry of the $graph" % run))
            else:
                op = copy.deepcopy(target)
                op.pop("id", None)
                step["run"] = op
    return wf


def load_document(source):
    """Parse ``source`` (bytes or str of YAML/JSON). Returns a ``LoadedDocument``."""
    raw = source if isinstance(source, bytes) else str(source).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    errors, warnings = [], []
    try:
        doc = _parse(raw.decode("utf-8"))
    except Exception as e:  # yaml syntax error, bad encoding
        errors.append(issue("", "parse_error", "the document is not valid YAML/JSON: %s" % _WS.sub(" ", str(e))))
        return LoadedDocument(None, sha, errors, warnings)
    if not isinstance(doc, dict):
        errors.append(issue("", "parse_error", "the document must be a CWL object (mapping)"))
        return LoadedDocument(None, sha, errors, warnings)
    if "$schemas" in doc:
        doc.pop("$schemas")
        warnings.append(issue("$schemas", "schemas_ignored", "$schemas is ignored: registration never fetches anything"))
    wf = _select_workflow(doc, errors)
    if isinstance(wf, dict) and "$schemas" in wf:
        wf.pop("$schemas")
    return LoadedDocument(wf, sha, errors, warnings)


def inline_run_refs(doc, run_resolver):
    """Replace string ``run`` references (registry-generated Operation files, B17) by the resolved dict."""
    if not isinstance(doc, dict):
        return doc
    wf = copy.deepcopy(doc)
    steps = wf.get("steps")
    for sid, step in _map_items(steps):
        run = step.get("run") if isinstance(step, dict) else None
        if isinstance(run, str):
            resolved = run_resolver(run) if run_resolver else None
            if isinstance(resolved, dict):
                step["run"] = copy.deepcopy(resolved)
            # unresolved references are reported by the profile check ("unresolved_run")
    return wf


class _Capture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self, logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


def _is_hysds_noise(msg):
    return HYSDS_NS in msg and ("Unknown hint" in msg or "undefined reference" in msg)


def _cwl_error(exc):
    text = str(exc)
    flat = _WS.sub(" ", text).strip()
    m = _LOCATION.match(flat)
    line = int(m.group(1)) if m else None
    return issue("", "cwl_validation", flat), line


def validate_with_cwltool(doc, uri_hint="workflow.cwl"):
    """Run cwltool's loader (schema validation) and static checker on ``doc`` (a plain dict).

    Returns ``(errors, warnings)``, each a list of ``{path, code, message}``. No network access and no
    subprocess; cwltool log output is captured, not printed. Not re-entrant (it temporarily swaps the
    cwltool/schema-salad logger handlers), so calls are serialised with a lock.
    """
    from cwltool.context import LoadingContext
    from cwltool.load_tool import load_tool
    from cwltool.workflow import default_make_tool

    errors, warnings = [], []
    cap = _Capture()
    names = ("cwltool", "salad")
    with _CAPTURE_LOCK:
        saved = {}
        for n in names:
            lg = logging.getLogger(n)
            saved[n] = (lg.handlers[:], lg.propagate, lg.level)
            lg.handlers = [cap]
            lg.propagate = False
            lg.setLevel(logging.INFO)
        try:
            lc = LoadingContext()
            lc.do_validate = True
            lc.construct_tool_object = default_make_tool
            lc.fetcher_constructor = _no_network_fetcher()
            try:
                load_tool(_with_base_uri(doc, uri_hint), lc)
            except Exception as e:  # ValidationException, WorkflowException, ...
                err, _line = _cwl_error(e)
                errors.append(err)
        finally:
            for n, (h, p, lvl) in saved.items():
                lg = logging.getLogger(n)
                lg.handlers, lg.propagate = h, p
                lg.setLevel(lvl)
    seen = set()
    for lvl, msg in cap.records:
        if lvl < logging.WARNING:
            continue
        flat = _WS.sub(" ", msg).strip()
        if _is_hysds_noise(flat) or flat in seen:
            continue
        seen.add(flat)
        warnings.append(issue("", "cwl_warning", flat))
    return errors, warnings


def _with_base_uri(doc, uri_hint):
    """A copy of ``doc`` whose ``id`` is an absolute URI, so cwltool never resolves it against the cwd."""
    d = copy.deepcopy(doc)
    ident = str(d.get("id") or "workflow")
    d["id"] = "file:///hysds-cwl/%s" % uri_hint if "#" in ident or "/" in ident else "file:///hysds-cwl/%s#%s" % (uri_hint, ident)
    return d


def _no_network_fetcher():
    """A cwltool fetcher that refuses http(s) so validation can never reach the network."""
    from schema_salad.ref_resolver import DefaultFetcher

    class NoNetworkFetcher(DefaultFetcher):
        def fetch_text(self, url, content_types=None):
            if url.startswith(("http://", "https://")):
                raise RuntimeError("network access is disabled while validating CWL (%s)" % url)
            return DefaultFetcher.fetch_text(self, url, content_types)

        def check_exists(self, url):
            if url.startswith(("http://", "https://")):
                return False
            return DefaultFetcher.check_exists(self, url)

    return NoNetworkFetcher

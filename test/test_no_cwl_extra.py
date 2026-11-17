"""hysds_commons without the optional ``cwl`` extra (cwltool, cwl-utils).

Runs in a fresh interpreter with ``sys.modules['cwltool'] = sys.modules['cwl_utils'] = None`` (any
``import cwltool`` then raises ImportError, as on an install without the extra). The workflow runtime
modules that mozart and hysds import at load time must not need the extra; only ``hysds_commons.cwl``
(the CWL compiler, used at registration) may raise, with the install hint.
"""
import json
import os
import subprocess
import sys
import textwrap

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

RUNTIME_MODULES = [
    "hysds_commons.workflow_refs",
    "hysds_commons.workflow_refs2",
    "hysds_commons.workflow_run2",
    "hysds_commons.workflow_spec",
    "hysds_commons.workflow_spec2",
    "hysds_commons.workflow_job2",
    "hysds_commons.workflow_outputs",
    "hysds_commons.workflow_submit",
]


def test_runtime_modules_import_without_cwl_extra():
    script = textwrap.dedent("""
        import importlib, json, sys
        sys.modules["cwltool"] = None
        sys.modules["cwl_utils"] = None
        out = {"ok": [], "failed": {}}
        for name in %r:
            try:
                importlib.import_module(name)
                out["ok"].append(name)
            except Exception as e:
                out["failed"][name] = "%%s: %%s" %% (type(e).__name__, e)
        try:
            import hysds_commons.cwl
            out["cwl"] = "imported"
        except ImportError as e:
            out["cwl"] = str(e)
        out["loaded_cwl_modules"] = sorted(m for m in sys.modules
                                           if m.startswith("hysds_commons.cwl.") and sys.modules[m] is not None)
        from hysds_commons.workflow_refs2 import flatten_files
        out["flatten"] = flatten_files([{"class": "File", "location": "s3://b/k"}, {"class": "Directory", "path": "/d"}, 3])
        print("RESULT " + json.dumps(out))
    """ % RUNTIME_MODULES)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([REPO, os.environ.get("PYTHONPATH", "")]))
    p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    out = json.loads([x for x in p.stdout.splitlines() if x.startswith("RESULT ")][-1][len("RESULT "):])
    assert out["failed"] == {}, out
    assert out["ok"] == RUNTIME_MODULES
    assert "hysds_commons[cwl]" in out["cwl"], out
    assert out["flatten"] == ["s3://b/k", "/d", 3]


def test_cwl_profile_uses_the_shared_flatten_helper():
    """The compiler and the runtime flatten File/Directory defaults the same way (one implementation)."""
    import pytest

    pytest.importorskip("cwltool")
    from hysds_commons.cwl import profile
    from hysds_commons.workflow_refs2 import flatten_files

    assert profile._default_value is flatten_files

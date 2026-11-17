"""CWL v1.2 -> HySDS workflow spec 2.0 compiler (PLAN-workflow-cwl-profile.md, B10 and section 8).

Requires the ``cwl`` extra (``pip install hysds_commons[cwl]``: cwltool + cwl-utils). Importing this
package without it raises ``ImportError`` with a clear message; nothing else in hysds_commons imports
cwltool, so only callers that handle CWL workflows (mozart registration) need the extra.

Public API (what mozart should call):

* ``compile_cwl(source, job_type_lookup=None, run_resolver=None)`` -> ``CompileResult``
  (``ok``, ``errors``, ``warnings``, ``spec``, ``graph``, ``cwl``); never raises for bad input.
* ``compile_cwl_or_raise(...)`` -> spec dict, raises ``CWLCompileError`` (``.errors``) otherwise.
* ``generate_operation(job_type, hysds_io, job_spec, outputs=None)`` / ``operation_snippet(...)``
  for ``GET /workflow/cwl/operation/<job_type>``.
"""

EXTRA_MESSAGE = (
    "CWL workflows require the 'cwl' extra of hysds_commons (cwltool and cwl-utils). "
    "Install it with: pip install 'hysds_commons[cwl]'"
)


def _require_extra():
    try:
        import cwltool  # noqa: F401
        import cwl_utils  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised through a simulated ImportError in tests
        raise ImportError("%s (missing: %s)" % (EXTRA_MESSAGE, e.name or e)) from e


_require_extra()

from hysds_commons.cwl.compile import (  # noqa: E402
    CompileResult,
    CWLCompileError,
    SOURCE_FORMAT,
    _validate_compiled,
    compile_cwl,
    compile_cwl_or_raise,
)
from hysds_commons.cwl.loader import HYSDS_NS, load_document, validate_with_cwltool  # noqa: E402
from hysds_commons.cwl.operation import generate_operation, operation_snippet  # noqa: E402

__all__ = [
    "CompileResult", "CWLCompileError", "SOURCE_FORMAT", "HYSDS_NS", "compile_cwl", "compile_cwl_or_raise",
    "load_document", "validate_with_cwltool", "generate_operation", "operation_snippet", "EXTRA_MESSAGE",
]

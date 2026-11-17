"""Typed output lists (ADR-003) and the ref error type shared by ``workflow_refs2`` and the engine.

The v1 ``$.``-reference resolver was removed; schema 2.0 refs live in ``workflow_refs2``.
Pure module: no OpenSearch, no celery, no I/O.
"""

import copy

_VALUE_SLOTS = ("value_s", "value_d", "value_b")


class WorkflowRefError(Exception):
    """Raised for a malformed ref, a malformed outputs list, or an unresolvable ref."""


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

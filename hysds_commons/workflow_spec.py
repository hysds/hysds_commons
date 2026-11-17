"""Shared workflow helpers: the spec error type and the retry rule (contract 12.2).

The v1 node-graph spec (schema 1.0) and its validator were removed; schema 2.0 lives in
``workflow_spec2`` and the CWL compiler. Pure module: no OpenSearch, no celery, no I/O.
"""


class WorkflowSpecError(Exception):
    """Raised by the schema 2.0 validator; ``.errors`` holds every validation error."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("invalid workflow spec: " + "; ".join(self.errors))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


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


# Deterministic failures: re-running the job gives the same result, so no retry_on pattern (not even ``*``) retries
# them. ``outputs``: invalid / oversized / unresolvable workflow outputs (job_worker or the engine).
NEVER_RETRIED_CLASSES = frozenset({"outputs"})


def is_retryable(policy, error_class, retry_count):
    """Contract 12.2: retryable iff ``retry_count < max_retries`` and the class matches ``retry_on``; a class in
    :data:`NEVER_RETRIED_CLASSES` is never retryable."""
    if error_class in NEVER_RETRIED_CLASSES:
        return False
    count = retry_count if _is_int(retry_count) else 0
    return count < policy.get("max_retries", 0) and error_class_matches(policy.get("retry_on"), error_class)

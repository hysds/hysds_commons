"""The helpers v2 still takes from the trimmed v1 modules: the retry rule and typed output lists."""

import pytest

from hysds_commons.workflow_refs import WorkflowRefError, outputs_to_dict
from hysds_commons.workflow_spec import error_class_matches, is_retryable


@pytest.mark.parametrize("pats,ec,expected", [
    (["*"], "worker", True),
    (["*"], None, True),
    (["exit_code:*"], "exit_code:1", True),
    (["exit_code:*"], "exit_code:-9", True),
    (["exit_code:*"], "timeout", False),
    (["exit_code:*"], "pge:x", False),
    (["exit_code:1"], "exit_code:1", True),
    (["exit_code:1"], "exit_code:10", False),
    (["timeout", "offline"], "offline", True),
    (["timeout"], "signal", False),
    (["pge:transient"], "pge:transient", True),
    (["pge:transient"], "pge:other", False),
    (["pge:*"], "pge:anything", True),
    (["pge:*"], "exit_code:1", False),
    (["exit_code:*"], None, False),
    (["exit_code:*"], "", False),
    ([], "worker", False),
    (None, "worker", False),
])
def test_error_class_matches(pats, ec, expected):
    assert error_class_matches(pats, ec) is expected


@pytest.mark.parametrize("count,mx,ec,pats,expected", [
    (0, 2, "exit_code:1", ["exit_code:*"], True),
    (1, 2, "exit_code:1", ["exit_code:*"], True),
    (2, 2, "exit_code:1", ["exit_code:*"], False),
    (3, 2, "exit_code:1", ["exit_code:*"], False),
    (0, 0, "exit_code:1", ["*"], False),
    (0, 2, "timeout", ["exit_code:*"], False),
    (None, 1, "worker", ["*"], True),
    (True, 1, "worker", ["*"], True),  # bool is not a count: treated as 0
])
def test_is_retryable(count, mx, ec, pats, expected):
    assert is_retryable({"max_retries": mx, "retry_on": pats}, ec, count) is expected


def test_is_retryable_empty_policy():
    assert is_retryable({}, "worker", 0) is False


def test_outputs_class_never_retried():
    # invalid / oversized workflow outputs are deterministic: not even "*" retries them
    assert is_retryable({"max_retries": 3, "retry_on": ["*"]}, "outputs", 0) is False
    assert is_retryable({"max_retries": 3, "retry_on": ["*"]}, "worker", 0) is True


def test_outputs_to_dict_slots():
    kv = [
        {"name": "s", "value_s": "x"},
        {"name": "d", "value_d": 0.5},
        {"name": "b", "value_b": False},
        {"name": "ls", "value_s": ["a", "b"]},
        {"name": "ld", "value_d": [1, 2.5]},
        {"name": "lb", "value_b": [True, False]},
    ]
    assert outputs_to_dict(kv) == {"s": "x", "d": 0.5, "b": False, "ls": ["a", "b"], "ld": [1, 2.5], "lb": [True, False]}
    assert outputs_to_dict(None) == {} and outputs_to_dict([]) == {}


@pytest.mark.parametrize("bad", [
    {"a": 1}, [1], [{"value_s": "x"}], [{"name": "a"}], [{"name": "a", "value_s": "x", "value_d": 1}],
])
def test_outputs_to_dict_bad(bad):
    with pytest.raises(WorkflowRefError):
        outputs_to_dict(bad)

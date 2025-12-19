"""
Unit tests for SearchUtility closed index handling (HC-600).

These tests verify that the SearchUtility class correctly handles
closed indices in wildcard queries by automatically applying the
appropriate parameters (ignore_unavailable, allow_no_indices, expand_wildcards).
"""

import pytest
from unittest.mock import MagicMock, patch

from hysds_commons.search_utils import SearchUtility


class ConcreteSearchUtility(SearchUtility):
    """Concrete implementation of SearchUtility for testing."""

    def __init__(self):
        super().__init__(host="http://localhost:9200")
        self.es = MagicMock()
        self.engine = "elasticsearch"
        self.version = None
        self.flavor = "default"


class TestIsWildcardIndex:
    """Tests for _is_wildcard_index() static method."""

    def test_wildcard_asterisk(self):
        """Index pattern with asterisk should be detected as wildcard."""
        assert SearchUtility._is_wildcard_index("job_status-*") is True
        assert SearchUtility._is_wildcard_index("*") is True
        assert SearchUtility._is_wildcard_index("grq_*_product") is True

    def test_multiple_indices_comma(self):
        """Index pattern with comma should be detected as wildcard."""
        assert SearchUtility._is_wildcard_index("index1,index2") is True
        assert SearchUtility._is_wildcard_index("job_status-2024.01,job_status-2024.02") is True

    def test_combined_wildcard_and_comma(self):
        """Index pattern with both asterisk and comma should be detected."""
        assert SearchUtility._is_wildcard_index("job_status-*,task_status-*") is True

    def test_single_index_no_wildcard(self):
        """Single index without wildcards should not be detected."""
        assert SearchUtility._is_wildcard_index("job_status-current") is False
        assert SearchUtility._is_wildcard_index("grq_v1.0_product") is False

    def test_none_index(self):
        """None index should return False."""
        assert SearchUtility._is_wildcard_index(None) is False

    def test_empty_string(self):
        """Empty string should return False."""
        assert SearchUtility._is_wildcard_index("") is False

    def test_list_with_wildcard(self):
        """List containing wildcard patterns should be detected."""
        assert SearchUtility._is_wildcard_index(["logs-*", "metrics-*"]) is True
        assert SearchUtility._is_wildcard_index(["job_status-*"]) is True
        assert SearchUtility._is_wildcard_index(["grq_*_product", "task_status"]) is True

    def test_list_with_comma_pattern(self):
        """List containing comma patterns should be detected."""
        assert SearchUtility._is_wildcard_index(["index1,index2"]) is True

    def test_list_without_wildcard(self):
        """List without any wildcards should return False."""
        assert SearchUtility._is_wildcard_index(["job_status-current"]) is False
        assert SearchUtility._is_wildcard_index(["index1", "index2"]) is False
        assert SearchUtility._is_wildcard_index(["grq_v1.0_product", "grq_v1.0_task"]) is False

    def test_empty_list(self):
        """Empty list should return False."""
        assert SearchUtility._is_wildcard_index([]) is False

    def test_list_with_non_string_elements(self):
        """List with non-string elements should be handled gracefully."""
        assert SearchUtility._is_wildcard_index([None, "job_status-*"]) is True
        assert SearchUtility._is_wildcard_index([123, "job_status"]) is False


class TestApplyClosedIndexParams:
    """Tests for _apply_closed_index_params() method."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()

    def test_applies_params_for_wildcard_index(self):
        """Should apply all three params for wildcard index patterns."""
        kwargs = {"index": "job_status-*", "body": {"query": {"match_all": {}}}}
        self.utility._apply_closed_index_params(kwargs)

        assert kwargs["ignore_unavailable"] is True
        assert kwargs["allow_no_indices"] is True
        assert kwargs["expand_wildcards"] == "open"

    def test_applies_params_for_comma_separated_indices(self):
        """Should apply params for comma-separated indices."""
        kwargs = {"index": "index1,index2", "body": {}}
        self.utility._apply_closed_index_params(kwargs)

        assert kwargs["ignore_unavailable"] is True
        assert kwargs["allow_no_indices"] is True
        assert kwargs["expand_wildcards"] == "open"

    def test_does_not_apply_for_single_index(self):
        """Should not apply params for single index without wildcards."""
        kwargs = {"index": "job_status-current", "body": {}}
        self.utility._apply_closed_index_params(kwargs)

        assert "ignore_unavailable" not in kwargs
        assert "allow_no_indices" not in kwargs
        assert "expand_wildcards" not in kwargs

    def test_does_not_override_existing_params(self):
        """Should not override caller-specified params."""
        kwargs = {
            "index": "job_status-*",
            "body": {},
            "ignore_unavailable": False,
            "allow_no_indices": False,
            "expand_wildcards": "all"
        }
        self.utility._apply_closed_index_params(kwargs)

        # Should preserve caller's values
        assert kwargs["ignore_unavailable"] is False
        assert kwargs["allow_no_indices"] is False
        assert kwargs["expand_wildcards"] == "all"

    def test_partial_override(self):
        """Should only set params that aren't already specified."""
        kwargs = {
            "index": "job_status-*",
            "body": {},
            "ignore_unavailable": False  # Only this one specified
        }
        self.utility._apply_closed_index_params(kwargs)

        # Caller's value preserved
        assert kwargs["ignore_unavailable"] is False
        # Defaults applied for unspecified
        assert kwargs["allow_no_indices"] is True
        assert kwargs["expand_wildcards"] == "open"

    def test_handles_none_index(self):
        """Should handle None index gracefully."""
        kwargs = {"body": {}}
        self.utility._apply_closed_index_params(kwargs)

        assert "ignore_unavailable" not in kwargs
        assert "allow_no_indices" not in kwargs
        assert "expand_wildcards" not in kwargs

    def test_returns_kwargs(self):
        """Should return the modified kwargs dict."""
        kwargs = {"index": "job_status-*", "body": {}}
        result = self.utility._apply_closed_index_params(kwargs)

        assert result is kwargs


class TestSearchMethod:
    """Tests for search() method with closed index handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()
        self.utility.es.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

    def test_search_with_wildcard_applies_params(self):
        """search() should apply closed index params for wildcard patterns."""
        self.utility.search(index="job_status-*", body={"query": {"match_all": {}}})

        call_kwargs = self.utility.es.search.call_args[1]
        assert call_kwargs["ignore_unavailable"] is True
        assert call_kwargs["allow_no_indices"] is True
        assert call_kwargs["expand_wildcards"] == "open"

    def test_search_without_wildcard_no_params(self):
        """search() should not apply params for non-wildcard index."""
        self.utility.search(index="job_status-current", body={"query": {"match_all": {}}})

        call_kwargs = self.utility.es.search.call_args[1]
        assert "ignore_unavailable" not in call_kwargs
        assert "allow_no_indices" not in call_kwargs
        assert "expand_wildcards" not in call_kwargs


class TestQueryMethod:
    """Tests for query() method with closed index handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()
        # Return 0 total to prevent pagination loop, but include some hits for first call
        self.utility.es.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

    def test_query_with_wildcard_applies_params(self):
        """query() should apply closed index params for wildcard patterns."""
        self.utility.query(index="job_status-*", body={"query": {"match_all": {}}})

        # Get the first call's kwargs (the initial search call)
        call_kwargs = self.utility.es.search.call_args_list[0][1]
        assert call_kwargs["ignore_unavailable"] is True
        assert call_kwargs["allow_no_indices"] is True
        assert call_kwargs["expand_wildcards"] == "open"

    def test_query_without_wildcard_no_params(self):
        """query() should not apply params for non-wildcard index."""
        self.utility.query(index="job_status-current", body={"query": {"match_all": {}}})

        # Get the first call's kwargs (the initial search call)
        call_kwargs = self.utility.es.search.call_args_list[0][1]
        assert "ignore_unavailable" not in call_kwargs
        assert "allow_no_indices" not in call_kwargs
        assert "expand_wildcards" not in call_kwargs


class TestGetCountMethod:
    """Tests for get_count() method with closed index handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()
        self.utility.es.count.return_value = {"count": 42}

    def test_get_count_with_wildcard_applies_params(self):
        """get_count() should apply closed index params for wildcard patterns."""
        result = self.utility.get_count(index="job_status-*", body={"query": {"match_all": {}}})

        call_kwargs = self.utility.es.count.call_args[1]
        assert call_kwargs["ignore_unavailable"] is True
        assert call_kwargs["allow_no_indices"] is True
        assert call_kwargs["expand_wildcards"] == "open"
        assert result == 42

    def test_get_count_without_wildcard_no_params(self):
        """get_count() should not apply params for non-wildcard index."""
        self.utility.get_count(index="job_status-current", body={"query": {"match_all": {}}})

        call_kwargs = self.utility.es.count.call_args[1]
        assert "ignore_unavailable" not in call_kwargs
        assert "allow_no_indices" not in call_kwargs
        assert "expand_wildcards" not in call_kwargs


class TestSearchByIdMethod:
    """Tests for search_by_id() method with closed index handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()
        self.utility.es.search.return_value = {
            "hits": {"total": {"value": 1}, "hits": [{"_id": "doc1", "_source": {}}]}
        }

    def test_search_by_id_with_wildcard_applies_params(self):
        """search_by_id() should apply closed index params for wildcard patterns."""
        self.utility.search_by_id(index="job_status-*", id="doc1")

        call_kwargs = self.utility.es.search.call_args[1]
        assert call_kwargs["ignore_unavailable"] is True
        assert call_kwargs["allow_no_indices"] is True
        assert call_kwargs["expand_wildcards"] == "open"

    def test_search_by_id_without_wildcard_no_params(self):
        """search_by_id() should not apply params for non-wildcard index."""
        self.utility.search_by_id(index="job_status-current", id="doc1")

        call_kwargs = self.utility.es.search.call_args[1]
        assert "ignore_unavailable" not in call_kwargs
        assert "allow_no_indices" not in call_kwargs
        assert "expand_wildcards" not in call_kwargs


class TestPitMethod:
    """Tests for _pit() method with closed index handling."""

    def setup_method(self):
        """Set up test fixtures."""
        self.utility = ConcreteSearchUtility()
        self.utility.es.open_point_in_time.return_value = {"id": "pit_id_123"}
        self.utility.es.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }
        self.utility.es.close_point_in_time.return_value = {"succeeded": True}

    def test_pit_with_wildcard_applies_params_to_open_point_in_time(self):
        """_pit() should apply closed index params to open_point_in_time for wildcard patterns."""
        self.utility._pit(index="job_status-*", body={"query": {"match_all": {}}})

        # Verify open_point_in_time was called with closed index params
        call_kwargs = self.utility.es.open_point_in_time.call_args[1]
        assert call_kwargs["ignore_unavailable"] is True
        assert call_kwargs["allow_no_indices"] is True
        assert call_kwargs["expand_wildcards"] == "open"

    def test_pit_without_wildcard_no_params_to_open_point_in_time(self):
        """_pit() should not apply params to open_point_in_time for non-wildcard index."""
        self.utility._pit(index="job_status-current", body={"query": {"match_all": {}}})

        # Verify open_point_in_time was called without closed index params
        call_kwargs = self.utility.es.open_point_in_time.call_args[1]
        assert "ignore_unavailable" not in call_kwargs
        assert "allow_no_indices" not in call_kwargs
        assert "expand_wildcards" not in call_kwargs


class TestClosedIndexParamsConstant:
    """Tests for CLOSED_INDEX_PARAMS class constant."""

    def test_contains_required_keys(self):
        """CLOSED_INDEX_PARAMS should contain all required keys."""
        assert "ignore_unavailable" in SearchUtility.CLOSED_INDEX_PARAMS
        assert "allow_no_indices" in SearchUtility.CLOSED_INDEX_PARAMS
        assert "expand_wildcards" in SearchUtility.CLOSED_INDEX_PARAMS

    def test_correct_values(self):
        """CLOSED_INDEX_PARAMS should have correct default values."""
        assert SearchUtility.CLOSED_INDEX_PARAMS["ignore_unavailable"] is True
        assert SearchUtility.CLOSED_INDEX_PARAMS["allow_no_indices"] is True
        assert SearchUtility.CLOSED_INDEX_PARAMS["expand_wildcards"] == "open"

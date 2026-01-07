from future import standard_library
standard_library.install_aliases()

from abc import ABC
from packaging import version
from pathlib import Path
import os
import netrc
import warnings
import backoff

warnings.simplefilter('always', UserWarning)


class SearchUtility(ABC):
    # Default parameters for handling closed indices in wildcard queries (HC-600)
    # These parameters allow queries to gracefully skip closed indices instead of failing
    CLOSED_INDEX_PARAMS = {
        "ignore_unavailable": True,   # Skip closed/missing indices without error
        "allow_no_indices": True,     # Return empty results if all matching indices are closed
        "expand_wildcards": "open"    # Only query open indices, skip closed ones
    }

    def __init__(self, host, **kwargs):
        self.es = None
        self.version = None
        self.engine = None
        self.flavor = None

    @staticmethod
    def _is_wildcard_index(index):
        """
        Check if an index pattern contains wildcards or multiple indices.

        Args:
            index: Index name or pattern (can be str, list, or None)

        Returns:
            bool: True if index contains wildcards (*) or multiple indices (,)
        """
        if index is None:
            return False
        # Handle list of indices - check each element for wildcards
        if isinstance(index, list):
            return any("*" in idx or "," in idx for idx in index if isinstance(idx, str))
        return "*" in index or "," in index

    def _apply_closed_index_params(self, kwargs):
        """
        Apply closed index handling parameters to all search queries.

        These parameters are always applied because:
        1. Aliases (like 'grq') can resolve to multiple indices, some of which may be closed
        2. The params are harmless for single open indices
        3. They only affect closed indices and don't change behavior for open ones

        Only sets params if not already specified by the caller.

        Args:
            kwargs: Dictionary of keyword arguments (modified in place)

        Returns:
            kwargs: The modified keyword arguments dictionary
        """
        for key, value in self.CLOSED_INDEX_PARAMS.items():
            kwargs.setdefault(key, value)
        return kwargs

    def set_version(self):
        """
        Sets the version of elasticsearch; ex. 7.10.2
        """
        es_info = self.es.info()
        version_info = es_info["version"]
        version_number = version_info["number"]
        self.version = version.parse(version_number)
        self.flavor = version_info.get("build_flavor", "default")

    def index_document(self, **kwargs):
        """
        indexing (adding) document to Elasticsearch
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.index
            index – (required) The name of the index
            body – The document
            id – (optional) Document ID, will use ES generated id if not specified
            refresh – If true then refresh the affected shards to make this operation visible to search
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        return self.es.index(**kwargs)

    def get_by_id(self, **kwargs):
        """
        retrieving document from Elasticsearch based on _id
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.get
            index (required) – A comma-separated list of index names
            allow_no_indices – Ignore if a wildcard expression resolves to no concrete indices (default: false)
            expand_wildcards – Whether wildcard expressions should get expanded to open or closed indices
                (default: open) Valid choices: open, closed, hidden, none, all Default: open
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        return self.es.get(**kwargs)

    def search_by_id(self, **kwargs):
        """
        similar to get_by_id, but this uses the _search API and can be used on aliases w/ multiple indices
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.search
        @param kwargs:
            - id: Str, ES document id
            - index: Str, ES index or alias
            - return_all: Bool, if there are more than one records returned
                List[Dict] if True, else returns Dict of the latest record
        @return: Dict or List[Dict]
        """
        _id = kwargs.pop("id", None)
        if not _id:
            raise RuntimeError("_id key argument must be supplied")
        index = kwargs.get("index", None)
        if not index:
            raise RuntimeError("index key argument must be supplied")

        ignore = kwargs.get("ignore", None)
        kwargs["sort"] = kwargs.get("sort", "@timestamp:desc")

        return_all = kwargs.pop("return_all", False)

        kwargs["body"] = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"_id": _id}}
                    ]
                }
            }
        }
        self._apply_closed_index_params(kwargs)
        docs = self.es.search(**kwargs)

        hits = docs.get("hits", {}).get("hits", [])
        if len(hits) == 0:
            if (type(ignore) is list and 404 in ignore) or (type(ignore) is int and ignore == 404):
                not_found_doc = {
                    "_index": index,
                    "_type": "_doc",
                    "_id": _id,
                    "found": False
                }
                return [not_found_doc] if return_all is True else not_found_doc
            err = f"{_id} not found on index/alias {index}"
            raise ValueError(err)

        for hit in hits:
            hit["found"] = True  # adding "found" to match get_by_id
        return hits if return_all is True else hits[0]

    def _pit(self, **kwargs):
        """
        using the PIT (point-in-time) + search_after API to do deep pagination
        https://www.elastic.co/guide/en/elasticsearch/reference/7.10/point-in-time-api.html
        https://www.elastic.co/guide/en/elasticsearch/reference/7.10/paginate-search-results.html#search-after
        :param kwargs: please see the docstrings for the "search" method below
            * index is required when using the search_after API
        :return: List[any]
        """
        if not self.flavor:
            self.set_version()

        keep_alive = "2m"
        body = kwargs.pop("body", {})
        index = kwargs.pop("index", body.pop("index", None))
        if index is None:
            raise RuntimeError("ElasticsearchUtility._pit: the search_after API must specify a index/alias")

        # Apply closed index params (HC-600) - always apply since aliases can
        # resolve to multiple indices, some of which may be closed
        pit_params = {}
        for key, value in self.CLOSED_INDEX_PARAMS.items():
            kwargs.setdefault(key, value)
            pit_params[key] = kwargs[key]

        size = kwargs.get("size", body.get("size"))
        if not size:
            kwargs["size"] = 1000

        sort = kwargs.get("sort", body.get("sort", []))
        if not sort:
            body["sort"] = [{"@timestamp": "desc"}, {"id.keyword": "asc"}]

        pit = None
        if self.flavor != "oss":
            pit = self.es.open_point_in_time(index=index, keep_alive=keep_alive, **pit_params)
            body = {
                **body,
                **{"pit": {**pit, **{"keep_alive": keep_alive}}},
            }
        else:
            warnings.warn("Elasticsearch OSS does not support _pit, will use search_after without _pit...")
        res = self.es.search(body=body, **kwargs)

        records = []
        while True:
            if len(res["hits"]["hits"]) == 0:
                break
            records.extend(res["hits"]["hits"])
            last_record = res["hits"]["hits"][-1]
            body["search_after"] = last_record["sort"]
            res = self.es.search(body=body, **kwargs)

        if pit:
            self.es.close_point_in_time(body=pit)
        return records

    def _scroll(self, **kwargs):
        if "size" not in kwargs:
            kwargs["size"] = 1000
        if "scroll" not in kwargs:
            kwargs["scroll"] = "2m"
        scroll = kwargs["scroll"]  # re-use in each subsequent scroll

        page = self.es.search(**kwargs)
        sid = page["_scroll_id"]
        scroll_id = sid
        documents = page["hits"]["hits"]

        page_size = page["hits"]["total"]["value"]
        if page_size <= len(documents):  # avoid scrolling if we get all data in initial query
            self.es.clear_scroll(scroll_id=scroll_id, ignore=[404])
            return documents

        while page_size > 0:
            page = self.es.scroll(scroll_id=sid, scroll=scroll)
            scroll_documents = page["hits"]["hits"]
            sid = page["_scroll_id"]
            if sid != scroll_id:
                self.es.clear_scroll(scroll_id=scroll_id, ignore=[404])
                scroll_id = sid

            page_size = len(scroll_documents)
            documents.extend(scroll_documents)

        self.es.clear_scroll(scroll_id=scroll_id, ignore=[404])  # clear the last scroll id (if possible)
        return documents

    def query(self, **kwargs):
        """
        returns all records returned from a query, through the scroll API

        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.search
            body – The search definition using the Query DSL
            index – (required) A comma-separated list of index names to search (or aliases)
            _source – True or false to return the _source field or not, or a list of fields to return
            _source_excludes – A list of fields to exclude from the returned _source field
            _source_includes – A list of fields to extract and return from the _source field
            q – Query in the Lucene query string syntax
            scroll – Specify how long a consistent view of the index should be maintained for scrolled search
            size – Number of hits to return (default: 10)
            sort – A comma-separated list of <field>:<direction> pairs
            ignore_unavailable – (auto-set for wildcards) Skip unavailable indices
            allow_no_indices – (auto-set for wildcards) Allow empty results if no indices match
            expand_wildcards – (auto-set for wildcards) Control wildcard expansion (default: "open")

        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.clear_scroll
            body – A comma-separated list of scroll IDs to clear if none was specified via the scroll_id parameter
            scroll_id – A comma-separated list of scroll IDs to clear
        """
        self._apply_closed_index_params(kwargs)
        page_limit = 10000
        if "size" not in kwargs and "size" not in kwargs.get("body", {}):
            kwargs["size"] = 1000
        else:
            kwargs["size"] = kwargs.get("size") or kwargs.get("body", {}).get("size", 1000)
        scroll = kwargs.pop("scroll", "2m")
        data = self.es.search(**kwargs)
        total = data["hits"]["total"]["value"]

        if total >= page_limit:
            if self.engine == "elasticsearch" and self.version is None:
                self.set_version()
            if self.engine == "opensearch" or (self.version >= version.parse("7.10") and self.flavor != "oss"):
                return self._pit(**kwargs)
            else:
                kwargs["scroll"] = scroll
                return self._scroll(**kwargs)
        else:
            page_size = kwargs["size"]
            documents = data["hits"]["hits"]
            kwargs["from_"] = 0
            while page_size > 0:
                kwargs["from_"] += page_size  # shift offset afterwards
                if kwargs["from_"] + kwargs["size"] >= page_limit:
                    kwargs["size"] = page_limit - kwargs["from_"]

                data = self.es.search(**kwargs)
                rows = data["hits"]["hits"]
                page_size = len(rows)
                documents.extend(rows)
            return documents

    def search(self, **kwargs):
        """
        similar to query method but does not scroll, used if user doesnt want to scroll
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.search
            index – (required) A comma-separated list of index names to search (or aliases)
            body – The search definition using the Query DSL
            _source – True or false to return the _source field or not, or a list of fields to return
            q – Query in the Lucene query string syntax
            scroll – Specify how long a consistent view of the index should be maintained for scrolled search
            size – Number of hits to return (default: 10)
            sort – A comma-separated list of <field>:<direction> pairs
            ignore_unavailable – (auto-set for wildcards) Skip unavailable indices
            allow_no_indices – (auto-set for wildcards) Allow empty results if no indices match
            expand_wildcards – (auto-set for wildcards) Control wildcard expansion (default: "open")
        """
        self._apply_closed_index_params(kwargs)
        return self.es.search(**kwargs)

    def get_count(self, **kwargs):
        """
        returning the count for a given query (warning: ES7 returns max of 10000)
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.count
            body – A query to restrict the results specified with the Query DSL (optional)
            index – (required) A comma-separated list of indices to restrict the results
            q – Query in the Lucene query string syntax
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
            ignore_unavailable – (auto-set for wildcards) Skip unavailable indices
            allow_no_indices – (auto-set for wildcards) Allow empty results if no indices match
            expand_wildcards – (auto-set for wildcards) Control wildcard expansion (default: "open")
        """
        self._apply_closed_index_params(kwargs)
        result = self.es.count(**kwargs)
        return result["count"]

    def delete_by_id(self, **kwargs):
        """
        Removes a document from the index
        https://www.elastic.co/guide/en/elasticsearch/reference/master/docs-delete.html
            index – (required) The name of the index
            id – The document ID
            refresh – If true then refresh the affected shards to make this operation visible to search
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        return self.es.delete(**kwargs)

    def update_document(self, **kwargs):
        """
        updates Elasticsearch document using the update API
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.update
            index – (required) The name of the index
            id – Document ID
            body – The request definition requires either script or partial doc:
                ex. {
                    "doc_as_upsert": true,
                    "doc": <ES document>
                }
            _source – True or false to return the _source field or not, or a list of fields to return
            refresh – If true then refresh the affected shards to make this operation visible to search
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        return self.es.update(**kwargs)

    def file_is_0600(self, filename):
        """Check that a file has 0600 permissions (read/write for user only)."""
        return oct(Path(filename).stat().st_mode)[-4:] == "0600"

    def get_creds(self, creds_entry, path="~/.netrc-os"):
        """Extract a username/password tuple from the entry creds_entry in netrc file provided in path.
        Updates credential file permissions to 600 if needed to keep them secure."""
        netrc_file = Path(path).expanduser()
        n = netrc.netrc(netrc_file)
        has_correct_permission = self.file_is_0600(netrc_file)

        if not has_correct_permission:
            self.logger.error("Your %s file does not have the correct permissions: 0600 (r/w for user only. "
                         "Attempting to chmod", path)
            os.chmod(netrc_file, 0o600)

        credentials = n.authenticators(creds_entry)
        if credentials is not None:
            return credentials[0], credentials[2]
        else:
            raise KeyError("Unable to extract OpenSearch credentials from %s for %s", path, creds_entry)

class JitteredBackoffException(Exception):
    pass


class JitteredBackoffHandler:
    def __init__(self, max_value, max_time, logger):
        self.max_value = max_value
        self.max_time = max_time
        self.logger = logger

    def log_backoff(self, details):
        if self.logger is not None:
            self.logger.error(
                "Backing off {wait:0.1f} seconds after {tries} tries " "calling function {target} with args {args} and kwargs " "{kwargs}".format(**details)
            )

    def log_giveup(self, details):
        if self.logger is not None:
            self.logger.error(f"Giving up after {details['tries']} tries due to {details['value']}")

    def backoff_wrapper(self, func, *args, **kwargs):
        @backoff.on_exception(
            backoff.expo,
            Exception,
            max_value=self.max_value,
            max_time=self.max_time,
            jitter=backoff.full_jitter,
            on_backoff=self.log_backoff,
            on_giveup=self.log_giveup,
        )
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if self.logger is not None:
                    self.logger.error(f"Exception occurred: {str(e)}")
                raise JitteredBackoffException(f"Exception occurred: {str(e)}") from e

        return wrapper


def jittered_backoff_class_factory(base_conn_class):
    class JitteredBackoffConnection(base_conn_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.max_value = kwargs.get("max_value", 13)
            self.max_time = kwargs.get("max_time", 34)
            self.logger = kwargs.get("logger", None)
            self.backoff_handler = JitteredBackoffHandler(self.max_value, self.max_time, self.logger)
            self.backoff_wrapped_func = self.backoff_handler.backoff_wrapper(super().perform_request)

        def perform_request(
            self,
            method,
            url,
            params=None,
            body=None,
            timeout=None,
            ignore=(),
            headers=None,
        ):
            return self.backoff_wrapped_func(
                method,
                url,
                params=params,
                body=body,
                timeout=timeout,
                ignore=ignore,
                headers=headers,
            )

    return JitteredBackoffConnection

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

from packaging import version
import elasticsearch
from elasticsearch.exceptions import NotFoundError, RequestError, ElasticsearchException


class ElasticsearchUtility:
    def __init__(self, es_url, logger=None, **kwargs):
        self.es = elasticsearch.Elasticsearch(hosts=[es_url], **kwargs)
        self.es_url = es_url
        self.logger = logger

        es_info = self.es.info()
        self.version = version.parse(es_info["version"]["number"])

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
        try:
            result = self.es.index(**kwargs)
            return result
        except RequestError as e:
            self.logger.exception(e.info) if self.logger else print(e.info)
            raise e
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e

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
        try:
            data = self.es.get(**kwargs)
            return data
        except NotFoundError as e:
            self.logger.error(e) if self.logger else print(e)
            raise e
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e

    def _pit(self, **kwargs):
        """
        using the PIT (point-in-time) + search_after API to do deep pagination
        https://www.elastic.co/guide/en/elasticsearch/reference/7.10/point-in-time-api.html
        https://www.elastic.co/guide/en/elasticsearch/reference/7.10/paginate-search-results.html#search-after
        :param kwargs: please see the docstrings for the "search" method below
            * index is required when using the search_after API
        :return: List[any]
        """
        keep_alive = "2m"
        body = kwargs.pop("body", {})
        index = kwargs.pop("index", body.pop("index", None))
        if index is None:
            raise RuntimeError("ElasticsearchUtility._pit: the search_after API must specify a index/alias")

        pit = self.es.open_point_in_time(index=index, keep_alive=keep_alive)

        size = kwargs.get("size", body.get("size", 1000))
        if not size:
            kwargs["size"] = size

        sort = kwargs.get("sort", body.get("sort", []))
        if not sort:
            body["sort"] = [{"@timestamp": "desc"}, {"id.keyword": "asc"}]

        body = {
            **body,
            **{"pit": {**pit, **{"keep_alive": keep_alive}}},
        }
        res = self.es.search(body=body, **kwargs)

        records = []
        while True:
            if len(res["hits"]["hits"]) == 0:
                break
            records.extend(res["hits"]["hits"])
            last_record = res["hits"]["hits"][-1]
            body["search_after"] = last_record["sort"]
            res = self.es.search(body=body, **kwargs)

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

        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.clear_scroll
            body – A comma-separated list of scroll IDs to clear if none was specified via the scroll_id parameter
            scroll_id – A comma-separated list of scroll IDs to clear
        """
        page_limit = 10000
        if "size" not in kwargs and "size" not in kwargs.get("body", {}):
            kwargs["size"] = 1000
        else:
            kwargs["size"] = kwargs.get("size") or kwargs.get("body", {}).get("size", 1000)
        scroll = kwargs.pop("scroll", "2m")
        data = self.es.search(**kwargs)
        total = data["hits"]["total"]["value"]

        if total >= page_limit:
            if self.version >= version.parse("7.10"):
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
        """
        try:
            if self.logger:
                self.logger.info("search **kwargs: {}".format(dict(**kwargs)))
            result = self.es.search(**kwargs)
            return result
        except RequestError as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e

    def get_count(self, **kwargs):
        """
        returning the count for a given query (warning: ES7 returns max of 10000)
        https://elasticsearch-py.readthedocs.io/en/master/api.html#elasticsearch.Elasticsearch.count
            body – A query to restrict the results specified with the Query DSL (optional)
            index – (required) A comma-separated list of indices to restrict the results
            q – Query in the Lucene query string syntax
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        try:
            result = self.es.count(**kwargs)
            return result["count"]
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e

    def delete_by_id(self, **kwargs):
        """
        Removes a document from the index
        https://www.elastic.co/guide/en/elasticsearch/reference/master/docs-delete.html
            index – (required) The name of the index
            id – The document ID
            refresh – If true then refresh the affected shards to make this operation visible to search
            ignore - will not raise error if status code is specified (ex. 404, [400, 404])
        """
        try:
            if self.logger:
                self.logger.info("query **kwargs: {}".format(dict(**kwargs)))
            result = self.es.delete(**kwargs)
            return result
        except NotFoundError as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e

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
        try:
            if self.logger:
                self.logger.info("update_document **kwargs".format(dict(**kwargs)))
            result = self.es.update(**kwargs)
            return result
        except RequestError as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e
        except (ElasticsearchException, Exception) as e:
            self.logger.exception(e) if self.logger else print(e)
            raise e


# TODO: remove all code that uses this function
def get_es_scrolled_data(es_url, index, query):
    es = elasticsearch.Elasticsearch([es_url])

    documents = []
    page = es.search(index=index, scroll="2m", size=100, body=query)

    sid = page["_scroll_id"]
    documents.extend(page["hits"]["hits"])
    page_size = page["hits"]["total"]["value"]

    # Start scrolling
    while page_size > 0:
        page = es.scroll(scroll_id=sid, scroll="2m")

        # Update the scroll ID
        sid = page["_scroll_id"]
        scroll_document = page["hits"]["hits"]

        # Get the number of results that we returned in the last scroll
        page_size = len(scroll_document)
        documents.extend(scroll_document)
    return documents

from future import standard_library
from opensearchpy import OpenSearch
from opensearchpy import RequestsHttpConnection as RequestsHttpConnectionOS
from hysds_commons.search_utils import jittered_backoff_class_factory
from hysds_commons.log_utils import logger
from hysds_commons.search_utils import SearchUtility

standard_library.install_aliases()

class OpenSearchUtility(SearchUtility):
    def __init__(self, host, **kwargs):
        super().__init__(host)

        self.es = OpenSearch(hosts=host if type(host) == list else [host],
                             use_ssl=True,
                             verify_certs=False,
                             ssl_assert_hostname=False,
                             connection_class=jittered_backoff_class_factory(RequestsHttpConnectionOS),
                             connection_class_params={
                                 "max_value": 13,
                                 "max_time": 34,
                                 "logger": logger,
                             },
                             ssl_show_warn=False,
                             http_auth=self.get_creds(creds_entry="default"),
                             **kwargs)
        self.version = None
        self.engine = "opensearch"

    def _pit(self, **kwargs):
        """
        using the PIT (point-in-time) + search_after API to do deep pagination
          - https://opensearch.org/docs/latest/search-plugins/point-in-time/
          - https://opensearch.org/docs/latest/search-plugins/point-in-time/#pagination-with-pit-and-search_after
          - https://opensearch-project.github.io/opensearch-py/api-ref/clients/opensearch_client.html#opensearchpy.OpenSearch.create_point_in_time
          - https://opensearch-project.github.io/opensearch-py/api-ref/clients/opensearch_client.html#opensearchpy.OpenSearch.delete_point_in_time
        :param kwargs: please see the docstrings for the "search" method below
            * index is required when using the search_after API
        :return: List[any]
        """
        keep_alive = "2m"
        body = kwargs.pop("body", {})
        index = kwargs.pop("index", body.pop("index", None))
        if index is None:
            raise RuntimeError("OpenSearchUtility._pit: the search_after API must specify a index/alias")

        pit = self.es.create_point_in_time(index=index, keep_alive=keep_alive)
        pit_id = pit["pit_id"]

        size = kwargs.get("size", body.get("size"))
        if not size:
            kwargs["size"] = 1000

        sort = kwargs.get("sort", body.get("sort", []))
        if not sort:
            body["sort"] = [{"@timestamp": "desc"}, {"id.keyword": "asc"}]

        body = {
            **body,
            **{"pit": {"id": pit_id, "keep_alive": keep_alive}}
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

        self.es.delete_point_in_time(body={"pit_id": [pit_id]})
        return records

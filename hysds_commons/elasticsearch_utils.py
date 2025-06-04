from future import standard_library
standard_library.install_aliases()

from elasticsearch import Elasticsearch
from hysds_commons.search_utils import SearchUtility


class ElasticsearchUtility(SearchUtility):
    def __init__(self, host, **kwargs):
        super().__init__(host)
        self.es = Elasticsearch(hosts=host if type(host) == list else [host], **kwargs)
        self.version = None
        self.engine = "elasticsearch"

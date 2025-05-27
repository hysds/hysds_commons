from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
from elasticsearch import Elasticsearch
from elasticsearch import RequestsHttpConnection as RequestsHttpConnectionES
from hysds_commons.search_utils import jittered_backoff_class_factory
from hysds_commons.log_utils import logger
from hysds_commons.search_utils import SearchUtility


class ElasticsearchUtility(SearchUtility):
    def __init__(self, host, **kwargs):
        super().__init__(host)


        # No ssl=true parameter for Elasticsearch client. Need to ensure "https" in host url(s)
        self.es = Elasticsearch(hosts=host if type(host) == list else [host],
                                verify_certs=False,
                                ssl_show_warn=False,
                                connection_class=jittered_backoff_class_factory(RequestsHttpConnectionES),
                                connection_class_params={
                                    "max_value": 13,
                                    "max_time": 34,
                                    "logger": logger,
                                },
                                basic_auth=self.get_creds(creds_entry="default"),
                                **kwargs)
        self.version = None
        self.engine = "elasticsearch"

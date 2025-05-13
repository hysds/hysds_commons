from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

from elasticsearch import Elasticsearch
from hysds_commons.search_utils import SearchUtility


class ElasticsearchUtility(SearchUtility):
    def __init__(self, host, **kwargs):
        super().__init__(host)
        # No ssl=true parameter for Elasticsearch client. Need to ensure "https" in host url(s)
        self.es = Elasticsearch(hosts=host if type(host) == list else [host],
                                verify_certs=False,
                                ssl_show_warn=False,
                                basic_auth=self.get_creds(creds_entry="hysdsops"),
                                **kwargs)
        self.version = None
        self.engine = "elasticsearch"

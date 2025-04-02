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
        self.es = Elasticsearch(hosts=host if type(host) == list else [host],
                                use_ssl=True,
                                verify_certs=False,
                                ssl_assert_hostname=False,
                                ssl_show_warn=False,
                                **kwargs)
        self.version = None
        self.engine = "elasticsearch"

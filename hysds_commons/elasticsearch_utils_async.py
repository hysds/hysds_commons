from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

from elasticsearch import AsyncElasticsearch


class ElasticsearchUtilityAsync:
    def __init__(self, es_url, **kwargs):
        self.es_url = es_url
        self.es = AsyncElasticsearch(hosts=[es_url], **kwargs)

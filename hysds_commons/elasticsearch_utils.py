from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import json
from elasticsearch import Elasticsearch


def get_es_scrolled_data(es_url, index, query):
    es = Elasticsearch([es_url])

    documents = []
    page = es.search(index=index, scroll='2m', size=100, body=query)

    sid = page['_scroll_id']
    documents.extend(page['hits']['hits'])
    page_size = page['hits']['total']['value']

    # Start scrolling
    while page_size > 0:
        page = es.scroll(scroll_id=sid, scroll='2m')

        # Update the scroll ID
        sid = page['_scroll_id']

        scroll_document = page['hits']['hits']

        # Get the number of results that we returned in the last scroll
        page_size = len(scroll_document)

        documents.extend(scroll_document)

    return documents

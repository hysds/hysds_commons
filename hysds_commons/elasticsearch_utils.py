from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

from elasticsearch import Elasticsearch, NotFoundError, RequestsHttpConnection, RequestError, ElasticsearchException
# from requests_aws4auth import AWS4Auth


class ElasticsearchUtility:
    def __init__(self, es_url, logger=None, **kwargs):
        # TODO: ADD AWS AUTHENTICATION
        # TODO: https://docs.aws.amazon.com/elasticsearch-service/latest/developerguide/es-request-signing.html
        self.es = Elasticsearch(hosts=[es_url], **kwargs)
        self.es_url = es_url
        self.logger = logger

    def index_document(self, index, doc, refresh=True):
        try:
            result = self.es.index(index=index, body=doc, refresh=refresh)
            return result
        except RequestError as e:
            if self.logger:
                self.logger.error("status code 400: bad request to Elasticsearch, please check your document")
            raise RequestError(e)
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

    def get_by_id(self, index, _id, safe=False, include_source=False):
        try:
            data = self.es.get(index=index, id=_id)
            if self.logger:
                self.logger.info("retrieved _id %s from index %s" % (_id, index))
            if include_source:
                return data
            else:
                return data["_source"]
        except NotFoundError as e:
            if safe:
                if self.logger:
                    self.logger.warning("%s not found in index %s, safe set to True, will not raise error" % (_id, index))
                    self.logger.warning(e)
                return False
            else:
                if self.logger:
                    self.logger.error("%s not found in index %s" % (_id, index))
                    self.logger.error(e)
                raise ElasticsearchException("%s not found in index %s" % (_id, index))
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise Exception(e)

    def query(self, index, query):
        documents = []

        try:
            page = self.es.search(index=index, scroll='2m', size=100, body=query)
            sid = page['_scroll_id']
            documents.extend(page['hits']['hits'])
            page_size = page['hits']['total']['value']
        except RequestError as e:
            if self.logger:
                self.logger.error("status code 400: bad request to Elasticsearch, please check your query")
            raise RequestError(e)
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

        while page_size > 0:
            page = self.es.scroll(scroll_id=sid, scroll='2m')
            sid = page['_scroll_id']  # Update the scroll ID

            scroll_document = page['hits']['hits']
            page_size = len(scroll_document)  # Get the number of results that we returned in the last scroll
            documents.extend(scroll_document)
        return documents

    def get_count(self, index, query):
        try:
            data = self.es.count(index=index, body=query)
            return data['count']
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

    def delete_by_id(self, index, _id):
        try:
            if self.logger:
                self.es.delete(index=index, id=_id)
                self.logger('%s successfully deleted from index: %s' % (_id, index))
            return True
        except NotFoundError as e:
            if self.logger:
                self.logger('%s not found in index: %s' % (_id, index))
                self.logger.error(e)
            raise NotFoundError(e)
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

    def update_document(self, index, _id, body, refresh=False):
        try:
            new_doc = {
                'doc_as_upsert': True,
                'doc': body
            }
            if self.logger:
                self.es.update(index, id=_id, body=new_doc, refresh=refresh)
                self.logger.info()
            return True
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)


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

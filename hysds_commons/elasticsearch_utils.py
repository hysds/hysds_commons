from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
import json
from elasticsearch import Elasticsearch, NotFoundError, RequestsHttpConnection, RequestError, ElasticsearchException

standard_library.install_aliases()
# from requests_aws4auth import AWS4Auth


class ElasticsearchUtility:
    def __init__(self, es_url, logger=None, **kwargs):
        # TODO: ADD AWS AUTHENTICATION
        # TODO: https://docs.aws.amazon.com/elasticsearch-service/latest/developerguide/es-request-signing.html
        self.es = Elasticsearch(hosts=[es_url], **kwargs)
        self.es_url = es_url
        self.logger = logger

    def index_document(self, index, doc, _id=None, refresh=False):
        """
        indexing (adding) document to elasticsearch
        :param index: str, elasticsearch index
        :param doc: dict, document body
        :param _id: str
        :param refresh: Boolean, True will refresh the index and document will show up immediately
        :return:
        """
        try:
            if _id:
                result = self.es.index(index=index, id=_id, body=doc, refresh=refresh)
            else:
                if self.logger:
                    self.logger.info("no id provided, Elasticsearch will auto-generate id")
                else:
                    print("no id provided, Elasticsearch will auto-generate id")
                result = self.es.index(index=index, body=doc, refresh=refresh)
            if self.logger:
                self.logger.info("successfully indexed document to index: %s with _id: %s" % (index, result.get('_id')))
                self.logger.info(json.dumps(result))
            else:
                print("successfully indexed document to index: %s with _id: %s" % (index, result.get('_id')))
                print(json.dumps(result))
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
        """
        retrieving document from elasticsearch based on _id
        :param index: str, elasticsearch index
        :param _id: str
        :param safe: Boolean, safe=True will not raise exception
        :param include_source: will return the _source object from the document
        :return: dict
        """
        try:
            data = self.es.get(index=index, id=_id)
            if self.logger:
                self.logger.info("retrieved _id %s from index %s" % (_id, index))
            else:
                print("retrieved _id %s from index %s" % (_id, index))
            return data if include_source else data["_source"]
        except NotFoundError as e:
            if safe is True:
                if self.logger:
                    self.logger.warning("%s not found in index %s" % (_id, index))
                    self.logger.warning("safe set to True, will not raise error")
                    self.logger.warning(e)
                else:
                    print("%s not found in index %s" % (_id, index))
                    print("safe set to True, will not raise error")
                    print(e)
                return False
            else:
                if self.logger:
                    self.logger.error(e)
                raise ElasticsearchException(e)
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise Exception(e)

    def query(self, index, query):
        """
        returns all records returned from a query, through the scroll API
        :param index: str, elasticsearch index
        :param query: dict, document body
        :return: dict
        """
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

        while page_size > 0:  # start scrolling
            page = self.es.scroll(scroll_id=sid, scroll='2m')
            sid = page['_scroll_id']  # Update the scroll ID

            scroll_document = page['hits']['hits']
            page_size = len(scroll_document)  # Get the number of results that we returned in the last scroll
            documents.extend(scroll_document)
        return documents

    def search(self, index, query):
        """
        similar to query method but does not scroll
        should be used for queries expecting only one result (not using the _id field)
        :param index: str, elasticsearch index
        :param query: dict, document body
        :return:
        """
        try:
            result = self.es.search(index=index, body=query)
            return result
        except RequestError as e:
            if self.logger:
                self.logger.error(e)
            raise RequestError(e)
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

    def get_count(self, index, query):
        """returning the count for a given query (warning: ES7 returns max count of 10000)"""
        try:
            data = self.es.count(index=index, body=query)
            return data['count']
        except ElasticsearchException as e:
            if self.logger:
                self.logger.error(e)
            raise ElasticsearchException(e)

    def delete_by_id(self, index, _id):
        """
        TODO: should we add a safe parameter if the user does not want to raise an error if not found
        :param index: str, elasticsearch index
        :param _id: str
        :return: Boolean
        """
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
        """
        updates elasticsearch document using the update API
        :param index: str, elasticsearch index
        :param _id: str
        :param body: dict
        :param refresh: Boolean
        :return: Boolean
        """
        try:
            new_doc = {
                'doc_as_upsert': True,
                'doc': body
            }
            if self.logger:
                self.es.update(index, id=_id, body=new_doc, refresh=refresh)
                self.logger.info("%s: %s updated with new document" % (index, _id))
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

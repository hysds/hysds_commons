from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

import json
from hysds_commons.elasticsearch_utils import get_es_scrolled_data
from elasticsearch import Elasticsearch, NotFoundError, ElasticsearchException

# from . import request_utils


def get_types(es_url, es_index, logger=None):
    """
    Get the listing of spec IDs from an ES index
    @param es_url: elastic search url
    @param es_index: index to list from
    @return: list of ids from given index
    """

    query = {
        "query": {
            "match_all": {}
        }
    }
    results = get_es_scrolled_data(es_url, es_index, query)
    logger.info("Received all _id's from index: %s" % es_index)
    return sorted([result["_id"] for result in results])


def get_all(es_url, es_index, query=None, logger=None):
    """
    Get all spec documents in ES index
    @param es_url: elastic search url
    @param es_index - index containing id
    @return: list of all specification docs
    """

    if query is None:
        query = {
            "query": {
                "match_all": {}
            }
        }
    results = get_es_scrolled_data(es_url, es_index, query)
    logger.info("get_all query: %s" % json.dumps(query, indent=2))
    logger.info("retrieved all data from index: %s" % es_index)
    return results


def get_by_id(es_url, es_index, ident, safe=False, logger=None):
    """
    Get a spec document by ID
    @param es_url: elastic search url
    @param es_index - index containing id
    @param ident - ID
    @param safe - returns False if set to True, raises Exception if set to False
    @return: dict representing anonymous object of specifications
    """

    if ident is None:
        raise Exception("id must be supplied")

    es = Elasticsearch([es_url])
    try:
        dataset_metadata = es.get(index=es_index, id=ident)
    except NotFoundError as e:
        if safe:
            logger.warning("%s not found in index %s" % (ident, es_index))
            logger.warning(e)
            return False
        else:
            logger.error("%s not found in index %s" % (ident, es_index))
            logger.error(e)
            raise Exception("%s not found in index %s" % (ident, es_index))
    except ElasticsearchException as e:
        if logger:
            logger.error(e)
        raise Exception(e)

    # Navigate around Dataset metadata to get true specification
    ret = dataset_metadata["_source"]
    return ret


def add_metadata(es_url, es_index, obj, logger=None):
    """
    Ingests a metadata into the Mozart ElasticSearch index
    @param es_url: elastic search url
    @param es_index - ElasticSearch index to place object into
    @param obj - object for ingestion into ES
    """
    es = Elasticsearch([es_url])
    id = obj['id']
    try:
        es.index(index=es_index, id=id, body=obj)
        logger.info("added to index %s: %s" % (es_index, id))
    except ElasticsearchException as e:
        logger.error("unable to index document: %s" % id)
        logger.error(e)
        raise Exception(e)


def remove_metadata(es_url, es_index, ident, safe=False, logger=None):
    """
    Remove a container
    @param es_url: elastic search url
    @param es_index - ElasticSearch index to place object into
    @param ident - id of container to delete
    """
    if ident is None:
        raise Exception("id must be supplied")

    es = Elasticsearch([es_url])
    try:
        es.delete(index=es_index, id=ident)
        logger.info("%s deleted from index: %s" % (ident, es_index))
    except NotFoundError as e:
        if safe:
            logger.warning("%s not found in index %s" % (ident, es_index))
            logger.warning(e)
            return False
        else:
            logger.error("%s not found in index %s" % (ident, es_index))
            logger.error(e)
            raise Exception("%s not found in index %s" % (ident, es_index))
    except ElasticsearchException as e:
        if logger:
            logger.error(e)
        raise Exception(e)

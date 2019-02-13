import requests
import json
from . import request_utils


def get_types(es_url, es_index, logger=None):
    '''
    Get the listing of spec IDs from an ES index
    @param es_url: elastic search url
    @param es_index: index to list from
    @return: list of ids from given index
    '''

    query = {"query": {"match_all": {}}}
    url = "{0}/{1}/_search".format(es_url, es_index)
    es_url = "{0}/_search".format(es_url)
    data = json.dumps(query)
    results = request_utils.post_scrolled_json_responses(
        url, es_url, data=data, logger=logger)
    return sorted([result["_id"] for result in results])


def get_all(es_url, es_index, es_type, query=None, logger=None):
    '''
    Get all spec documents in ES index
    @param es_url: elastic search url
    @param es_index - index containing id
    @param es_type - index containing type
    @return: list of all specification docs
    '''

    if query is None:
        query = {"query": {"match_all": {}}}
    url = "{0}/{1}/_search".format(es_url, es_index)
    es_url = "{0}/_search".format(es_url)
    return request_utils.post_scrolled_json_responses(url, es_url, data=json.dumps(query), logger=logger)


def get_by_id(es_url, es_index, es_type, ident, logger=None):
    '''
    Get a spec document by ID
    @param es_url: elastic search url
    @param es_index - index containing id
    @param es_type - index containing type
    @param ident - ID
    @return: dict representing anonymous object of specifications
    '''

    if ident is None:
        raise Exception("id must be supplied")
    final_url = '{0}/{1}/{2}/{3}'.format(es_url, es_index, es_type, ident)
    dataset_metadata = request_utils.get_requests_json_response(
        final_url, logger=logger)
    # Navigate around Dataset metadata to get true specification
    ret = dataset_metadata["_source"]
    return ret


def add_metadata(es_url, es_index, es_type, obj, logger=None):
    '''
    Ingests a metadata into the Mozart ElasticSearch index
    @param es_url: elastic search url
    @param es_index - ElasticSearch index to place object into
    @param es_type - ElasticSearch type to place object into
    @param obj - object for ingestion into ES
    '''

    #data = {"doc_as_upsert": True,"doc":obj}
    final_url = "{0}/{1}/{2}/{3}".format(es_url, es_index, es_type, obj["id"])
    request_utils.requests_json_response(
        "POST", final_url, json.dumps(obj), logger=logger)


def remove_metadata(es_url, es_index, es_type, ident, logger=None):
    '''
    Remove a container
    @param es_url: elastic search url
    @param es_index - ElasticSearch index to place object into
    @param es_type - ElasticSearch type to place object into
    @param ident - id of container to delete
    '''

    final_url = "{0}/{1}/{2}/{3}".format(es_url, es_index, es_type, ident)
    request_utils.requests_json_response("DELETE", final_url, logger=logger)

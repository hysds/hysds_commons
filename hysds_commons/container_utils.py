from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import hysds_commons.metadata_rest_utils


CONTAINER_INDEX = "containers"


def get_container_types(es_url, logger=None):
    '''
    Get the container listings from Elastic Search
    @param es_url - elasticsearch URL
    @return: list of container ids
    '''
    return hysds_commons.metadata_rest_utils.get_types(es_url, CONTAINER_INDEX, logger=logger)


def get_container(es_url, ident, logger=None):
    '''
    Get a container (JSON body)
    @param es_url - elasticsearch URL
    @param ident - identity of container
    @param container_type - mapping type for container index, only _doc type allowed in es7
    @return: dict representing anonymous object of HySDS IO
    '''
    return hysds_commons.metadata_rest_utils.get_by_id(es_url, CONTAINER_INDEX, ident, logger=logger)


def add_container(es_url, name, url, version, digest, logger=None):
    '''
    Ingests a container into the Mozart ElasticSearch index
    @param es_url - elasticsearch URL
    @param name - name of object for ingestion into ES
    @param url - url of object for ingestion into ES
    @param version - version of object for ingestion into ES
    @param container_type - mapping type for container index, only _doc type allowed in es7
    @param digest - sha256 digest ID of container image
    '''
    container_obj = {
        "id": name,
        "digest": digest,
        "url": url,
        "version": version
    }
    return hysds_commons.metadata_rest_utils.add_metadata(es_url, CONTAINER_INDEX, container_obj, logger=logger)


def remove_container(es_url, ident, logger=None):
    '''
    Remove a container
    @param es_url - elasticsearch URL
    @param ident - id to delete
    @param container_type - mapping type for container index, only _doc type allowed in es7
    '''
    return hysds_commons.metadata_rest_utils.remove_metadata(es_url, CONTAINER_INDEX, ident, logger=logger)

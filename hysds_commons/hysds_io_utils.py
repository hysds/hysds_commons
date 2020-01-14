from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import hysds_commons.metadata_rest_utils


HYSDS_IO_INDEX = "hysds_ios"


def get_hysds_io_types(es_url, logger=None):
    '''
    Get the hysds_io listings from Elastic Search
    @param es_url - elastic search URL (from owning app i.e. Mozart, Tosca)
    @return: list of hysds_io ids
    '''
    return hysds_commons.metadata_rest_utils.get_types(es_url, HYSDS_IO_INDEX, logger=logger)


def get_hysds_ios(es_url, logger=None):
    '''
    Get all hysds_io docs from ES
    @param es_url - elastic search URL (from owning app i.e. Mozart, Tosca)
    @param hysds_io_type - str - ES type in the hysds_ios index (optional for now because types are removed in es7+)
    @return: dict representing anonymous object of HySDS IO
    '''
    return hysds_commons.metadata_rest_utils.get_all(es_url, HYSDS_IO_INDEX, logger=logger)


def get_hysds_io(es_url, ident, logger=None):
    '''
    Get a hysds_io (JSON body)
    @param es_url - elastic search URL (from owning app i.e. Mozart, Tosca)
    @param ident - identity of hysds_io
    @param hysds_io_type - str - ES type in the hysds_ios index (optional for now because types are removed in es7+)
    @return: dict representing anonymous object of HySDS IO
    '''
    return hysds_commons.metadata_rest_utils.get_by_id(es_url, HYSDS_IO_INDEX, ident, logger=logger)


def add_hysds_io(es_url, obj, logger=None):
    '''
    Ingests a hysds_io into the Mozart ElasticSearch index
    @param es_url - elastic search URL (from owning app i.e. Mozart, Tosca)
    @param obj - object for ingestion into ES
    @param hysds_io_type - str - ES type in the hysds_ios index (optional for now because types are removed in es7+)
    '''
    return hysds_commons.metadata_rest_utils.add_metadata(es_url, HYSDS_IO_INDEX, obj, logger=logger)


def remove_hysds_io(es_url, ident, logger=None):
    '''
    Remove a container
    @param es_url - elastic search URL (from owning app i.e. Mozart, Tosca)
    @param ident - id to delete
    '''
    return hysds_commons.metadata_rest_utils.remove_metadata(es_url, HYSDS_IO_INDEX, ident, logger=logger)

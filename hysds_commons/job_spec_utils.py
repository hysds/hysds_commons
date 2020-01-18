from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import hysds_commons.metadata_rest_utils


JOB_SPEC_INDEX = "job_specs"


def get_job_spec_types(es_url, logger=None):
    '''
    Get the job_spec listings from Elastic Search
    @param es_url - elasticsearch URL
    @return: list of job_spec ids
    '''
    return hysds_commons.metadata_rest_utils.get_types(es_url, JOB_SPEC_INDEX, logger=logger)


def get_job_specs(es_url, logger=None):
    '''
    Get all job spec docs from ES
    @param es_url - elastic search URL
    @param job_spec_type - es index type, now _doc type in es7+, preserve for now
    @return: dict representing anonymous object of job specs
    '''
    return hysds_commons.metadata_rest_utils.get_all(es_url, JOB_SPEC_INDEX, logger=logger)


def get_job_spec(es_url, ident, logger=None):
    '''
    Get a job_spec (JSON body)
    @param es_url - elasticsearch URL
    @param ident - identity of job_spec
    @param job_spec_type - es index type, now _doc type in es7+, preserve for now
    @return: dict representing anonymous object of HySDS IO
    '''
    return hysds_commons.metadata_rest_utils.get_by_id(es_url, JOB_SPEC_INDEX, ident, logger=logger)


def add_job_spec(es_url, obj, logger=None):
    '''
    Ingests a job_spec into ES
    @param es_url - elasticsearch URL
    @param job_spec_type - es index type, now _doc type in es7+, preserve for now
    @param obj - object for ingestion into ES
    '''
    return hysds_commons.metadata_rest_utils.add_metadata(es_url, JOB_SPEC_INDEX, obj, logger=logger)


def remove_job_spec(es_url, ident, logger=None):
    '''
    Remove a container
    @param es_url - elasticsearch URL
    @param job_spec_type - es index type, now _doc type in es7+, preserve for now
    @param ident - id to delete
    '''
    return hysds_commons.metadata_rest_utils.remove_metadata(es_url, JOB_SPEC_INDEX, ident, logger=logger)

import os
import json
from hysds_commons.request_utils import requests_json_response


DEFAULT_MOZART_VERSION = "v0.1"


def mozart_call(mozart_url, method, data, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Call mozart method with data
    @param mozart_url - url to mozart
    @param method - method to call
    @param data - data to supply to call
    @param version - mozart API version
    @param logger - logger to log to
    '''

    url = os.path.join(mozart_url, "api", version, method)
    getpost = "GET"
    if method == "job/submit":
        getpost = "POST"
    res = requests_json_response(
        getpost, url, data=data, verify=False, logger=logger)
    return res["result"]


def get_job_spec(mozart_url, ident, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Queries Mozart for the job type
    @param mozart_url - url to mozart
    @param ident - id of the job type
    @param version - mozart API version
    @param logger - logger to log to
    '''
    return mozart_call(mozart_url, "job_spec/type", {"id": ident}, version, logger)


def get_job_spec_list(mozart_url, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Queries Mozart for the job types
    @param mozart_url - url to mozart
    @param version - mozart API version
    @param logger - logger to log to
    '''

    lst = mozart_call(mozart_url, "job_spec/list", {}, version, logger)
    return lst


def get_queue_list(mozart_url, ident=None, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Queries Mozart for the active queues
    @param mozart_url - url to mozart
    @param version - mozart API version
    @param logger - logger to log to
    '''

    data = {}
    if not ident is None:
        data = {"id": ident}
    return mozart_call(mozart_url, "queue/list", data, version, logger)


def submit_job(mozart_url, data, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Submit a job with given data
    @param mozart_url - url to mozart
    @param data - data to submit as job
    @param version - mozart API version
    @param logger - logger to log to
    '''
    return mozart_call(mozart_url, "job/submit", data, version, logger)

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

import os
import json
import requests
from hysds_commons.request_utils import requests_json_response


DEFAULT_MOZART_VERSION = "v0.1"


def mozart_call(mozart_url, method, data, version=DEFAULT_MOZART_VERSION, logger=None):
    """
    Call mozart method with data
    @param mozart_url - url to mozart
    @param method - method to call
    @param data - data to supply to call
    @param version - mozart API version
    @param logger - logger to log to
    """

    url = os.path.join(mozart_url, "api", version, method)

    if method == "job/submit":  # POST request
        req = requests.post(url, data=data, verify=False)
        req.raise_for_status()
    else:  # GET request
        req = requests.get(url, data=data, verify=False)
        req.raise_for_status()

    res = req.json()
    return res["result"]


def submit_job(mozart_url, data, version=DEFAULT_MOZART_VERSION, logger=None):
    '''
    Submit a job with given data
    @param mozart_url - url to mozart
    @param data - data to submit as job
    @param version - mozart API version
    @param logger - logger to log to
    '''
    return mozart_call(mozart_url, "job/submit", data, version, logger)

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import str
from future import standard_library
standard_library.install_aliases()
import traceback
import requests
import json


def requests_json_response(method, url, data="{}", ignore_errors=False, auth=None, verify=True, logger=None,
                           attached_headers=None):
    '''
    Sends a request with supplied data and method
    @param method - "GET" or "POST" method
    @param url - url to request
    @param data - data to send along with request
    @return: dictionary representing JSON object
    '''

    # headers = {"Content-Type": "application/json"}
    try:
        if method == "GET":
            r = requests.get(url, data=data, auth=auth, verify=verify)
        elif method == "POST":
            if attached_headers:
                r = requests.post(url, data=data, auth=auth, verify=verify, headers=attached_headers)
            else:
                r = requests.post(url, data=data, auth=auth, verify=verify)
        elif method == "DELETE":
            r = requests.delete(url, auth=auth, verify=verify)
        else:
            raise Exception(
                "requests_json_response doesn't support request-method: {0}".format(method))
        r.raise_for_status()
        return r.json()
    except Exception as e:
        message = "Failed to '{0}' to {1}. Exception: {2}:{3}.\nData: {4}".format(
            method, url, type(e), str(e), data)
        # If the response is resolvable as JSON, return it in the message
        try:
            message = message + \
                "\nResponse: {5}".format(json.dumps(r.json(), indent=2))
        except:
            pass
        # Log if the logger was passed in
        if not logger is None:
            logger.warning(message)
        if not ignore_errors:
            raise(e)


def get_requests_json_response(url, **kwargs):
    '''
    Calls requests_json_response with "GET" argument
    @param url - url param
    @param kwargs - passthrough kwargs
    '''
    return requests_json_response("GET", url, **kwargs)


def post_requests_json_response(url, **kwargs):
    '''
    Calls requests_json_response with "POST" argument
    @param url - url param
    @param kwargs - passthrough kwargs
    '''
    return requests_json_response("POST", url, **kwargs)


def post_scrolled_json_responses(url, es_url, generator=False, **kwargs):
    '''
    Calls get_json_rsponse in a scrolling manner (ES compatible)
    @param url - url to setup scan
    @param es_url - es url to scan through
    @param kwargs - pass through kwargs
    @param generator - True if we should return a generator and not a list
    @return: list of results from scrolled ES results
    '''

    def getResultsGenerator():
        if not url.rstrip("/").endswith("_search"):
            raise Exception(
                "Scrolling only works on search URLs. {0} incompatible.".format(url))
        setup_url = url + "?search_type=scan&scroll=10m&size=100"
        result = post_requests_json_response(setup_url, **kwargs)
        # Harvest scan-setup
        count = result['hits']['total']
        scroll_id = result['_scroll_id']
        scroll_url = es_url + "/scroll?scroll=10m"
        while True:
            # Data is no longer a query, and now a scroll_id
            kwargs["data"] = scroll_id
            result = post_requests_json_response(scroll_url, **kwargs)
            scroll_id = result['_scroll_id']
            if len(result['hits']['hits']) == 0:
                break
            for hit in result['hits']['hits']:
                yield hit

    gen = getResultsGenerator()
    if generator:
        return gen
    return [res for res in gen]

#!/bin/env python
import copy
import json
import traceback
import logging

from hysds_commons.request_utils import post_scrolled_json_responses
from hysds_commons.hysds_io_utils import get_hysds_io
from hysds_commons.job_utils import submit_mozart_job

from hysds.celery import app


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("job-iterator")


def get_component_config(component):
    '''
    From a component get the common configuration values
    @param component - component 
    '''
    if component == "mozart" or component == "figaro":
        es_url = app.conf["JOBS_ES_URL"]
        query_idx = app.conf["STATUS_ALIAS"]
        facetview_url = app.conf["MOZART_URL"]
    elif component == "tosca":
        es_url = app.conf["GRQ_ES_URL"]
        query_idx = app.conf["DATASET_ALIAS"]
        facetview_url = app.conf["GRQ_URL"]
    return (es_url, query_idx, facetview_url)


def normalize_query(rule):
    """Normalize final query."""

    if rule.get('passthru_query', False) is True:
        query = rule['query']
        filts = []
        if 'filtered' in query:
            final_query = copy.deepcopy(query)
            filts.append(final_query['filtered']['filter'])
            final_query['filtered']['filter'] = {
                'and': filts
            }
        else:
            final_query = {
                'filtered': {
                    'query': query
                }
            }
        final_query = {"query": final_query}
        logger.info("final_query: %s" % json.dumps(final_query, indent=2))
        rule['query'] = final_query
        rule['query_string'] = json.dumps(final_query)


def iterate(component, rule):
    '''
    Iterator used to iterate across a query result and submit jobs for every hit
    @param component - "mozart" or "tosca" where this submission came from
    @param rule - rule containing information for running jobs
    '''
    # Accumulators variables
    ids = []
    error_count = 0
    errors = []

    # Read config from "origin"
    es_url, es_index, ignore1 = get_component_config(component)

    # Read in JSON formatted args and setup passthrough
    normalize_query(rule)
    if 'query' in rule.get('query', {}):
        queryobj = rule["query"]
    else:
        queryobj = {"query": rule["query"]}

    # Get wiring
    hysdsio = get_hysds_io(es_url, rule["job_type"], logger=logger)

    # Is this a single submission
    passthru = rule.get('passthru_query', False)
    single = hysdsio.get(
        "submission_type", "individual" if passthru is True else "iteration") == "individual"
    logger.info("single submission type: %s" % single)

    # Do we need the results
    run_query = False if single else True
    if not run_query:  # check if we need the results anyway
        run_query = any((i["from"].startswith('dataset_jpath')
                         for i in hysdsio["params"]))
    logger.info("run_query: %s" % run_query)

    # Run the query to get the products; for efficiency, run query only if we need the results
    results = [{"_id": "Transient Faux-Results"}]
    if run_query:
        # Scroll product results
        start_url = "{0}/{1}/_search".format(es_url, es_index)
        scroll_url = "{0}/_search".format(es_url, es_index)
        results = post_scrolled_json_responses(start_url, scroll_url, data=json.dumps(queryobj),
                                               logger=logger, generator=True)

    # What to iterate for submission
    submission_iterable = [
        {"_id": "Global Single Submission"}] if single else results
    # Iterator loop
    for item in submission_iterable:
        try:
            # For single submissions, submit all results as one
            product = results if single else item
            logger.info("Submitting mozart job for product: %s" % product)

            # set clean descriptive job name
            job_type = rule['job_type']
            if job_type.startswith('hysds-io-'):
                job_type = job_type.replace('hysds-io-', '', 1)
            if isinstance(product, dict):
                job_name = "%s-%s" % (job_type, product.get('_id', 'unknown'))
            else:
                job_name = "%s-single_submission" % job_type

            # disable dedup for passthru single submissions
            enable_dedup = False if not run_query and single else True
            logger.info("enable_dedup: %s" % enable_dedup)

            # override enable_dedup setting from hysdsio
            if 'enable_dedup' in hysdsio:
                enable_dedup = hysdsio['enable_dedup']
                logger.info("hysdsio overrided enable_dedup: %s" %
                            enable_dedup)

            ids.append(submit_mozart_job(product, rule, hysdsio,
                                         job_name=job_name,
                                         enable_dedup=enable_dedup))
        except Exception as e:
            error_count = error_count + 1
            if not str(e) in errors:
                errors.append(str(e))
            logger.warning(
                "Failed to submit jobs: {0}:{1}".format(type(e), str(e)))
            logger.warning(traceback.format_exc())
    if error_count > 0:
        logger.error("Failed to submit: {0} of {1} jobs. {2}".format(
            error_count, len(list(results)), " ".join(errors)))
        raise Exception("Job Submitter Job failed to submit all actions")

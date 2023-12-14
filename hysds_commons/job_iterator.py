#!/bin/env python
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import str
from future import standard_library
standard_library.install_aliases()

import json
import traceback
import logging

from hysds.celery import app
from hysds.es_util import get_mozart_es, get_grq_es
from hysds_commons.job_utils import submit_mozart_job

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("job-iterator")

HYSDS_IOS_GRQ = app.conf.get('HYSDS_IOS_GRQ', 'hysds_ios-grq')
HYSDS_IOS_MOZART = app.conf.get('HYSDS_IOS_MOZART', 'hysds_ios-mozart')

mozart_es = get_mozart_es()
grq_es = get_grq_es()


def get_component_config(component):
    """
    From a component get the common configuration values
    @param component - component 
    """
    if component == "mozart" or component == "figaro":
        query_idx = app.conf["STATUS_ALIAS"]
        facetview_url = app.conf["MOZART_URL"]
    else:  # tosca:
        query_idx = app.conf["DATASET_ALIAS"]
        facetview_url = app.conf["GRQ_URL"]
    return query_idx, facetview_url


def iterate(component, rule):
    """
    Iterator used to iterate across a query result and submit jobs for every hit
    @param component - "mozart" or "tosca" where this submission came from
    @param rule - rule containing information for running jobs, note - NOT A USER RULE
    """
    ids = []  # Accumulators variables
    error_count = 0
    errors = []

    es_index, ignore1 = get_component_config(component)  # Read config from "origin"

    # Read in JSON formatted args and setup passthrough
    if 'query' in rule.get('query', {}):
        queryobj = rule["query"]
    else:
        queryobj = {
            "query": rule["query"]
        }
        rule['query'] = {
            "query": rule['query']
        }
    logger.info("Elasticsearch queryobj: %s" % json.dumps(queryobj))

    # Get hysds_ios wiring
    hysds_io_index = HYSDS_IOS_MOZART if component in ('mozart', 'figaro') else HYSDS_IOS_GRQ
    hysdsio = mozart_es.get_by_id(index=hysds_io_index, id=rule["job_type"])
    hysdsio = hysdsio['_source']

    # Is this a single submission
    passthru = rule.get('passthru_query', False)
    single = hysdsio.get("submission_type", "individual" if passthru is True else "iteration") == "individual"
    logger.info("single submission type: %s" % single)

    # Do we need the results
    run_query = False if single else True
    if not run_query:  # check if we need the results anyway
        run_query = any((i["from"].startswith('dataset_jpath') for i in hysdsio["params"]))
    logger.info("run_query: %s" % run_query)

    # Run the query to get the products; for efficiency, run query only if we need the results
    results = [{"_id": "Transient Faux-Results"}]
    sort = ["@timestamp:desc", "id.keyword:asc"]
    if run_query:
        if component == "mozart" or component == "figaro":
            results = mozart_es.query(index=es_index, body=queryobj, sort=sort)
        else:
            results = grq_es.query(index=es_index, body=queryobj, sort=sort)

    # What to iterate for submission
    submission_iterable = [{"_id": "Global Single Submission"}] if single else results

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

            # get enable_dedup flag: rule > hysdsio
            if rule.get("enable_dedup") is None:
                rule['enable_dedup'] = hysdsio.get("enable_dedup", True)

            task_id = submit_mozart_job(product, rule, hysdsio, job_name=job_name)
            ids.append(task_id)

        except Exception as e:
            error_count = error_count + 1
            if not str(e) in errors:
                errors.append(str(e))
            logger.warning("Failed to submit jobs: {0}:{1}".format(type(e), str(e)))
            logger.warning(traceback.format_exc())

    if error_count > 0:
        logger.error("Failed to submit: {0} of {1} jobs. {2}".format(
            error_count, len(list(results)), " ".join(errors)))
        raise Exception("Job Submitter Job failed to submit all actions")

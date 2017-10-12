import json, traceback

from hysds_commons.job_utils import resolve_mozart_job
from hysds_commons.mozart_utils import submit_job
from hysds_commons.log_utils import logger


def single_process_and_submission(mozart_url, product, rule, hysdsio=None,
                                  es_hysdsio_url=None, queue=None):
    '''
    Run a single job from inside the iterator
    @param mozart_url - mozart url to submit to
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param es_hysdsio_url - (optional) url to request hysdsio data from
    @param queue - (optional) job queue override
    '''

    # resolve job
    job = resolve_mozart_job(product, rule, hysdsio, es_hysdsio_url, queue)
    logger.info("resolved job: {}".format(json.dumps(job, indent=2)))

    # submit job
    res = submit_job(mozart_url, job)
    logger.info("submitted job to {}".format(mozart_url))

    return res

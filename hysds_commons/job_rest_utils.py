from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

import json
from hysds_commons.job_utils import resolve_mozart_job
from hysds_commons.mozart_utils import submit_job
from hysds_commons.log_utils import logger


def single_process_and_submission(mozart_url, product, rule, hysdsio=None, queue=None, component=None):
    """
    Run a single job from inside the iterator
    @param mozart_url - mozart url to submit to
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param component - (optional) tosca/grq or mozart/figaro, get hysds_io from ES (hysds_ios-mozart vs hysds_ios-grq)
    @param queue - (optional) job queue override
    """

    # resolve job
    job = resolve_mozart_job(product, rule, hysdsio, queue, component)
    logger.info("resolved job: {}".format(json.dumps(job, indent=2)))

    # submit job
    res = submit_job(mozart_url, job)
    logger.info("submitted job to {}".format(mozart_url))

    return res

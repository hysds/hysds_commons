from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import os
import sys
import operator
import functools
import json
import traceback

import hysds_commons.job_spec_utils
import hysds_commons.hysds_io_utils
from hysds_commons.log_utils import logger


def create_action_from_entry(jobspec_es_url, wiring, ops_account="ops"):
    '''
    Create an action from an spec pulled from ES
    @param jobspec_es_url: job spec ES url
    @param wiring: wiring specification from HySDS
    @param ops_account: string name of ops account
    @return: action ready for display
    '''

    try:
        # Setup the basic information for these actions
        action_type = wiring.get("action-type", "both")
        action = {
            "allowed_accounts": [ops_account],
            "monitoring_allowed": action_type == "trigger" or action_type == "both",
            "processing_allowed": action_type == "on-demand" or action_type == "both",
            "public": False
        }

        # Break-out user inputs into kwargs
        # and look for "passthrough", to set the correct paramater
        action["kwargs"] = [param for param in wiring.get(
            "params", []) if param.get("from", "") == "submitter"]
        for arg in action["kwargs"]:
            arg["validator"] = {"required": not arg.get("optional", False)}
            arg["type"] = arg.get("type", "text")
            arg["placeholder"] = arg.get("placeholder", arg["name"])
        action["passthru_query"] = functools.reduce(
            lambda x, param: operator.or_(
                x, param["from"] == "passthrough" and param["name"] == "query"),
            wiring.get("params", []), False)

        # Setup user permissions
        if "allowed_accounts" in wiring:
            accounts = wiring.get("allowed_accounts", [ops_account])
            if not ops_account in accounts:
                accounts.append(ops_account)
            action["allowed_accounts"] = accounts
            # If accounts are explicit, this is not public
            action["public"] = "_all" in action["allowed_accounts"]

        # Setup action requirements
        if "label" in wiring:
            action["label"] = "{0} [{1}]".format(wiring.get(
                "label"), wiring.get("job-version", "unknown-version"))
        else:
            label = wiring.get("id", "unknown-job:unknown-version")
            try:
                sp = label.split(":")
                label = "{0} [{1}]".format(sp[0], sp[1])
            except Exception as e:
                logger.warning("Error: {0}:{1}".format(type(e), e))
            action["label"] = label
        action["type"] = wiring.get("id", "unknown-job")
        action["job_type"] = wiring.get("id", "unknown-job")
        #action["queues"] = hysds_commons.mozart_utils.get_queue_list(mozart_url,wiring["job-specification"])
        #action["specification"] = spec
        action["wiring"] = wiring
    except Exception as e:
        logger.warning("Caught exception {}:\n{}".format(
            type(e), traceback.format_exc()))
        return {
            "allowed_accounts": ['ops'],
            "monitoring_allowed": False,
            "processing_allowed": False,
            "public": False
        }
    return action


def get_action_spec(iospec_es_url, jobspec_es_url, ops_account="ops"):
    """
    Returns action spec
    @param iospec_es_url: ES url for IO specs
    @param jobspec_es_url: ES url for job specs
    """

    action_specs = []
    wirings = [spec.get('_source', {})
               for spec in hysds_commons.hysds_io_utils.get_hysds_ios(iospec_es_url)]
    #logger.info("wirings: %s" % json.dumps(wirings, indent=2))
    jobs = hysds_commons.job_spec_utils.get_job_spec_types(jobspec_es_url)
    #logger.info("jobs: %s" % json.dumps(jobs, indent=2))
    for wiring in wirings:
        if not "job-specification" in wiring or not wiring["job-specification"] in jobs:
            continue
        action_specs.append(create_action_from_entry(
            jobspec_es_url, wiring, ops_account))
    #logger.info("action_specs: %s" % json.dumps(action_specs, indent=2))
    return action_specs


def check_passthrough_query(params):
    """
    returns True if params is:
    {
        "from": "passthrough",
        "name": "query"
    }
    """
    for param in params:
        if param['from'] == 'passthrough' and param['name'] == 'query':
            return True
    return False

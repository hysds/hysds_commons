from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import dict
from builtins import int
from future import standard_library
standard_library.install_aliases()
import os
import pwd
import json
import traceback
import types
import collections

from hysds_commons.job_spec_utils import get_job_spec
from hysds_commons.hysds_io_utils import get_hysds_io
from hysds_commons.container_utils import get_container
from hysds_commons.log_utils import logger

from hysds.celery import app
from hysds.orchestrator import do_submit_job


def get_username():
    """Get username."""

    try:
        return pwd.getpwuid(os.getuid())[0]
    except:
        return None


def get_params_for_products_set(wiring, kwargs, passthrough=None, products=None):
    '''
    Get parameters for parameters for  set of products
    @param wiring: wiring specification
    @param kwargs: key word arguments from submitter
    @param passthrough: rule containing passthrough arguments
    @param products: a list of products
    '''

    params = {}
    if products is None:
        return params
    for prod in products:
        get_params_for_submission(
            wiring, kwargs, passthrough, prod, params, aggregate=True)
    return params


def get_params_for_submission(wiring, kwargs, passthrough=None, product=None,
                              params=None, aggregate=False):
    '''
    Get params for submission for HySDS/Tosca style workflow
    @param wiring - wiring specification
    @param kwargs - arguments from user form
    @param passthrough - rule
    @param product - product
    @param params - existing params
    '''
    logger.info("wiring: %s" % json.dumps(wiring, indent=2))
    logger.info("kwargs: %s" % json.dumps(kwargs, indent=2))
    logger.info("passthrough: %s" % json.dumps(passthrough, indent=2))
    logger.info("product: %s" % json.dumps(product, indent=2))
    if params is None:
        params = {}
    for wire in wiring["params"]:
        # Aggregated results from dataset_jpath:... are put into a list
        if aggregate and wire["from"].startswith("dataset_jpath:"):
            val = get_inputs(wire, kwargs, passthrough, product)
            val = run_type_conversion(wire, val)
            val = run_lambda(wire, val)
            if not wire["name"] in params:
                params[wire["name"]] = []
            params[wire["name"]].append(val)
        # Non-aggregated and non-dataset_jpath fields are set once
        elif not wire["name"] in params:
            val = get_inputs(wire, kwargs, passthrough, product)
            val = run_type_conversion(wire, val)
            val = run_lambda(wire, val)
            params[wire["name"]] = val
    return params


def run_type_conversion(wire, val):
    '''
    Run a conversion from the input string to a known type
    @param wire: hysds-wiring record
    @param val: value to convert
    @returns: type converted value
    '''
    # Unspefied types are text
    param_type = wire.get("type", "text")
    # Unfilled optional parameters get the empty string as a converted type
    if wire.get("optional", False) and val.strip() == "":
        return val.strip()
    elif param_type == "number":
        return float(val)
    elif param_type == "boolean":
        return val.lower() == "true"
    elif param_type in ["region"]:
        return json.loads(val)
    else:
        return val


def run_lambda(wire, val):
    '''
    Runs the lambda key as a lambda function with 1 arg, the previous value
    @param wire - wiring spec to check for lambda
    @param val
    '''

    if "lambda" in wire:
        try:
            if not wire["lambda"].startswith("lambda:") and not wire["lambda"].startswith("lambda "):
                raise RuntimeError(
                    "[ERROR] Failed to run lambda function, must be lambda expression taking 0 or 1 inputs")
            import functools
            from . import lambda_builtins
            namespace = {"functools": functools}
            for name in dir(lambda_builtins):
                if name.startswith("__"):
                    continue
                namespace[name] = lambda_builtins.__dict__[name]
            fn = eval(wire["lambda"], namespace, {})
            val = fn(val)
        except Exception as e:
            raise RuntimeError("[ERROR] Failed to run lambda function to fill {0}. {1}:{2}".format(
                wire["name"], type(e), e))
    return val


def get_inputs(param, kwargs, rule=None, product=None):
    '''
    Update parameter to add in a value for the param
    @param param - parameter to update
    @param kwargs - inputs from user form
    @param rule - (optional) rule hit to use to fill pass throughs
    @param product - (optional) product hit for augmenting
    '''

    # Break out if value is known
    if "value" in param:
        ret = param["value"]
        return ret
    source = param.get("from", "unknown")
    # Get a value
    ret = param.get("default_value", None)
    if source == "submitter":
        ret = kwargs.get(param.get("name", "unknown"), None)
    elif source == "passthrough" and not rule is None:
        ret = rule.get(param["name"], None)
    elif source.startswith("dataset_jpath:") and not product is None:
        # If we are processing a list of products, create a list for outputs
        ret = process_xpath(source.split(":")[1], product)
    # Check value is found
    if ret is None and not product is None and not rule is None:
        raise RuntimeError("Failed to find '{0}' input from '{1}'".format(
            param.get("name", "unknown"), source))
    return ret


def process_xpath(xpath, trigger):
    '''
    Process the xpath to extract data from a trigger
    @param xpath - xpath location in trigger
    @param trigger - trigger metadata to extract XPath
    '''

    ret = trigger
    parts = xpath.replace("xpath.", "").split(".")
    for part in parts:
        if ret is None or part == "":
            return ret
        # Try to convert to integer, if possible, for list indicies
        try:
            part = int(part)
            if len(ret) <= part:
                ret = None
            else:
                ret = ret[part]
            continue
        except:
            pass
        ret = ret.get(part, None)
    return ret


def match_inputs(param, context):
    '''
    Update parameter to add in a value for the param
    @param param - parameter to update
    @param context - context of job specification
    '''

    # Break out if value is known
    if "value" in param:
        return
    source = param.get("source", "unknown")
    # Get a value
    param["value"] = param.get("default_value", None)
    if source == "submitter":
        param["value"] = context.get("inputs", {}).get(
            param.get("name", "unknown"), None)
    elif source == "context":
        param["value"] = context.get(param.get("name"), None)
    elif source.startswith("xpath."):
        param["value"] = process_xpath(source, context.get("trigger", {}))
    # Check value is found
    if param["value"] is None:
        raise RuntimeError("Failed to find '{0}' input from '{1}'".format(
            param.get("name", "unknown"), source))


def fill_localize(value, localize_urls):
    '''
    Fill the localize product list. Must handle a value that is one of the following:
        1. A URL as a string, should be wrapped like {"url":stinrg}
        2. An object defining "url". Does not need wrapping
        3. Another list containing URLs/objects. Should be flattened
    Note: this function recurses to handle sub-lists
    @param value: a list conforming to points 1-3 above
    @param localize_urls: in-out list to append localize object to
           Note: this parameter is modified
    '''
    # Check for string that need to be wrapped in
    # localize format
    if isinstance(value, str):
        localize_urls.append({"url": value})
    # Check for objects that define the "url" field, and if so
    # they can be passed right in
    elif not getattr(value, "get", None) is None and not value.get("url", None) is None:
        localize_urls.append(value)
    # Other types of objects must throw an error
    elif not getattr(value, "get", None) is None:
        raise ValueError("Invalid object of type {0} trying to be localized. {1}".format(
            type(value), value))
    # Handle lists and other iterables by recursing
    else:
        for val in value:
            fill_localize(val, localize_urls)


def route_outputs(param, context, positional, localize_urls):
    '''
    Route the input parameters to their destinations
    @param param - parameter specification
    @param context - context object to fill with context-destined items
    @param positional - list to append positional arguments to
    @param localize_urls - list to append localize urls to
    '''

    destination = param.get("destination", "unknown")
    if destination == "positional":
        context[param["name"]] = param.get("value", "")
        positional.append(context[param["name"]])
    elif destination == "localize":
        val = param.get("value", "")
        fill_localize(val, localize_urls)
    elif destination == "context":
        context[param["name"]] = param.get("value", "")


def get_command_line(command, positional):
    '''
    Gets the command line invocation for this submission by
    concatenating the command, and positional arguments
    @param command - command line from job spec
    @param positional - positional arguments
    @return: command strin with args, or None
    '''

    parts = []
    if not command is None:
        parts.append(command)
    for posit in positional:
        # Escape any single quotes
        if not isinstance(posit, str):
            posit = "{0}".format(json.dumps(posit))
        posit = posit.replace("'", "'\"'\"'")
        # Add in encapsulating single quotes
        parts.append("'{0}'".format(posit))
    ret = " ".join(parts)
    return ret


def resolve_mozart_job(product, rule, hysdsio=None, es_hysdsio_url=None, queue=None):
    '''
    Resolve Mozart job JSON.
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param es_hysdsio_url - (optional) url to request hysdsio data from
    @param queue - (optional) job queue override
    '''

    logger.info("rule: %s" % json.dumps(rule, indent=2))
    logger.info("hysdsio: %s" % json.dumps(hysdsio, indent=2))
    logger.info("es_hysdsio_url: %s" % es_hysdsio_url)
    logger.info("queue: %s" % queue)

    # override queue
    queue = rule['queue'] if queue is None else queue

    # ensure priority is int
    if rule['priority'] is not None:
        rule["priority"] = int(rule['priority'])

    # this is the common data for all jobs, and will be copied for each individual submission
    if hysdsio is None and es_hysdsio_url is None:
        message = "[ERROR] Must supply a hysds-io object or a ES-URL to request one"
        logger.error(message)
        raise RuntimeError(message)
    elif hysdsio is None:
        hysdsio = get_hysds_io(es_hysdsio_url, rule["job_type"], logger=logger, hysds_io_type="_doc")

    # initialize job JSON
    job = {
        "queue": queue,
        "priority": rule["priority"],
        "type": hysdsio["job-specification"],
        "tags": json.dumps([rule["rule_name"], hysdsio["id"]]),
        "username": rule.get("username", get_username()),
    }
    rule["name"] = rule["rule_name"]
    #logger.info("job before get_params_for_submission: %s" % json.dumps(job, indent=2))

    # resolve parameters for job JSON
    if not isinstance(product, dict):
        logger.info("Products: %s" % product)
        params = get_params_for_products_set(
            hysdsio, json.loads(rule["kwargs"]), rule, product)
    else:
        logger.info("Product: %s" % product)
        params = get_params_for_submission(
            hysdsio, json.loads(rule["kwargs"]), rule, product)
    #logger.info("job after get_params_for_submission: %s" % json.dumps(job, indent=2))
    #logger.info("params from get_params_for_submission: %s" % json.dumps(params, indent=2))
    logger.info("params: %s" % json.dumps(params, indent=2))

    # set params
    job["params"] = json.dumps(params)
    #logger.info("job after adding params: %s" % json.dumps(job, indent=2))

    # set enable_dedup setting from hysdsio
    if 'enable_dedup' in hysdsio:
        job['enable_dedup'] = hysdsio['enable_dedup']

    return job


def resolve_hysds_job(job_type=None, queue=None, priority=None, tags=None,
                      params=None, job_name=None, payload_hash=None,
                      enable_dedup=True, username=None, soft_time_limit=None,
                      time_limit=None):
    '''
    Resolve HySDS job JSON.
    @param job_type - type of the job spec to go find
    @param queue - queue to submit to
    @param priority - priority of job
    @param tags - tags for this job
    @param params - stringified dictionary representing job params
    @param job_name - base job name override
    @param payload_hash - user-generated payload hash
    @param enable_dedup - flag to enable/disable job dedup
    @param username - username
    @param soft_time_limit - soft time limit for job execution
    @param time_limit - hard time limit for job execution
    '''

    # check args
    if job_type is None:
        raise RuntimeError("'type' must be supplied in request")
    if queue is None:
        raise RuntimeError("'queue' must be supplied in request")
    if params is None:
        params = {}
    elif isinstance(params, (str,)):
        params = json.loads(params)
    elif isinstance(params, dict):
        pass
    else:
        raise RuntimeError(
            "Invalid arg type 'params': {} {}".format(type(params), params))

    # pull mozart job and container specs
    specification = get_job_spec(app.conf['JOBS_ES_URL'], job_type)
    container_id = specification.get("container", None)
    container_spec = get_container(app.conf['JOBS_ES_URL'], container_id)
    logger.info("Running from: {0} in container: {1}".format(
        job_type, container_id))

    # resolve inputs/outputs
    positional = []
    context = {}
    localize_urls = specification.get("localize_urls", [])
    logger.info("params: {}".format(specification.get("params", [])))
    for param in specification.get("params", []):
        # TODO: change to "check_inputs"
        # match_inputs(param,context)
        logger.info("param: {}".format(param))
        if not param["name"] in params:
            raise RuntimeError(
                "'params' must specify '{0}' parameter".format(param["name"]))
        param["value"] = params[param["name"]]
        route_outputs(param, context, positional, localize_urls)

    # build command line
    cmd = get_command_line(specification.get("command", None), positional)

    # add docker value overlays
    overlays = specification.get("imported_worker_files", {})

    # get runtime options
    runtime_options = specification.get("runtime_options", {})

    # get hard/soft time limits and override if specified
    if time_limit is None:
        time_limit = specification.get('time_limit', None)
    if soft_time_limit is None:
        soft_time_limit = specification.get('soft_time_limit', None)

    # initialize hysds job JSON
    job = {
        "job_type": "job:{0}".format(job_type),
        "job_queue": queue,
        "container_image_url": container_spec.get("url", None),
        "container_image_name": container_spec.get("id", None),
        "container_mappings": overlays,
        "runtime_options": runtime_options,
        "time_limit": time_limit,
        "soft_time_limit": soft_time_limit,
        "enable_dedup": enable_dedup,
        "payload": {
            "_command": cmd,
            "localize_urls": localize_urls,
        }
    }

    # add optional parameters
    if job_name is not None:
        job["job_name"] = job_name
    if "disk_usage" in specification:
        job["payload"]["_disk_usage"] = specification.get("disk_usage")
    if priority is not None:
        job["priority"] = priority
    if payload_hash is not None:
        job["payload_hash"] = payload_hash
    if username is not None:
        job["username"] = username
    # deserialize tags, if needed
    if isinstance(tags, (str,)):
        tags = json.loads(tags)
    if tags is not None:
        job["tags"] = tags
    if tags is not None and len(tags) > 0:
        job["tag"] = tags[0]
    job["payload"]["job_specification"] = specification
    job["payload"]["container_specification"] = container_spec

    # Merge in context wihout overwrite
    for k, v in context.items():
        if not k in job["payload"]:
            job["payload"][k] = v

    # add tag to payload for propagation
    if "tag" in job and "tag" not in job["payload"]:
        job["payload"]["tag"] = job["tag"]

    return job


def submit_hysds_job(job):
    '''
    Submits a HySDS job via celery
    @param job_type - type of the job spec to go find
    @param queue - queue to submit to
    @param priority - priority of job
    @param tags - tags for this job
    @param params - dictionary representing job params
    '''

    logger.info("Submitting job:\n{0}".format(json.dumps(job, indent=2)))
    res = do_submit_job(job, 'jobs_processed')
    return res.id


def submit_mozart_job(product, rule, hysdsio=None, es_hysdsio_url=None,
                      queue=None, job_name=None, payload_hash=None,
                      enable_dedup=None, soft_time_limit=None,
                      time_limit=None):
    '''
    Resolve a Mozart job to a HySDS job and submit via celery
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param es_hysdsio_url - (optional) url to request hysdsio data from
    @param queue - (optional) job queue override
    @param job_name - (optional) base job name override
    @param payload_hash - (optional) user-generated payload hash
    @param enable_dedup - (optional) flag to enable/disable job dedup; if None resolve from hysdsio
    @param soft_time_limit - (optional) soft time limit for job execution
    @param time_limit - (optional) hard time limit for job execution
    '''

    # resolve mozart job
    moz_job = resolve_mozart_job(product, rule, hysdsio, es_hysdsio_url, queue)
    logger.info("resolved mozart job: {}".format(
        json.dumps(moz_job, indent=2)))

    # enable dedup; param overrides hysdsio
    dedup = moz_job.get(
        'enable_dedup', True) if enable_dedup is None else enable_dedup

    # resolve hysds job
    job = resolve_hysds_job(moz_job['type'], moz_job['queue'], moz_job['priority'],
                            moz_job['tags'], moz_job['params'], job_name, payload_hash,
                            dedup, moz_job['username'], soft_time_limit, time_limit)
    logger.info("resolved HySDS job: {}".format(json.dumps(job, indent=2)))

    # submit hysds job
    return submit_hysds_job(job)

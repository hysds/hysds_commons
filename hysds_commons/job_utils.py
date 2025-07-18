from future import standard_library
standard_library.install_aliases()

import os
import pwd
import json

from hysds_commons.log_utils import logger
from hysds.orchestrator import do_submit_job
from hysds.es_util import get_mozart_es

mozart_es = get_mozart_es()


def get_username():
    """Get username."""
    try:
        return pwd.getpwuid(os.getuid())[0]
    except:
        return None


def get_params_for_products_set(wiring, kwargs, passthrough=None, products=None):
    """
    Get parameters for parameters for  set of products
    @param wiring: wiring specification
    @param kwargs: key word arguments from submitter
    @param passthrough: rule containing passthrough arguments
    @param products: a list of products
    """
    params = {}
    if products is None:
        return params
    for prod in products:
        get_params_for_submission(
            wiring, kwargs, passthrough, prod, params, aggregate=True)
    return params


def get_hysds_io_params(job_spec):
    """
    returns a set of optional parameters from the hysds-io given the job_spec
    :param job_spec: str - name of job_spec
    :return: Dict[str, Dict], Set[Str]
    """
    hysdsio_params = dict()
    optional_params = set()
    hysdsio_name = job_spec.replace("job", "hysds-io", 1)

    hysds_io = mozart_es.get_by_id(index="hysds_ios-grq", id=hysdsio_name, ignore=[404])
    if hysds_io["found"] is False:
        hysds_io = mozart_es.get_by_id(index="hysds_ios-mozart", id=hysdsio_name, ignore=[404])
        if hysds_io["found"] is False:
            raise ValueError("hysds-ios not found: %s" % hysdsio_name)
    params = hysds_io["_source"]["params"]
    for param in params:
        param_name = param["name"]
        hysdsio_params[param_name] = param
        if param.get("optional", False) is True:
            optional_params.add(param_name)
    return hysdsio_params, optional_params


def get_params_for_submission(wiring, kwargs, passthrough=None, product=None, params=None, aggregate=False):
    """
    Get params for submission for HySDS/Tosca style workflow
    @param wiring - wiring specification
    @param kwargs - arguments from user form
    @param passthrough - rule
    @param product - product
    @param params - existing params
    """
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
    """
    Run a conversion from the input string to a known type
    @param wire: hysds-wiring record
    @param val: value to convert
    @returns: type converted value
    """
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
    """
    Runs the lambda key as a lambda function with 1 arg, the previous value
    @param wire - wiring spec to check for lambda
    @param val
    """

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
            raise RuntimeError("[ERROR] Failed to run lambda function to fill {}. {}:{}".format(
                wire["name"], type(e), e))
    return val


def get_inputs(param, kwargs, rule=None, product=None):
    """
    Update parameter to add in a value for the param
    @param param - parameter to update (param field in hysds-io)
    @param kwargs - job param inputs from user form
    @param rule - (optional) rule hit to use to fill pass through's
    @param product - (optional) product hit for augmenting
    """
    if "value" in param:  # Break out if value is known
        ret = param["value"]
        return ret

    source = param.get("from", "unknown")
    ret = param.get("default", None)  # Get a value

    if source == "submitter":
        ret = kwargs.get(param.get("name", "unknown"), ret)
    elif source == "passthrough" and not rule is None:
        ret = rule.get(param["name"], None)
    elif source.startswith("dataset_jpath:") and not product is None:
        # If we are processing a list of products, create a list for outputs
        ret = process_xpath(source.split(":")[1], product)

    # if the job param is optional but the value is not set (None/null)
    if param.get('optional', False) and ret is None:
        return ""

    # Check value is found
    if ret is None and not (product is None) and not (rule is None):
        raise RuntimeError("Failed to find '{}' input from '{}'".format(param.get("name", "unknown"), source))
    return ret


def process_xpath(xpath, trigger):
    """
    Process the xpath to extract data from a trigger
    @param xpath - xpath location in trigger
    @param trigger - trigger metadata to extract XPath
    """

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
    """
    Update parameter to add in a value for the param
    @param param - parameter to update
    @param context - context of job specification
    """
    if "value" in param:  # Break out if value is known
        return

    source = param.get("source", "unknown")
    param["value"] = param.get("default_value", None)  # Get a value

    if source == "submitter":
        param["value"] = context.get("inputs", {}).get(
            param.get("name", "unknown"), None)
    elif source == "context":
        param["value"] = context.get(param.get("name"), None)
    elif source.startswith("xpath."):
        param["value"] = process_xpath(source, context.get("trigger", {}))

    # Check value is found
    if param["value"] is None:
        raise RuntimeError("Failed to find '{}' input from '{}'".format(param.get("name", "unknown"), source))


def fill_localize(value, localize_urls):
    """
    Fill the localize product list. Must handle a value that is one of the following:
        1. A URL as a string, should be wrapped like {"url":stinrg}
        2. An object defining "url". Does not need wrapping
        3. Another list containing URLs/objects. Should be flattened
    Note: this function recurses to handle sub-lists
    @param value: a list conforming to points 1-3 above
    @param localize_urls: in-out list to append localize object to
           Note: this parameter is modified
    """
    # Check for string that need to be wrapped in localize format
    if isinstance(value, str):
        localize_urls.append({"url": value})
    # Check for objects that define the "url" field, and if so they can be passed right in
    elif not getattr(value, "get", None) is None and not value.get("url", None) is None:
        localize_urls.append(value)
    # Other types of objects must throw an error
    elif not getattr(value, "get", None) is None:
        raise ValueError(f"Invalid object of type {type(value)} trying to be localized. {value}")
    # Handle lists and other iterables by recursing
    else:
        for val in value:
            fill_localize(val, localize_urls)


def route_outputs(param, context, positional, localize_urls):
    """
    Route the input parameters to their destinations
    @param param - parameter specification
    @param context - context object to fill with context-destined items
    @param positional - list to append positional arguments to
    @param localize_urls - list to append localize urls to
    """

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
    """
    Gets the command line invocation for this submission by
    concatenating the command, and positional arguments
    @param command - command line from job spec
    @param positional - positional arguments
    @return: command strin with args, or None
    """

    parts = []
    if not command is None:
        parts.append(command)
    for posit in positional:
        # Escape any single quotes
        if not isinstance(posit, str):
            posit = f"{json.dumps(posit)}"
        posit = posit.replace("'", "'\"'\"'")
        # Add in encapsulating single quotes
        parts.append(f"'{posit}'")
    ret = " ".join(parts)
    return ret


def resolve_mozart_job(product, rule, hysdsio=None, queue=None, component=None):
    """
    Resolve Mozart job JSON.
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param component - tosca/grq or mozart/figaro, retrieve hysds_io from ES index (hysds_ios-mozart vs hysds_ios-grq)
    @param queue - (optional) job queue override
    """

    logger.info("rule: %s" % json.dumps(rule, indent=2))
    logger.info("hysdsio: %s" % json.dumps(hysdsio, indent=2))
    logger.info("component: %s" % component)
    logger.info("queue: %s" % queue)

    queue = rule['queue'] if queue is None else queue  # override queue

    if rule['priority'] is not None:  # ensure priority is int
        rule["priority"] = int(rule['priority'])

    # this is the common data for all jobs, and will be copied for each individual submission
    if hysdsio is None and component is None:
        message = "[ERROR] Must supply a hysds-io object or a ES-URL to request one"
        logger.error(message)
        raise RuntimeError(message)
    elif hysdsio is None:
        if component.lower() in ('tosca', 'grq'):
            hysds_io_index = 'hysds_ios-grq'
        else:
            hysds_io_index = 'hysds_ios-mozart'
        hysdsio = mozart_es.get_by_id(index=hysds_io_index, id=rule["job_type"])
        hysdsio = hysdsio['_source']

    # initialize job JSON
    job = {
        "queue": queue,
        "priority": rule["priority"],
        "soft_time_limit": rule.get("soft_time_limit", None),
        "time_limit": rule.get("time_limit", None),
        "disk_usage": rule.get("disk_usage", None),
        "type": hysdsio["job-specification"],
        "tags": json.dumps([rule["rule_name"], hysdsio["id"]]),
        "username": rule.get("username", get_username()),
        "enable_dedup": rule.get('enable_dedup', True)
    }
    rule["name"] = rule["rule_name"]

    # resolve parameters for job JSON
    if not isinstance(product, dict):
        logger.info("Products: %s" % product)
        params = get_params_for_products_set(hysdsio, json.loads(rule["kwargs"]), rule, product)
    else:
        logger.info("Product: %s" % product)
        params = get_params_for_submission(hysdsio, json.loads(rule["kwargs"]), rule, product)

    logger.info("params: %s" % json.dumps(params, indent=2))

    job["params"] = json.dumps(params)  # set params
    return job


def resolve_hysds_job(job_type=None, queue=None, priority=None, tags=None, params=None, job_name=None,
                      payload_hash=None, enable_dedup=True, username=None, soft_time_limit=None, time_limit=None,
                      disk_usage=None, publish_overwrite_ok=False):
    """
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
    @param disk_usage - disk usage for PGE (KB, MB, GB, etc)
    @param publish_overwrite_ok - enable a job to overwrite an existing dataset on publish
    """
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
        raise RuntimeError(f"Invalid arg type 'params': {type(params)} {params}")

    # pull mozart job and container specs
    specification = mozart_es.get_by_id(index='job_specs', id=job_type)
    specification = specification['_source']

    # optional parameters from hysds-io
    hysdsio_params, optional_params = get_hysds_io_params(job_type)

    container_id = specification.get("container", None)
    container_spec = mozart_es.get_by_id(index='containers', id=container_id)
    container_spec = container_spec['_source']
    logger.info(f"Running from: {job_type} in container: {container_id}")

    # resolve inputs/outputs
    positional = []
    context = {}
    localize_urls = specification.get("localize_urls", [])
    logger.info("job_spec params: {}".format(specification.get("params", [])))

    for param in specification.get("params", []):
        # TODO: change to "check_inputs"
        # match_inputs(param,context)
        logger.info(f"param: {param}")
        if param["name"] not in params:
            default_value = hysdsio_params.get(param["name"], {}).get("default", None)
            if default_value is not None:
                logger.warning("{} not found, using default value {}".format(param["name"], default_value))
                param["value"] = run_type_conversion(hysdsio_params[param["name"]], default_value)
            else:
                if param["name"] in optional_params:
                    logger.warning("{} is not supplied but optional, skipping...".format(param["name"]))
                    continue
                else:
                    raise RuntimeError("params must specify '{}' parameter".format(param["name"]))
        else:
            param["value"] = params[param["name"]]
        route_outputs(param, context, positional, localize_urls)

    cmd = get_command_line(specification.get("command", None), positional)  # build command line

    overlays = specification.get("imported_worker_files", {})  # add docker value overlays

    runtime_options = specification.get("runtime_options", {})  # get runtime options

    # get hard/soft time limits and override if specified
    if time_limit is None:
        time_limit = specification.get('time_limit', None)
    if soft_time_limit is None:
        soft_time_limit = specification.get('soft_time_limit', None)
    if disk_usage is None:
        disk_usage = specification.get('disk_usage', None)

    # initialize hysds job JSON
    job = {
        "job_type": f"job:{job_type}",
        "job_queue": queue,
        "container_image_url": container_spec.get("url", None),
        "container_image_name": container_spec.get("id", None),
        "container_mappings": overlays,
        "runtime_options": runtime_options,
        "time_limit": time_limit,
        "soft_time_limit": soft_time_limit,
        "disk_usage": disk_usage,
        "enable_dedup": enable_dedup,
        "publish_overwrite_ok": publish_overwrite_ok,
        "payload": {
            "_command": cmd,
            "localize_urls": localize_urls,
        }
    }

    # add optional parameters
    job["payload"]["_disk_usage"] = disk_usage
    if job_name is not None:
        job["job_name"] = job_name
    if priority is not None:
        job["priority"] = priority
    if payload_hash is not None:
        job["payload_hash"] = payload_hash
    if username is not None:
        job["username"] = username
    if isinstance(tags, (str,)):  # deserialize tags, if needed
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
    """
    Submits a HySDS job via celery
    # @param job_type - type of the job spec to go find
    # @param queue - queue to submit to
    # @param priority - priority of job
    # @param tags - tags for this job
    # @param params - dictionary representing job params
    """
    queue = 'jobs_processed'
    logger.info(f"Submitting job:\n{json.dumps(job, indent=2)}")

    res = do_submit_job(job, queue)
    logger.info("submitted job to queue: %s" % queue)

    return res.id


def submit_mozart_job(product, rule, hysdsio=None, queue=None, job_name=None, payload_hash=None, enable_dedup=None,
                      soft_time_limit=None, time_limit=None, component=None):
    """
    Resolve a Mozart job to a HySDS job and submit via celery
    @param product - product result body
    @param rule - rule specification body
    @param hysdsio - (optional) hysds-io body
    @param queue - (optional) job queue override
    @param job_name - (optional) base job name override
    @param payload_hash - (optional) user-generated payload hash
    @param enable_dedup - (optional) flag to enable/disable job dedup; if None resolve from hysdsio
    @param soft_time_limit - (optional) soft time limit for job execution
    @param time_limit - (optional) hard time limit for job execution
    @param component - tosca/grq or mozart/figaro, retrieve hysds_io from ES index (hysds_ios-mozart vs hysds_ios-grq)
    """

    # raise if using deprecated keywords; TODO: remove in future release
    if soft_time_limit is not None or time_limit is not None:
        raise RuntimeError("(soft_)time_limit is no longer supported. Override these values by passing them in the"
                           "`rule` param")
    if enable_dedup is not None:
        raise RuntimeError("enable_dedup is no longer supported. Override these values by passing them in the `rule`"
                           "param")

    # resolve mozart job
    moz_job = resolve_mozart_job(product, rule, hysdsio, queue, component=component)
    logger.info(f"resolved mozart job: {json.dumps(moz_job)}")

    # resolve hysds job
    job = resolve_hysds_job(job_type=moz_job['type'],
                            queue=moz_job['queue'],
                            priority=moz_job['priority'],
                            tags=moz_job['tags'],
                            params=moz_job['params'],
                            job_name=job_name,
                            payload_hash=payload_hash,
                            enable_dedup=moz_job['enable_dedup'],
                            username=moz_job['username'],
                            soft_time_limit=moz_job['soft_time_limit'], time_limit=moz_job['time_limit'],
                            disk_usage=moz_job['disk_usage'])
    logger.info(f"resolved HySDS job: {json.dumps(job, indent=2)}")

    # submit hysds job
    return submit_hysds_job(job)

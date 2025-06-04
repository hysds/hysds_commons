#!/usr/bin/env python

import os.path
import re
import sys
import json
import jsonschema
import inspect
import types


# A global fail flag, used to fail the program on leathal errors
fail = False

SCHEMAS_DIR_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "../schemas")
HYSDS_IO_SCHEMA_FILE = os.path.join(SCHEMAS_DIR_PATH, 'hysds-io-schema.json')
JOB_SPEC_SCHEMA_FILE = os.path.join(SCHEMAS_DIR_PATH, 'job-spec-schema.json')


def check_true(boolean, lethal, message):
    """
    Check if boolean is true, and print message if not true
    @param boolean: result of condition
    @param lethal: should this cause an error and fail the program
    @message: message to print
    """
    # Note: using global to fail program
    global fail
    if boolean:
        return
    printable = "[WARNING] {0}"
    if lethal:
        printable = "[ERROR] {0}"
        fail = True
    print(printable.format(message, file=sys.stderr))


def check_paired_args(prefix, spec, io):
    """
    Check that the job-spec and hysds-io define the same parameters
    @param prefix - prefix
    @param spec - job-spec
    @param io - hysds-io
    """
    spec_names = [param.get("name", None) for param in spec.get("params", [])]
    io_names = [param.get("name", None) for param in io.get("params", [])]

    for name in spec_names:
        check_true(name in io_names,
                   True,
                   "{} defines job-spec parameter without match in "
                   "hysds-io: {}".format(prefix, name))
    for name in io_names:
        check_true(name in spec_names,
                   True,
                   "{} defines hysds-io parameter without match in "
                   "job-spec: {}".format(prefix, name))


def check_lambdas(prefix, io):
    """
    Check that the lambdas are well formed in the hysds-io file
    @param prefix - prefix
    @param io - io object
    """
    params = io.get("params", [])
    for param in params:
        name = param.get("name", "no-name")

        if "lambda" in param:
            try:
                import functools
                import hysds_commons.lambda_builtins
                namespace = {"functools": functools}
                for nm in dir(hysds_commons.lambda_builtins):
                    if nm.startswith("__"):
                        continue
                    namespace[nm] = hysds_commons.lambda_builtins.__dict__[nm]
                fn = eval(param["lambda"], namespace, {})
                check_true(isinstance(fn, types.LambdaType),
                           True,
                           "{} defines hysds-io lambda modifier for "
                           "parameter, {}, which does not equate to a lambda "
                           "function".format(prefix, name))
                argspec = inspect.getfullargspec(fn)
                check_true(len(argspec[0]) == 1,
                           True,
                           "{} defines hysds-io lambda modifer for "
                           "parameter, {}, which does not except exactly "
                           "1 argument".format(prefix, name))
                try:
                    fn("Some test value")
                except NameError as ne:
                    if "global name" in str(ne) and \
                            "is not defined" in str(ne):
                        raise
                except Exception as err:
                    pass
            except Exception as e:
                check_true(False,
                           True,
                           "{} defines hysds-io lambda modifier for "
                           "parameter, {}, which errors on compile. "
                           "{}:{}".format(
                               prefix, name, str(type(e)), e))


def validate(name, instance, schema):
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
    for error in errors:
        message = "name: {}, location: {}, message: {}".format(
            name, error.absolute_path, error.message)
        check_true(False, True, message)


def pair(jsons):
    """
    Pair the JSONS to the base name
    @param jsons - jsons dictionary
    @return: list of dictioaries
    """
    objects = {}
    reg = re.compile("^.*/(.*)\\.json\\.?(.*)$")
    for k, v in list(jsons.items()):
        match = reg.match(k)
        name = match.group(2)
        f_type = match.group(1)
        if name not in objects:
            objects[name] = {}
        objects[name][f_type] = v
    for k, v in list(objects.items()):
        for f_type in ["hysds-io", "job-spec"]:
            check_true(f_type in v, False,
                       f"{k} does not define a {f_type}.json")
    return objects


def json_formatted(files):
    """
    Check JSON formatting
    @param files - files to loop through
    @return: name to JSON dict
    """
    jsons = {}
    for fle in files:
        try:
            with open(fle) as fp:
                jsons[fle] = json.load(fp)
        except Exception as e:
            extra = "" if "No JSON object could be decoded" in str(e) \
                else "Probable a malformed literal. Fractional numbers must " \
                     "start with 0. "
            check_true(False, True,
                       "Failed to validate JSON in: {0}. {3}{1}:{2}".format(
                           fle, type(e), str(e), extra))
    return jsons


if __name__ == "__main__":
    """Main functions"""
    if len(sys.argv) != 2:
        print(f"Usage:\n\t{sys.argv[0]} <directory>", file=sys.stderr)
        sys.exit(-1)
    directory = sys.argv[1]
    try:
        files = [os.path.join(directory, fle) for fle in os.listdir(directory)
                 if os.path.isfile(os.path.join(directory, fle))
                 and ".json" in fle]
    except Exception as err:
        files = []
    if len(files) == 0:
        print("[ERROR] No files found in directory: {}".format(
            directory), file=sys.stderr)
        sys.exit(1)
    jsons = json_formatted(files)
    pairs = pair(jsons)
    # Parse the schemas
    with open(HYSDS_IO_SCHEMA_FILE) as f:
        io_schema = json.load(f)
    with open(JOB_SPEC_SCHEMA_FILE) as f:
        spec_schema = json.load(f)

    for k, v in list(pairs.items()):
        if "job-spec" in v:
            print(f"Validating job-spec for {k}")
            validate(k, v['job-spec'], spec_schema)
        if "hysds-io" in v:
            print(f"Validating hysds-io for {k}")
            validate(k, v['hysds-io'], io_schema)
            check_lambdas(k, v['hysds-io'])
        if "job-spec" in v and "hysds-io" in v:
            print("Checking that the job-spec and hysds-io define the "
                  "same parameters")
            check_paired_args(k, v["job-spec"], v["hysds-io"])
    if fail:
        sys.exit(1)
    else:
        print("All hysds-io and job-specs are valid")
        sys.exit(0)

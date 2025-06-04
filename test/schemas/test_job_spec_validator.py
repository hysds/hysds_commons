import jsonschema
import os
import json
import logging

schema_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           '../../schemas/job-spec-schema.json')
with open(schema_file) as f:
    schema_data = json.load(f)


def __validate(file):
    with open(file) as f:
        data = json.load(f)
    validator = jsonschema.Draft7Validator(schema_data)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    return errors


def test_valid():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/job-spec.json.valid')
    errors = __validate(test_file)
    assert len(errors) == 0


def test_value():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/job-spec.json.value')
    errors = __validate(test_file)
    assert len(errors) == 6
    assert "does not match" in errors[0].message
    assert "is not one of" in errors[1].message
    assert "required" in errors[2].message
    assert "is not of type 'array'" in errors[3].message
    assert "is not of type 'integer'" in errors[4].message
    assert "is not of type 'integer'" in errors[5].message


def test_missing():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/job-spec.json.missing')
    errors = __validate(test_file)
    assert len(errors) == 3
    assert "'disk_usage' is a required property" in errors[0].message
    assert "'soft_time_limit' is a required property" in errors[1].message
    assert "'time_limit' is a required property" in errors[2].message


def test_gpus():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/job-spec.json.gpus')
    errors = __validate(test_file)
    logging.info(errors)
    assert len(errors) == 0


def test_gpus_dependency_image():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/job-spec.json.gpus-dep_img')
    errors = __validate(test_file)
    logging.info(errors)
    assert len(errors) == 0

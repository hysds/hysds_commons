import jsonschema
import os
import json

schema_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                           '../../schemas/hysds-io-schema.json')
with open(schema_file, 'r') as f:
    schema_data = json.load(f)


def __validate(file):
    with open(file, 'r') as f:
        data = json.load(f)
    validator = jsonschema.Draft7Validator(schema_data)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    return errors


def test_valid():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/hysds-io.json.valid')
    errors = __validate(test_file)
    assert len(errors) == 0


def test_value():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/hysds-io.json.value')
    errors = __validate(test_file)
    assert len(errors) == 3
    assert "does not match" in errors[0].message
    assert "does not match" in errors[1].message
    assert "not of type \'string\'" in errors[2].message


def test_conditional_required():
    test_file = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             'test-files/hysds-io.json.conditional_required')
    errors = __validate(test_file)
    assert len(errors) == 3
    assert errors[0].message == "'value' is a required property"
    assert errors[1].message == "'enumerables' is a required property"
    assert errors[2].message == "'version_regex' is a required property"


test_valid()
test_value()
test_conditional_required()
#!/usr/bin/env python
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from builtins import open
from future import standard_library
standard_library.install_aliases()
import os
import sys
import json
import requests
from jinja2 import Template

from hysds.celery import app


def write_template(es_url, index, tmpl_file):
    """Write template to ES."""

    with open(tmpl_file) as f:
        tmpl = Template(f.read()).render(index=index)
    tmpl_url = "%s/_template/%s" % (es_url, index)
    r = requests.delete(tmpl_url)
    r = requests.put(tmpl_url, data=tmpl)
    r.raise_for_status()
    print(r.json())
    print("Successfully installed template %s at %s." % (index, tmpl_url))


if __name__ == "__main__":
    node = sys.argv[1]
    if node == "mozart":
        es_url = app.conf['JOBS_ES_URL']
        indices = ["containers", "job_specs", "hysds_ios"]
    elif node == "grq":
        es_url = app.conf['GRQ_ES_URL']
        indices = ["hysds_ios"]
    else:
        raise RuntimeError("Invalid node: %s" % node)
    tmpl_file = os.path.normpath(os.path.abspath(os.path.join(
        os.path.dirname(__file__), '..', 'config', 'es_template.json'
    )))
    for index in indices:
        write_template(es_url, index, tmpl_file)

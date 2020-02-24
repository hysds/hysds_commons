#!/bin/env python
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from builtins import str
from future import standard_library
standard_library.install_aliases()

from hysds.celery import app
from .elasticsearch_utils import ElasticsearchUtility


"""
because of cyclical dependency issues, i cannot initialize mozart_es and grq_es in "hysds" core repo
hysds_commons is already importing hysds.celery

so i am initializing it here to avoid the ImportError

raised unexpected: ImportError("cannot import name 'mozart_es' from 'hysds':
(/export/home/hysdsops/verdi/ops/hysds/hysds/__init__.py)")
"""

mozart_es = ElasticsearchUtility(app.conf['JOBS_ES_URL'])
grq_es = ElasticsearchUtility(app.conf['GRQ_ES_URL'])
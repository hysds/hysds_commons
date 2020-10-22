from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()

import logging


log_format = "[%(asctime)s: %(levelname)s %(filename)s:%(funcName)s] %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)
logger = logging.getLogger('hysds_commons')


def get_logger():
    return logger

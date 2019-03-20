from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()
import os
import sys

import hysds_commons.linux_utils


def get_container_host_ip():
    """Return the IP address of the container host if caller is running in a
       container. Otherwise, returns the default localhost IP address."""

    if sys.platform.startswith('linux'):
        return hysds_commons.linux_utils.get_container_host_ip()
    else:
        raise NotImplementedError("Platform %s not supported." % sys.platform)

import os, sys

import hysds_commons.linux_utils


def get_container_host_ip():
    """Return the IP address of the container host if caller is running in a
       container. Otherwise, returns the default localhost IP address."""

    if sys.platform.startswith('linux'):
        return hysds_commons.linux_utils.get_container_host_ip()
    else:
        raise NotImplementedError("Platform %s not supported." % sys.platform)

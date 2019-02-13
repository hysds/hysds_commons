import re
from subprocess import check_output, CalledProcessError


# compiled regexes
DEF_GATEWAY_RE = re.compile(r'^default\s+via\s+(.+)\s+dev')
DOCKER_RE = re.compile(r'docker')


def running_in_container():
    """Return True if caller is running in a container instance. False otherwise."""

    with open("/proc/1/cgroup") as f:
        cgroup = f.read()
    return True if DOCKER_RE.search(cgroup) else False


def get_gateway_ip():
    """Return IP address of default gateway."""

    out = check_output(["ip", "route", "show", "default", "0.0.0.0/0"])
    match = DEF_GATEWAY_RE.search(out)
    if not match:
        raise RuntimeError(
            "Failed to extract default gateway from ip route: %s" % out)
    return match.group(1)


def get_container_host_ip():
    """Return the IP address of the container host if caller is running in a
       container. Otherwise, returns the default localhost IP address."""

    return "127.0.0.1" if not running_in_container() else get_gateway_ip()

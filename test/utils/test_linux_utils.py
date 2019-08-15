import sys
sys.path.append('.')
from hysds_commons.linux_utils import *


def test_get_gateway_ip():
    host_ip = get_container_host_ip()
    print(host_ip)

    gateway_ip = get_gateway_ip()
    print(gateway_ip)

    in_container = running_in_container()
    print(in_container)

    ip_routes_output = check_output(["ip", "route", "show", "default", "0.0.0.0/0"])
    assert type(ip_routes_output) == bytes

    ip_routes_output_str = ip_routes_output.decode('utf-8')
    assert type(ip_routes_output_str) == str

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from future import standard_library
standard_library.install_aliases()


def check_passthrough_query(params):
    """
    returns True if params is:
    {
        "from": "passthrough",
        "name": "query"
    }
    """
    for param in params:
        if param['from'] == 'passthrough' and param['name'] == 'query':
            return True
    return False

from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import
from setuptools import setup, find_packages
import hysds_commons

setup(
    name='hysds_commons',
    version=hysds_commons.__version__,
    long_description=hysds_commons.__description__,
    url=hysds_commons.__url__,
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
    install_requires=[
        'requests>=2.7.0', 'future>=0.17.1', "jsonschema>=3.0.1", "requests_aws4auth=0.9"
    ]
)

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
        'requests>=2.7.0'
    ]
)

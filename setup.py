from setuptools import setup, find_packages
import hysds_commons


def readme():
    with open("README.md") as f:
        return f.read()


setup(
    name='hysds_commons',
    version=hysds_commons.__version__,
    description=hysds_commons.__description__,
    long_description_content_type="text/markdown",
    long_description=readme(),
    url=hysds_commons.__url__,
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
    python_requires='>=3.12',
    install_requires=[
        'elasticsearch>=7.0.0,<7.14.0',
        # Pin numpy due to ES incompatability: https://github.com/elastic/elasticsearch-py/issues/2646
        'numpy<2.0.0',
        'opensearch-py>=2.3.0,<3.0.0',
        'requests>=2.7.0',
        'future>=0.17.1',
        "jsonschema>=3.0.1",
        "shapely>=1.8.2"
    ],
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.12",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Unix",
        "Operating System :: MacOS :: MacOS X",
    ],
)

"""Test packaging configuration and metadata."""
import sys
from importlib.metadata import version, requires

import pytest


def test_version_starts_with_7():
    """Verify package version starts with 7."""
    v = version("hysds-commons")
    assert v.startswith("7."), f"Expected version 7.x, got {v}"


def test_future_not_a_dependency():
    """Verify 'future' package is not a dependency."""
    deps = requires("hysds-commons")
    assert deps is not None
    
    for dep in deps:
        dep_name = dep.split()[0].split(";")[0].split(">=")[0].split("~=")[0].split("<")[0]
        assert dep_name != "future", "'future' should not be a dependency on Python 3.12+"


def test_core_modules_importable():
    """Verify core hysds_commons modules can be imported."""
    import hysds_commons
    assert hasattr(hysds_commons, "__version__")
    assert hasattr(hysds_commons, "__description__")
    assert hasattr(hysds_commons, "__url__")


def test_python_version_requirement():
    """Verify running on Python 3.12+."""
    assert sys.version_info >= (3, 12), "Requires Python 3.12+"


def test_package_name_is_hysds_commons():
    """Verify package is published as hysds-commons."""
    v = version("hysds-commons")
    assert v is not None, "Package 'hysds-commons' not found"


def test_import_name_is_hysds_commons():
    """Verify import name remains 'hysds_commons' (not hysds_commons)."""
    import hysds_commons
    assert hysds_commons.__name__ == "hysds_commons"


def test_numpy_has_upper_bound():
    """Verify numpy has <2.0.0 upper bound."""
    deps = requires("hysds-commons")
    assert deps is not None
    
    numpy_deps = [d for d in deps if d.startswith("numpy")]
    assert numpy_deps, "numpy dependency not found"
    
    for dep in numpy_deps:
        assert "<2.0" in dep or "<2" in dep, \
            f"numpy should have <2.0.0 upper bound, found: {dep}"

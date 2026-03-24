# HySDS Commons Packaging Migration Summary

## Migration Completed: March 24, 2026

This document summarizes the migration of the `hysds_commons` repository from legacy `setup.py` to modern `pyproject.toml` packaging.

---

## Changes Made

### ✅ Files Created

1. **`pyproject.toml`** - Modern packaging configuration
   - Package name: `hysds-commons` (PyPI) / `hysds_commons` (import)
   - Version: Dynamic from git tags via `hatch-vcs`
   - Dependencies: 6 third-party packages

2. **`.github/workflows/publish.yml`** - PyPI publishing automation
   - Triggered on git tags (`v*`)
   - Uses PyPI Trusted Publishers (OIDC)

### ✅ Files Modified

1. **`hysds_commons/__init__.py`**
   - Changed from hardcoded version to `version("hysds-commons")`
   - Updated URL from internal JPL GitHub to public GitHub

2. **`setup.py`**
   - Replaced with minimal shim for backward compatibility
   - Delegates all configuration to `pyproject.toml`
   - Will be removed in v7.1.0+

---

## Key Dependency Changes

### Fixed Issues

| Issue | Before | After |
|-------|--------|-------|
| future dependency | `future>=0.17.1` | Removed |

### Dependencies Preserved Exactly

All 6 core dependencies maintained with exact pins from original `setup.py`:
- `elasticsearch>=7.0.0,<7.14.0`
- `numpy<2.0.0` (already had upper bound)
- `opensearch-py>=2.3.0,<3.0.0`
- `requests>=2.7.0`
- `jsonschema>=3.0.1`
- `shapely>=1.8.2`

---

## Build Verification

```bash
$ python -m build
Successfully built hysds_commons-2.2.0.post1.dev0+ge476695ba.d20260324.tar.gz
Successfully built hysds_commons-2.2.0.post1.dev0+ge476695ba.d20260324-py3-none-any.whl
```

---

## Next Steps

### Before Publishing to PyPI

1. **Tag version 7.0.0**
   ```bash
   git tag -a v7.0.0 -m "Release 7.0.0 - Modern packaging migration"
   git push origin v7.0.0
   ```

2. **Configure PyPI Trusted Publisher**
   - Go to https://pypi.org/manage/account/publishing/
   - Add GitHub Actions publisher for `hysds/hysds_commons` repo
   - Workflow: `publish.yml`
   - Environment: `pypi`

### Installation Methods

#### Development (Local)
```bash
pip install -e .
```

#### Development (From Git Branch)
```bash
pip install "git+https://github.com/hysds/hysds_commons.git@feature-branch"
```

#### Production (After PyPI Publishing)
```bash
pip install hysds-commons

# Or as dependency of other packages
pip install hysds-core  # Includes hysds-commons~=7.0
```

---

## Backward Compatibility

### Import Names (Unchanged)
```python
# All existing imports continue to work
import hysds_commons
from hysds_commons.elasticsearch_utils import ElasticsearchUtility
```

### Package Name Change
- **PyPI package**: `hysds_commons` → `hysds-commons`
- **Import name**: `hysds_commons` (unchanged)

---

## Migration Checklist

- [x] Create `pyproject.toml` with all dependencies
- [x] Remove `future` from dependencies
- [x] Preserve all other dependency pins exactly
- [x] Update `hysds_commons/__init__.py` for dynamic versioning
- [x] Update GitHub URL from internal to public
- [x] Add GitHub Actions workflow for PyPI publishing
- [x] Keep minimal `setup.py` shim for backward compatibility
- [x] Verify `python -m build` succeeds
- [ ] Tag v7.0.0 release
- [ ] Configure PyPI Trusted Publisher
- [ ] Publish to PyPI

---

## Contact

For questions about this migration, contact the HySDS team at hysds-help@jpl.nasa.gov

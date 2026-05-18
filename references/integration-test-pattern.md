# Bridge Integration-Test Pattern

Any project that depends on Chrome Bridge wants a test suite that:

- runs locally when the Bridge server + extension are up,
- runs in CI (where there's no browser) without failing,
- doesn't make every developer remember a `--skip` flag.

The pattern below auto-detects Bridge availability and skips the
relevant tests.

## Components

### 1. `pytest` marker (`pyproject.toml`)

```toml
[tool.pytest.ini_options]
markers = [
    "integration: tests that require Chrome Bridge + a logged-in browser",
]
pythonpath = [
    "scripts",
    # ...other paths your project needs
]
```

### 2. `conftest.py` — auto-skip

```python
from functools import lru_cache

import pytest


@lru_cache(maxsize=1)
def _bridge_available() -> bool:
    """Probe Chrome Bridge once per test session."""
    try:
        from bridge_client import BridgePage
        return BridgePage().is_extension_connected()
    except Exception:
        return False


def pytest_runtest_setup(item):
    """Skip integration tests when Bridge isn't reachable."""
    if (
        item.get_closest_marker("integration") is not None
        and not _bridge_available()
    ):
        pytest.skip("Chrome Bridge unavailable (no local browser / extension)")
```

### 3. Marked tests

```python
import pytest

from bridge_client import BridgePage


@pytest.mark.integration
class TestSomeFeature:
    @pytest.fixture(scope="module")
    def page(self):
        return BridgePage()

    def test_navigate(self, page):
        page.navigate("https://example.com")
        # ...
```

## Running

```bash
# CI — skip every integration test
uv run pytest tests/ -m "not integration"

# Local, Bridge online — run everything
uv run pytest tests/

# Local, Bridge offline — same command, integration tests auto-skip
uv run pytest tests/

# Only integration tests
uv run pytest tests/ -m integration
```

## Details that matter

- Use `item.get_closest_marker("integration")`, **not**
  `"integration" in item.keywords` — the latter false-matches any test
  whose *name* contains the word "integration".
- `@lru_cache(maxsize=1)` — instantiating `BridgePage` and probing the
  WebSocket isn't free; run it once per session.
- `scope="module"` on the `page` fixture — reuse one `BridgePage` across
  tests in the same file to avoid re-navigation churn.
- Keep `sys.path.insert` *out* of `_bridge_available()` — put the import
  path in `pyproject.toml`'s `pythonpath` so collection ordering doesn't
  break the probe.

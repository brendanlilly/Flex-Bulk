import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# tests never read or write the dashboard's saved settings or the live analytics log
import os  # noqa: E402
import tempfile  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="webb-tests-"))
os.environ["WEBB_MERCH_DIR"] = str(_TMP / "merch")
os.environ["WEBB_ANALYTICS_DB"] = str(_TMP / "analytics.sqlite")
os.environ["WEBB_EC_DIR"] = str(_TMP / "extended")       # a small Extended Catalog build for the tests (v2.3)
os.environ.pop("WEBB_EC_AUTOBUILD", None)
EC_TEST_ROWS = 30_000


@pytest.fixture(scope="session")
def engine():
    from webbsearch.config import settings
    if not (settings.index_dir / "products").exists():
        pytest.skip("not built yet; run python -m flexbulk.build")
    pass  # Flex Bulk: no Extended Catalog
    from webbsearch.llm.client import ClaudeClient
    from webbsearch.retrieval.service import Engine
    return Engine(claude=ClaudeClient(api_key=None), log=lambda *_: None)


def ec_build():
    """Build the tests' Extended Catalog once per session (about 10 s)."""
    from webbsearch.config import settings
    root = _TMP / "extended"
    if (root / "CURRENT").exists():
        return root
    from webbsearch.extended.build import main
    main(["--n", str(EC_TEST_ROWS), "--out", str(root / "builds" / "test"), "--shards", "1",
          "--canonical", str(settings.canonical_dir)])
    return root

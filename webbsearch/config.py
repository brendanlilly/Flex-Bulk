"""Central configuration. Everything overridable by env var so the same code runs
locally (embedded engine, synthetic data) and in production (OpenSearch, Snowflake)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(_env("WEBB_DATA_DIR", str(ROOT / "data")))
    # Raw source extracts (synthetic generator writes ERP-shaped files here;
    # real extracts drop in here and are mapped by config/source_mapping.yaml).
    extracts_dir: Path = Path(_env("WEBB_EXTRACTS_DIR", str(Path(_env("WEBB_DATA_DIR", str(ROOT / "data"))) / "extracts")))
    mapping_file: Path = Path(_env("WEBB_MAPPING_FILE", str(ROOT / "config" / "source_mapping.yaml")))

    # Serving backend: "local" (Tantivy + HNSW, embedded) or "opensearch".
    backend: str = _env("WEBB_BACKEND", "local")
    opensearch_url: str = _env("WEBB_OPENSEARCH_URL", "http://localhost:9200")
    opensearch_prefix: str = _env("WEBB_OPENSEARCH_PREFIX", "webb")

    # Feature store: "duckdb" (local parquet) or "snowflake".
    feature_store: str = _env("WEBB_FEATURE_STORE", "duckdb")

    # Claude
    anthropic_api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    model_fast: str = _env("WEBB_CLAUDE_FAST", "claude-haiku-4-5-20251001")   # QU, rerank
    model_smart: str = _env("WEBB_CLAUDE_SMART", "claude-sonnet-5")            # assistant, enrichment QA
    qu_timeout_s: float = float(_env("WEBB_QU_TIMEOUT", "0.6"))
    rerank_timeout_s: float = float(_env("WEBB_RERANK_TIMEOUT", "1.5"))
    assistant_timeout_s: float = float(_env("WEBB_ASSISTANT_TIMEOUT", "20"))
    llm_disabled: bool = _env("WEBB_LLM_DISABLED", "0") == "1"

    # Retrieval
    candidates_lexical: int = 300
    candidates_dense: int = 150
    candidates_rerank: int = 300   # LTR rescoring window
    llm_rerank_window: int = 20
    rrf_k: int = 60

    # Data scale (synthetic)
    n_products: int = int(_env("WEBB_N_PRODUCTS", "750000"))
    n_accounts: int = int(_env("WEBB_N_ACCOUNTS", "4000"))
    seed: int = 7

    @property
    def canonical_dir(self) -> Path:
        return self.data_dir / "canonical"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def features_dir(self) -> Path:
        return self.data_dir / "features"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key) and not self.llm_disabled


settings = Settings()

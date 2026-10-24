"""Portfolio data layer: schema, dataset, and dialect-aware database access."""
from .database import (
    Database,
    DatabaseError,
    DatabricksDatabase,
    DatabricksSettings,
    DuckDBDatabase,
    IntegrityError,
    open_database,
)
from .app_repository import (
    ApprovalRepository,
    BaselineRepository,
    IntakeSessionRepository,
    RecoveryScenarioRepository,
    TrackSnapshotRepository,
)
from .connection import Connection, connect, default_database_path, resolve_dialect
from .paths import DATA_ROOT, DATASET_DIR, SAMPLES_DIR
from .repository import HistoryRepository, dataset_loaded, load_dataset
from .schema import ALL_TABLES, APP_TABLES, TABLE_PREFIX, TABLES, render_ddl

__all__ = [
    "ApprovalRepository", "BaselineRepository", "Connection", "IntakeSessionRepository", "RecoveryScenarioRepository",
    "TrackSnapshotRepository", "ALL_TABLES", "APP_TABLES", "DATA_ROOT", "DATASET_DIR", "Database", "DatabaseError",
    "DatabricksDatabase", "DatabricksSettings", "DuckDBDatabase", "HistoryRepository", "IntegrityError",
    "SAMPLES_DIR", "TABLE_PREFIX", "TABLES", "connect", "dataset_loaded", "default_database_path",
    "load_dataset", "open_database", "render_ddl", "resolve_dialect",
]

"""Portfolio data layer: schema, dataset, and dialect-aware database access."""
from .database import (
    Database,
    DatabaseError,
    DatabricksDatabase,
    DatabricksSettings,
    DuckDBDatabase,
    IntegrityError,
    SQLiteDatabase,
    open_database,
)
from .app_repository import (
    BaselineRepository,
    IntakeSessionRepository,
    RecoveryScenarioRepository,
    TrackSnapshotRepository,
)
from .connection import Connection, connect, default_database_path, resolve_dialect
from .generator import DATASET_DIR, generate_dataset, write_dataset
from .repository import HistoryRepository, dataset_loaded, load_dataset
from .schema import ALL_TABLES, APP_TABLES, TABLES, render_ddl

__all__ = [
    "BaselineRepository", "Connection", "IntakeSessionRepository", "RecoveryScenarioRepository",
    "TrackSnapshotRepository", "ALL_TABLES", "APP_TABLES", "DATASET_DIR", "Database", "DatabaseError",
    "DatabricksDatabase", "DatabricksSettings", "DuckDBDatabase", "HistoryRepository", "IntegrityError",
    "SQLiteDatabase", "TABLES", "connect", "dataset_loaded", "default_database_path", "generate_dataset",
    "load_dataset", "open_database", "render_ddl", "resolve_dialect", "write_dataset",
]

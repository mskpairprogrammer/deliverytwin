"""Portfolio data layer: schema, dataset, and dialect-aware database access."""
from .database import (
    Database,
    DatabricksDatabase,
    DatabricksSettings,
    DuckDBDatabase,
    SQLiteDatabase,
    open_database,
)
from .generator import DATASET_DIR, generate_dataset, write_dataset
from .repository import HistoryRepository, dataset_loaded, load_dataset
from .schema import TABLES, render_ddl

__all__ = [
    "DATASET_DIR", "Database", "DatabricksDatabase", "DatabricksSettings", "DuckDBDatabase",
    "HistoryRepository", "SQLiteDatabase", "TABLES", "dataset_loaded", "generate_dataset",
    "load_dataset", "open_database", "render_ddl", "write_dataset",
]

"""Where the checked-in data files live: `src/data/` beside the package (`/app/data` in the container).

DELIVERYTWIN_DATA_DIR points the app at another copy, for example a mounted volume.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("DELIVERYTWIN_DATA_DIR") or Path(__file__).resolve().parents[2] / "data")
DATASET_DIR = DATA_ROOT / "delivery_intelligence"
TRANSCRIPT_DIRNAME = "ram_transcripts"
SAMPLES_DIR = DATA_ROOT / "samples"

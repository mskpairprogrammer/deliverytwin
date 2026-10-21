"""Zip `src/` for Azure App Service code deployment.

The archive holds exactly what the web app runs: the `deliverytwin` package, the `data/` files it reads, and
`requirements.txt` (App Service's Oryx build installs it on deploy). Local databases, `.env` files and caches
never ship. Run from anywhere: ``python scripts/package_app.py [--out dist/deliverytwin-app.zip]``.
"""
from __future__ import annotations

import argparse
import fnmatch
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
DEFAULT_OUT = ROOT / "dist" / "deliverytwin-app.zip"
EXCLUDED = (
    "__pycache__", "*.pyc", ".env", ".env.*", "*.duckdb", "*.duckdb.wal", "*.db", ".pytest_cache",
    ".dockerignore", "Dockerfile", "app.yaml",  # container and Databricks Apps files are not used by App Service
)


def _excluded(path: Path) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for part in path.relative_to(SOURCE).parts for pattern in EXCLUDED)


def requirements(with_databricks: bool = False) -> str:
    """requirements.txt as deployed; App Service installs only this file, so Databricks libraries are merged in."""
    text = (SOURCE / "requirements.txt").read_text(encoding="utf-8")
    if with_databricks:
        extra = (SOURCE / "requirements-databricks.txt").read_text(encoding="utf-8").splitlines()
        text = text.rstrip("\n") + "\n" + "\n".join(line for line in extra if line.strip() and not line.startswith("-r")) + "\n"
    return text


def package(out: Path | str = DEFAULT_OUT, with_databricks: bool = False) -> list[str]:
    """Write the archive with forward-slash paths (Linux extracts it) and return the names it contains."""
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    names = []
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and not _excluded(path):
                name = path.relative_to(SOURCE).as_posix()
                if name == "requirements.txt":
                    archive.writestr(name, requirements(with_databricks))
                else:
                    archive.write(path, name)
                names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zip src/ for Azure App Service code deployment.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"Archive path (default: {DEFAULT_OUT})")
    parser.add_argument("--with-databricks", action="store_true",
                        help="Also install the Databricks SQL connector and SDK (for DELIVERYTWIN_DATABRICKS_ENABLED=1)")
    args = parser.parse_args(argv)
    names = package(args.out, args.with_databricks)
    print(f"Wrote {args.out} ({len(names)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

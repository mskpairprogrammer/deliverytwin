"""Load synthetic source-system test data and move the demo date forward by N sprints.

Usage (from this folder, with the project's virtual environment):
    python simulate.py              # advance 5 sprints (14 days each)
    python simulate.py --sprints 3
    python simulate.py --reset      # clear it; SENSE / FORECAST / ACT go blank again

Reload the dashboard afterwards; no restart is needed.
"""
import sys

from deliverytwin.app import main

if __name__ == "__main__":
    raise SystemExit(main(["simulate", *sys.argv[1:]]))

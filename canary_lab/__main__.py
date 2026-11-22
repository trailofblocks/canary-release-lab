import argparse
import json
import sys
from pathlib import Path

from .runner import LabError, run


parser = argparse.ArgumentParser(description="Run promotion and automatic rollback on a disposable kind cluster.")
parser.add_argument("--output", type=Path, default=Path("artifacts"), help="Directory for JSON evidence and cluster diagnostics.")
args = parser.parse_args()
try:
    print(json.dumps(run(args.output.resolve()), indent=2))
except (LabError, OSError) as error:
    print(f"Canary Release Lab: {error}", file=sys.stderr)
    raise SystemExit(1) from error

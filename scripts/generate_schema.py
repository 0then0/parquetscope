"""Generate one artifact schema for offline browser validation; --check detects drift."""

import argparse
import json
from pathlib import Path

from parquetscope.models import Run

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
path = Path(__file__).resolve().parents[1] / "frontend/src/artifact-schema.json"
text = json.dumps(Run.model_json_schema(), indent=2) + "\n"
if args.check:
    if path.read_text() != text:
        parser.exit(1, "Artifact schema is stale. Run python scripts/generate_schema.py\n")
else:
    path.write_text(text)

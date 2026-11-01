"""Generate the artifact schemas for offline browser validation; --check detects drift."""

import argparse
import json
from pathlib import Path

from parquetscope.models import Run
from parquetscope.models_v2 import SetRun

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
source = Path(__file__).resolve().parents[1] / "frontend/src"
targets = {
    source / "artifact-schema.json": Run,
    source / "artifact-schema-v2.json": SetRun,
}
stale = []
for path, model in targets.items():
    text = json.dumps(model.model_json_schema(), indent=2) + "\n"
    if args.check:
        if not path.exists() or path.read_text() != text:
            stale.append(path.name)
    else:
        path.write_text(text)
if stale:
    names = ", ".join(stale)
    parser.exit(1, f"Artifact schema is stale ({names}). Run scripts/generate_schema.py\n")

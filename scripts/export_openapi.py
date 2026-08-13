"""Write openapi.json from the app factory.

Committed so the UI repo generates its client from a reviewed artifact
rather than a running server, and so contract changes show up in diffs.
"""

import json
import pathlib
import sys
import tempfile

# Running `python scripts/export_openapi.py` puts scripts/ on sys.path, not
# the repo root, so `import src.*` would fail. Prepend the root explicitly
# rather than forcing callers to remember `python -m`.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.api.app import create_app  # noqa: E402 — must follow the sys.path fix

OUT = REPO_ROOT / "openapi.json"

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        spec = create_app(db_path=f"{tmp}/export.db").openapi()
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT}")

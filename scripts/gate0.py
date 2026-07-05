"""Gate 0: import llm with zero provider SDKs; mock provider returns
schema-valid JSON for EVERY schema in schemas.py; config loads; python 3.11."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

failures = []

# 1. No provider SDKs installed (groq/ollama use requests by design).
for sdk in ("google.genai", "groq"):
    if importlib.util.find_spec(sdk.split(".")[0]) is not None and sdk != "google.genai":
        failures.append(f"provider SDK unexpectedly installed: {sdk}")
try:
    from google import genai  # noqa: F401
    failures.append("google-genai unexpectedly installed")
except ImportError:
    pass

# 2. import llm works regardless.
import llm  # noqa: E402
import schemas  # noqa: E402
from config import load_config  # noqa: E402

cfg = load_config()
assert sys.version_info[:2] == (3, 11), "gate must run on Python 3.11"

# 3. Mock provider yields schema-valid JSON for every schema.
for name in schemas.SCHEMAS:
    try:
        out = llm.complete_json(task=name, schema_name=name,
                                prompt=f"gate0 check for {name}",
                                provider="mock", cfg=cfg)
        schemas.validate(out, name)
        print(f"  mock -> {name}: OK")
    except Exception as e:  # noqa: BLE001
        failures.append(f"mock failed for schema '{name}': {e}")

# 4. Provider resolution logs + resolves to mock keyless.
resolved = llm.resolve_provider(cfg)
if resolved != "mock":
    failures.append(f"keyless auto resolution gave '{resolved}', expected 'mock'")

if failures:
    print("GATE 0 FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("GATE 0 PASSED")

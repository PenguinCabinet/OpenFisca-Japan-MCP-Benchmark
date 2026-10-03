"""Run the benchmark with the pinned OpenFisca-Japan-MCP submodule."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
raise SystemExit(subprocess.run(
    ["uv", "run", "--locked", "--python", "3.11", "python", "-X", "utf8", str(ROOT / "bench.py"), *sys.argv[1:]],
    cwd=ROOT,
    check=False,
).returncode)

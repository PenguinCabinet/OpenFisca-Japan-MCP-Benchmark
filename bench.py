"""Generate Harbor tasks from cases.json and run OpenCode with/without MCP.

Single score: result_match (agent's answer.json vs OpenFisca SDK oracle).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.json"
TASKS_DIR = ROOT / "harbor" / "datasets" / "openfisca-bench"

TASK_TOML_TEMPLATE = """schema_version = "1.3"

[task]
name = "openfisca/{task_id}"
description = "OpenFisca-Japan benefit amount QA (result_match only)"

[agent]
timeout_sec = 300.0

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 900.0

artifacts = ["/app/answer.json"]
"""

INSTRUCTION_TEMPLATE = """{prompt}

回答は `/app/answer.json` にJSON配列で保存してください。依頼文で金額を聞かれた順番と同じ順番で記載します。
各要素は金額の数値（例: `10000`）、または範囲を示すオブジェクトです。
範囲の形式: `{{"min": 下限, "max": 上限}}`（端点を含む場合は `min_inclusive` / `max_inclusive` を `true`、
含まない場合は `false`。省略時は含むものとします）。

例（1世帯）: `[10000]`
例（2世帯）: `[15000,10000]`
例（範囲）: `[{{"min": 10000, "max": 15000}}]`

利用可能なOpenFiscaツールがあれば制度確認と計算に使ってください。なければ依頼文から妥当な金額を推定して回答してください。
追加質問はせず、必ず `answer.json` を作成してください。
"""

DOCKERFILE = """FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir openfisca-japan-mcp==0.2.4
# Pre-install the agent toolchain so per-trial setup stays short.
RUN apt-get update && apt-get install -y --no-install-recommends curl bash coreutils ca-certificates \\
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \\
 && apt-get install -y --no-install-recommends nodejs \\
 && npm i -g opencode-ai \\
 && rm -rf /var/lib/apt/lists/*
"""

TEST_SH = """#!/bin/bash
set -uo pipefail
if python3 /tests/check.py; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""

CHECK_PY = '''"""Compare /app/answer.json against /tests/expected.json (result_match only)."""
import json
import sys
from pathlib import Path


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def element_match(actual, expected):
    if is_number(expected):
        if is_number(actual):
            return float(actual) == float(expected)
        if isinstance(actual, dict) and "min" in actual and "max" in actual:
            # Single-point range counts as the scalar value.
            return (
                is_number(actual["min"])
                and is_number(actual["max"])
                and float(actual["min"]) == float(expected)
                and float(actual["max"]) == float(expected)
            )
        return False
    if isinstance(expected, dict) and "min" in expected and "max" in expected:
        if not isinstance(actual, dict):
            return False
        for key in ("min", "max"):
            if key not in actual or not is_number(actual[key]):
                return False
            if float(actual[key]) != float(expected[key]):
                return False
        for key in ("min_inclusive", "max_inclusive"):
            if key in expected or key in actual:
                if bool(actual.get(key, True)) != bool(expected.get(key, True)):
                    return False
        return True
    return False


def main():
    try:
        expected = json.loads(Path("/tests/expected.json").read_text(encoding="utf-8"))["expected"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot load expected.json: {exc}")
        return 1
    candidates = [Path("/app/answer.json"), Path("answer.json")]
    actual_raw = None
    for path in candidates:
        if path.exists():
            actual_raw = path.read_text(encoding="utf-8")
            break
    if actual_raw is None:
        print("answer.json not found")
        return 1
    try:
        actual = json.loads(actual_raw)
    except ValueError as exc:
        print(f"answer.json is not valid JSON: {exc}")
        return 1
    if not isinstance(actual, list) or not isinstance(expected, list):
        print(f"both answer and expected must be arrays: actual={actual!r}")
        return 1
    if len(actual) != len(expected):
        print(f"length mismatch: expected={expected!r} actual={actual!r}")
        return 1
    for index, (item, want) in enumerate(zip(actual, expected)):
        if not element_match(item, want):
            print(f"mismatch at index {index}: expected={want!r} actual={item!r}")
            return 1
    print(f"result_match=1 expected={expected!r} actual={actual!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def load_cases(case_ids: list[str] | None) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            raise ValueError(f"Unknown case ID(s): {', '.join(sorted(missing))}")
    return cases


def oracle_values(case: dict) -> list:
    from openfisca_japan_mcp.sdk import calc, get_tax_benefit_info

    info = get_tax_benefit_info(case["benefit"])
    if not info:
        raise ValueError(f"Unsupported benefit in case {case['id']}: {case['benefit']}")
    result = calc(
        copy.deepcopy(case["household_list"]),
        [{"name": case["benefit"], "household_or_member": info["output_level"]}],
        case["date"],
    )
    values = []
    for target in case["targets"]:
        household = result[target["household_index"]]
        if target["scope"] == "household":
            value = household.get("household_attribute", {}).get(target["name"])
        elif target["scope"] == "member":
            value = household.get("member_attribute", [])[target["member_index"]].get(target["name"])
        else:
            raise ValueError(f"Unknown target scope: {target['scope']}")
        if value is None:
            raise ValueError(f"Oracle did not produce {target['name']} for {case['id']}")
        values.append(value)
    return values


def write_task(case: dict, expected: list) -> Path:
    dest = TASKS_DIR / case["id"]
    (dest / "environment").mkdir(parents=True, exist_ok=True)
    (dest / "solution").mkdir(parents=True, exist_ok=True)
    (dest / "tests").mkdir(parents=True, exist_ok=True)
    (dest / "task.toml").write_text(
        TASK_TOML_TEMPLATE.format(task_id=case["id"]), encoding="utf-8"
    )
    (dest / "instruction.md").write_text(
        INSTRUCTION_TEMPLATE.format(prompt=case["prompt"].strip()), encoding="utf-8"
    )
    (dest / "environment" / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (dest / "tests" / "expected.json").write_text(
        json.dumps({"expected": expected}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    check = (dest / "tests" / "check.py")
    check.write_text(CHECK_PY, encoding="utf-8")
    test_sh = (dest / "tests" / "test.sh")
    test_sh.write_text(TEST_SH, encoding="utf-8")
    try:
        os.chmod(test_sh, 0o755)
    except OSError:
        pass
    (dest / "solution" / "solve.sh").write_text(
        "#!/bin/bash\nset -euo pipefail\ncat > answer.json <<'EOF'\n"
        + json.dumps(expected, ensure_ascii=False)
        + "\nEOF\n",
        encoding="utf-8",
    )
    return dest


def generate(case_ids: list[str] | None) -> list[Path]:
    paths = []
    for case in load_cases(case_ids):
        expected = oracle_values(case)
        paths.append(write_task(case, expected))
        print(f"generated {case['id']} expected={expected}")
    return paths


def job_config(condition: str, model: str,
             case_ids: list[str] | None = None) -> dict:
    agent: dict = {"name": "opencode", "model_name": model}
    if condition == "with-mcp":
        agent["mcp_servers"] = [
            {
                "name": "openfisca",
                "transport": "stdio",
                "command": "openfisca-japan-mcp",
                "args": [],
            }
        ]
    dataset: dict = {"path": str(TASKS_DIR)}
    if case_ids:
        dataset["task_names"] = list(case_ids)
    return {
        "job_name": f"openfisca-{condition}",
        "datasets": [dataset],
        "agents": [agent],
        "environment": {"type": "docker"},
        "n_attempts": 1,
        # opencode setup (npm install inside the container) can exceed the default.
        "agent_setup_timeout_multiplier": 3.0,
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["both", "with-mcp", "without-mcp"], default="both")
    parser.add_argument("--model", default=os.getenv("OPENCODE_MODEL", "opencode/muse-spark-1.3-contributor-free"))
    parser.add_argument("--case", action="append", help="Generate/run only this case ID")
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--jobs-dir", default="jobs")
    parser.add_argument("harbor_args", nargs=argparse.REMAINDER, help="Extra args after --")
    args = parser.parse_args()

    generate(args.case)
    if args.generate_only:
        return

    conditions = ["without-mcp", "with-mcp"] if args.condition == "both" else [args.condition]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    returncode = 0
    for condition in conditions:
        config = job_config(condition, args.model, args.case)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
            config_path = handle.name
        cmd = ["harbor", "run", "--config", config_path,
               "--jobs-dir", args.jobs_dir, "--job-name", f"{stamp}-{condition}",
               *args.harbor_args]
        print(f"+ {' '.join(cmd)}")
        env = os.environ.copy()
        # Harbor on Windows: force UTF-8 file reads and quiet progress output.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("TERM", "dumb")
        if ("-q" not in args.harbor_args and "--quiet" not in args.harbor_args
                and "--silent" not in args.harbor_args):
            cmd.append("-q")
        try:
            result = subprocess.run(cmd, cwd=ROOT, check=False, env=env)
            returncode = result.returncode or returncode
        finally:
            try:
                os.unlink(config_path)
            except OSError:
                pass
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()

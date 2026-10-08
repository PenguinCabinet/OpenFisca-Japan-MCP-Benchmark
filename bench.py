"""Host benchmark: local opencode + model, with/without MCP. Score: result_match."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openfisca_japan_mcp.sdk import calc, get_tax_benefit_info

ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.json"
CASE_TIMEOUT_SEC = 600
RUNS_ROOT = Path(tempfile.gettempdir()) / "obench-runs"

ANSWER_INSTRUCTION_TEMPLATE = """
回答は `answer.json` にJSONオブジェクトで保存してください。キーは {keys} を使ってください。
各金額は数値で記載してください（例: `{{"世帯1": 10000}}`）。
追加質問はせず、必ず `answer.json` を作成してください。
"""


def target_keys(case: dict) -> list[str]:
    """Readable key per target from the case's own household/member names."""
    keys = []
    for target in case["targets"]:
        household = case["household_list"][target["household_index"]]
        if target["scope"] == "household":
            keys.append(household["name"])
        else:
            keys.append(household["member_attribute"][target["member_index"]]["name"])
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    unique = []
    for key, target in zip(keys, case["targets"]):
        if counts[key] > 1:
            household = case["household_list"][target["household_index"]]
            key = f"{household['name']}/{key}"
        unique.append(key)
    return unique


def find_opencode_cli() -> Path:
    """Locate an opencode v2 CLI: OPENCODE_CLI, PATH, or the desktop bundle."""
    override = os.environ.get("OPENCODE_CLI")
    if override:
        return Path(override)
    on_path = shutil.which("opencode")
    if on_path and _major(on_path) == "2":
        return Path(on_path)
    candidates = []
    base = Path.home() / "AppData" / "Roaming" / "ai.opencode.desktop" / "cli"
    if base.is_dir():
        for child in base.iterdir():
            exe = child / "opencode-cli.exe"
            if exe.is_file():
                candidates.append(exe)
    if not candidates:
        raise RuntimeError(
            "opencode v2 CLI not found. Set OPENCODE_CLI or put it on PATH "
            "(e.g. %APPDATA%\\ai.opencode.desktop\\cli\\<version>)"
        )
    return sorted(candidates)[-1]


def _major(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip().split()[-1].split(".")[0] if out.strip() else ""


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def element_match(actual, expected) -> bool:
    return (is_number(actual) and is_number(expected)
            and float(actual) == float(expected))


def values_match(actual, expected) -> bool:
    return (
        isinstance(actual, dict) and isinstance(expected, dict)
        and set(actual) == set(expected)
        and all(element_match(actual[k], expected[k]) for k in expected)
    )


def load_cases(case_ids: list[str] | None) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            raise ValueError(f"Unknown case ID(s): {', '.join(sorted(missing))}")
    return cases


def oracle_values(case: dict) -> dict:
    info = get_tax_benefit_info(case["benefit"])
    if not info:
        raise ValueError(f"Unsupported benefit in case {case['id']}: {case['benefit']}")
    result = calc(
        copy.deepcopy(case["household_list"]),
        [{"name": case["benefit"], "household_or_member": info["output_level"]}],
        case["date"],
    )
    values = {}
    for key, target in zip(target_keys(case), case["targets"]):
        household = result[target["household_index"]]
        if target["scope"] == "household":
            value = household.get("household_attribute", {}).get(target["name"])
        elif target["scope"] == "member":
            value = household.get("member_attribute", [])[target["member_index"]].get(target["name"])
        else:
            raise ValueError(f"Unknown target scope: {target['scope']}")
        if value is None:
            raise ValueError(f"Oracle did not produce {target['name']} for {case['id']}")
        values[key] = value
    return values


def run_trial(cli: Path, workdir: Path, model: str, case: dict, with_mcp: bool) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    config: dict = {"$schema": "https://opencode.ai/config.json", "model": model}
    if with_mcp:
        config["mcp"] = {
            "servers": {
                "openfisca": {
                    "type": "local",
                    "command": [
                        sys.executable, "-X", "utf8", "-c",
                        "from openfisca_japan_mcp.cli import main; main()",
                    ],
                }
            }
        }
    # Isolation: deny repo access. Shell must stay allowed: denying it trips
    # the free-tier gate ("can only be used from within OpenCode").
    repo = str(ROOT).replace("\\", "/") + "/*"
    config["permissions"] = [
        {"action": "read", "resource": repo, "effect": "deny"},
        {"action": "edit", "resource": repo, "effect": "deny"},
    ]
    (workdir / "opencode.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    keys = target_keys(case)
    prompt = (
        case["prompt"].strip()
        + ANSWER_INSTRUCTION_TEMPLATE.format(keys=", ".join(f'"{k}"' for k in keys))
    )
    proc = subprocess.run(
        [str(cli), "run", "--model", model, "--format", "json", "--auto", prompt],
        cwd=workdir, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=CASE_TIMEOUT_SEC,
    )
    (workdir / "stdout.log").write_text(proc.stdout, encoding="utf-8")
    (workdir / "stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"opencode exited {proc.returncode}: {proc.stderr[-500:]}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", choices=["both", "with-mcp", "without-mcp"], default="both")
    parser.add_argument("--model", default=os.getenv("OPENCODE_MODEL", "openai/gpt-5.6-luna"))
    parser.add_argument("--case", action="append")
    args = parser.parse_args()

    cli = find_opencode_cli()
    conditions = ["without-mcp", "with-mcp"] if args.condition == "both" else [args.condition]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {}
    for condition in conditions:
        for case in load_cases(args.case):
            expected = oracle_values(case)
            workdir = RUNS_ROOT / stamp / condition / case["id"]
            try:
                run_trial(cli, workdir, args.model, case, condition == "with-mcp")
                actual = json.loads((workdir / "answer.json").read_text(encoding="utf-8"))
                match = values_match(actual, expected)
            except Exception as exc:
                actual = f"{type(exc).__name__}: {exc}"
                match = False
            summary[f"{condition}/{case['id']}"] = match
            print(f"{condition}/{case['id']}: expected={expected} actual={actual} match={match}",
                  flush=True)
    by_condition = {}
    for condition in conditions:
        keys = [k for k in summary if k.startswith(condition + "/")]
        by_condition[condition] = f"{sum(summary[k] for k in keys)}/{len(keys)}"
    print(json.dumps({"model": args.model, "cases": len(summary),
                      "result_match": f"{sum(summary.values())}/{len(summary)}",
                      "by_condition": by_condition},
                     ensure_ascii=False))
    raise SystemExit(0 if all(summary.values()) else 1)


if __name__ == "__main__":
    main()

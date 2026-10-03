"""Run an oracle-vs-MCP benchmark for OpenFisca-Japan-MCP."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
from openai import AsyncOpenAI
from openfisca_japan_mcp.sdk import calc, get_tax_benefit_info


ROOT = Path(__file__).resolve().parent
CASES_PATH = ROOT / "cases.json"
RESULTS_DIR = ROOT / "results"
MAX_TOOL_ROUNDS = 8
load_dotenv(ROOT / ".env")
YEN_AMOUNT_RE = re.compile(
    r"(?:[0-9０-９][0-9０-９,，]*(?:\.[0-9０-９]+)?|[零〇一二三四五六七八九十百千万億兆]+)\s*(?:円|えん)"
)


def jsonable(value: Any) -> Any:
    """Convert SDK/MCP values into JSON-compatible data."""
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def load_cases() -> list[dict[str, Any]]:
    with CASES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def oracle_value(case: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Calculate expected result through the packaged OpenFisca SDK."""
    info = get_tax_benefit_info(case["benefit"])
    if not info:
        raise ValueError(f"Unsupported benefit in case {case['id']}: {case['benefit']}")
    households = copy.deepcopy(case["household_list"])
    result = calc(
        households,
        [{"name": case["benefit"], "household_or_member": info["output_level"]}],
        case["date"],
    )
    targets = case["targets"] if "targets" in case else [case["target"]]
    values = []
    for target in targets:
        household = result[target["household_index"]]
        if target["scope"] == "household":
            value = household.get("household_attribute", {}).get(target["name"])
        elif target["scope"] == "member":
            value = household.get("member_attribute", [])[target["member_index"]].get(target["name"])
        else:
            raise ValueError(f"Unknown target scope: {target['scope']}")
        if value is None:
            raise RuntimeError(f"Oracle did not produce {target['name']} for case {case['id']}")
        values.append(jsonable(value))
    return values, {"info": info, "calculated_households": jsonable(result)}


def mcp_content(result: Any) -> Any:
    """Prefer structured MCP output, otherwise parse text content when possible."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return jsonable(structured)
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            try:
                parts.append(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                parts.append(text)
    if len(parts) == 1:
        return jsonable(parts[0])
    return jsonable(parts)


def extract_actual_value(value: Any, target: dict[str, Any]) -> Any:
    """Find a calculated field inside the MCP tool's returned household list."""
    # MCP SDKs/servers may wrap the result in a text block or an object.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if isinstance(value, dict):
        for key in ("result", "data", "structuredContent"):
            if key in value:
                found = extract_actual_value(value[key], target)
                if found is not None:
                    return found
        if "household_attribute" in value or "member_attribute" in value:
            value = [value]
        else:
            return None
    if isinstance(value, list):
        index = target["household_index"]
        if index >= len(value) or not isinstance(value[index], dict):
            return None
        household = value[index]
        if target["scope"] == "household":
            return household.get("household_attribute", {}).get(target["name"])
        members = household.get("member_attribute", [])
        member_index = target["member_index"]
        if member_index >= len(members):
            return None
        return members[member_index].get(target["name"])
    return None


def normalized_households(households: Any) -> Any:
    """Compare the facts sent by the model, ignoring freely chosen display names."""
    if not isinstance(households, list):
        return None
    normalized = []
    for household in households:
        if not isinstance(household, dict):
            return None
        members = household.get("member_attribute")
        if not isinstance(members, list):
            return None
        normalized.append({
            "household_attribute": household.get("household_attribute", {}),
            "member_attribute": [
                {key: value for key, value in member.items() if key != "name"}
                for member in members
            ],
        })
    return normalized


def tool_specs(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    specs = []
    for tool in mcp_tools:
        schema = getattr(tool, "input_schema", None)
        if schema is None:
            schema = getattr(tool, "inputSchema", {})
        schema = jsonable(schema) or {"type": "object", "properties": {}}
        specs.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": schema,
            },
        })
    return specs


async def run_case(session: ClientSession, client: AsyncOpenAI, model: str,
                   case: dict[str, Any], tools: list[dict[str, Any]],
                   expected: list[Any], oracle_details: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "OpenFisca-Japan-MCPで制度を確認して計算するアシスタントです。"
                "必ずtax_benefit_infoの後にcalculate_tax_benefitを呼んでください。"
                "依頼文に必要情報は書かれているので、追加質問は禁止です。"
                "依頼文に書かれた日付・全世帯・全員の情報を使い、未記載の属性は補わないでください。"
                "学年は小学n年生=n、中学n年生=n+6、高校n年生=n+9です。"
                "計算結果を得てから、空でない日本語の最終回答を返してください。"
                "各世帯の金額はアラビア数字と円（例: 10,000円）で必ず示してください。"
            ),
        },
        {"role": "user", "content": case["prompt"]},
    ]
    calls: list[dict[str, Any]] = []
    final_text = ""
    error = None
    retries_without_calculation = 0
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0,
            )
            message = response.choices[0].message
            if message.content:
                final_text = message.content
            if not message.tool_calls:
                has_info_call = any(
                    call["name"] == "tax_benefit_info" and not call.get("is_error")
                    for call in calls
                )
                has_calculation_call = any(
                    call["name"] == "calculate_tax_benefit" and not call.get("is_error")
                    for call in calls
                )
                if has_info_call and not has_calculation_call and retries_without_calculation < 2:
                    retries_without_calculation += 1
                    messages.append(message.model_dump(exclude_none=True))
                    messages.append({
                        "role": "user",
                        "content": (
                            "まだ計算処理が終わっていません。追加質問は禁止です。"
                            "calculate_tax_benefitを使ってください。"
                            if retries_without_calculation == 1
                            else "MCPで制度情報は取得済みです。空回答や質問は禁止です。"
                                 "各世帯について、金額をアラビア数字と円で必ず回答してください。"
                        ),
                    })
                    continue
                break
            messages.append(message.model_dump(exclude_none=True))
            for tool_call in message.tool_calls:
                name = tool_call.function.name
                try:
                    arguments = json.loads(tool_call.function.arguments or "{}")
                    result = await session.call_tool(name, arguments)
                    payload = mcp_content(result)
                    is_error = getattr(result, "is_error", None)
                    if is_error is None:
                        is_error = getattr(result, "isError", False)
                    is_error = bool(is_error)
                    calls.append({"name": name, "arguments": arguments, "result": payload, "is_error": is_error})
                    tool_text = json.dumps(payload, ensure_ascii=False, default=str)
                    if is_error:
                        tool_text = "MCP tool returned an error: " + tool_text
                except Exception as exc:  # preserve failed calls in the report
                    calls.append({"name": name, "arguments": tool_call.function.arguments,
                                  "error": f"{type(exc).__name__}: {exc}", "is_error": True})
                    tool_text = f"MCP tool call failed: {type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_text})
        else:
            error = f"Exceeded {MAX_TOOL_ROUNDS} model/tool rounds"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    calculate_calls = [c for c in calls if c["name"] == "calculate_tax_benefit" and not c.get("is_error")]
    targets = case["targets"] if "targets" in case else [case["target"]]
    actual = [None] * len(targets)
    for call in reversed(calculate_calls):
        extracted = [extract_actual_value(call.get("result"), target) for target in targets]
        actual = [new if new is not None else old for old, new in zip(actual, extracted)]
        if all(value is not None for value in actual):
            break
    info_used = any(
        c["name"] == "tax_benefit_info"
        and not c.get("is_error")
        and c.get("arguments", {}).get("tax_benefit_name") == case["benefit"]
        for c in calls
    )
    mcp_tool_used = any(c.get("name") and not c.get("is_error") for c in calls)
    numeric_answer_present = bool(YEN_AMOUNT_RE.search(final_text))
    expected_households = normalized_households(case["household_list"])
    expected_outputs = [{"name": case["benefit"], "household_or_member": oracle_details["info"]["output_level"]}]
    input_match = any(
        not call.get("is_error")
        and normalized_households(call.get("arguments", {}).get("household_list")) == expected_households
        and call.get("arguments", {}).get("output_tax_benefit_list") == expected_outputs
        and call.get("arguments", {}).get("date") == case["date"]
        for call in calculate_calls
    )
    matches = [a is not None and float(a) == float(e) for a, e in zip(actual, expected)]
    result_matches = bool(matches) and all(matches)
    return {
        "id": case["id"],
        "benefit": case["benefit"],
        "date": case["date"],
        "prompt": case["prompt"],
        "expected": expected,
        "actual": jsonable(actual),
        "target_matches": matches,
        "result_match": result_matches,
        "input_match": input_match,
        "info_tool_used": info_used,
        "calculate_tool_used": bool(calculate_calls),
        "mcp_tool_used": mcp_tool_used,
        "numeric_answer_present": numeric_answer_present,
        "tool_and_numeric_answer": mcp_tool_used and numeric_answer_present,
        "tool_calls": calls,
        "final_answer": final_text,
        "error": error,
        "oracle": oracle_details,
    }


async def run_benchmark(args: argparse.Namespace) -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY before running the benchmark")
    model = args.model or os.environ.get("OPENAI_MODEL")
    if not model:
        raise RuntimeError("Set OPENAI_MODEL or pass --model")
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    params = StdioServerParameters(
        command="uv",
        args=[
            "run", "--locked", "--project", str(ROOT), "python", "-X", "utf8", "-c",
            "from openfisca_japan_mcp.cli import main; main()",
        ],
    )
    cases = load_cases()
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
        if not cases:
            raise ValueError("No cases matched --case")

    results = []
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_map = {tool.name: tool for tool in listed.tools}
            required = {"tax_benefit_info", "calculate_tax_benefit"}
            if not required.issubset(tool_map):
                raise RuntimeError(f"MCP server missing tools: {sorted(required - tool_map.keys())}")
            schemas = tool_specs(listed.tools)
            for case in cases:
                expected, details = oracle_value(case)
                print(f"Running {case['id']} (expected={expected})", flush=True)
                results.append(await run_case(session, client, model, case, schemas, expected, details))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = RESULTS_DIR / f"{stamp}.json"
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "base_url": base_url,
        "summary": {
            "cases": len(results),
            "result_matches": sum(r["result_match"] for r in results),
            "input_matches": sum(r["input_match"] for r in results),
            "info_tool_used": sum(r["info_tool_used"] for r in results),
            "calculate_tool_used": sum(r["calculate_tool_used"] for r in results),
            "mcp_tool_used": sum(r["mcp_tool_used"] for r in results),
            "numeric_answer_present": sum(r["numeric_answer_present"] for r in results),
            "tool_and_numeric_answer": sum(r["tool_and_numeric_answer"] for r in results),
        },
        "results": results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for result in results:
        calls = [call["name"] for call in result["tool_calls"]]
        print(
            f"{result['id']}: expected={result['expected']} actual={result['actual']} "
            f"result_match={result['result_match']} input_match={result['input_match']} "
            f"mcp_tool_used={result['mcp_tool_used']} "
            f"numeric_answer_present={result['numeric_answer_present']} tools={calls}"
        )
        if result["final_answer"]:
            print(f"  LLM answer: {result['final_answer']}")
        if result["error"]:
            print(f"  error: {result['error']}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"Report: {output}")
    await client.close()
    return 0 if all(r["tool_and_numeric_answer"] for r in results) else 1


def main() -> None:
    if os.name == "nt" and sys.flags.utf8_mode != 1:
        raise SystemExit(
            "Windowsでは `python scripts/run_benchmark.py` から実行してください "
            "（OpenFiscaのYAML読込にUTF-8モードが必要です）。"
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Model name (or set OPENAI_MODEL)")
    parser.add_argument("--base-url", help="OpenAI-compatible API URL (or set OPENAI_BASE_URL)")
    parser.add_argument("--case", action="append", help="Run only this case ID; may be repeated")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_benchmark(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        traceback.print_exception(exc, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

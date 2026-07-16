import json
import os
import sys
import asyncio

# MCP Server for STRIDE Test Automation
# Exposes tools: run_tests, collect_logs, analyze_logs, generate_report

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Install mcp package: pip install mcp")
    sys.exit(1)

from test_runner import run_all_tests
from pipeline import analyze, generate_report, collect_logs

server = Server("stride-test-automation")

_state = {
    "last_report": None,
}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="run_tests",
            description="Run all test cases defined in config.yaml. Returns structured JSON results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "config_path": {
                        "type": "string",
                        "description": "Path to config.yaml. Defaults to config/config.yaml.",
                    },
                    "password": {
                        "type": "string",
                        "description": "SSH password. If omitted, reads from NS_PASSWORD env var.",
                    },
                    "selected_tests": {
                        "type": "string",
                        "description": "Comma-separated test names to run (e.g. 'TC_01,TC_03'). Omit to run all.",
                    },
                },
            },
        ),
        Tool(
            name="collect_logs",
            description="Collect and save per-step logs from the last test run to a timestamped directory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_path": {
                        "type": "string",
                        "description": "Path to a test_report JSON file. If omitted, uses last run.",
                    },
                },
            },
        ),
        Tool(
            name="analyze_logs",
            description="Analyze test logs to pinpoint exact failure steps, commands and output context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_path": {
                        "type": "string",
                        "description": "Path to test report JSON. If omitted, uses last run.",
                    },
                },
            },
        ),
        Tool(
            name="generate_report",
            description="Generate a detailed human-readable report with root-cause classification.",
            inputSchema={
                "type": "object",
                "properties": {
                    "report_path": {
                        "type": "string",
                        "description": "Path to test report JSON. If omitted, uses last run.",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "run_tests":
        return await _handle_run_tests(arguments)
    elif name == "collect_logs":
        return await _handle_collect_logs(arguments)
    elif name == "analyze_logs":
        return await _handle_analyze_logs(arguments)
    elif name == "generate_report":
        return await _handle_generate_report(arguments)
    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def _handle_run_tests(arguments: dict):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = arguments.get("config_path", os.path.join(project_root, "config", "config.yaml"))
    password = arguments.get("password") or os.environ.get("NS_PASSWORD", "")

    if not password:
        return [TextContent(type="text", text="ERROR: No password. Set NS_PASSWORD or pass 'password' argument.")]

    os.environ["NS_PASSWORD"] = password

    selected = None
    if arguments.get("selected_tests"):
        selected = [t.strip() for t in arguments["selected_tests"].split(",")]

    try:
        report = run_all_tests(config_path, selected_tests=selected)
        _state["last_report"] = report
        summary = (
            f"Test run complete: {report['passed']}/{report['total_tests']} passed, "
            f"{report['failed']} failed, {report['errors']} errors"
        )
        return [TextContent(type="text", text=summary + "\n\n" + json.dumps(report, indent=2))]
    except Exception as e:
        return [TextContent(type="text", text=f"ERROR running tests: {str(e)}")]


async def _handle_collect_logs(arguments: dict):
    report = _load_report(arguments)
    if isinstance(report, list):
        return report  # error TextContent
    log_dir, log_files = collect_logs(report)
    return [TextContent(type="text", text=f"Logs saved to: {log_dir}\n" + "\n".join(log_files))]


async def _handle_analyze_logs(arguments: dict):
    report = _load_report(arguments)
    if isinstance(report, list):
        return report
    analysis = analyze(report)
    return [TextContent(type="text", text=json.dumps(analysis, indent=2))]


async def _handle_generate_report(arguments: dict):
    report = _load_report(arguments)
    if isinstance(report, list):
        return report
    analysis = analyze(report)
    text = generate_report(report, analysis)
    return [TextContent(type="text", text=text)]


def _load_report(arguments):
    report_path = arguments.get("report_path")
    if report_path:
        with open(report_path, "r") as f:
            return json.load(f)
    if _state["last_report"]:
        return _state["last_report"]
    return [TextContent(type="text", text="No report available. Run tests first or provide report_path.")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

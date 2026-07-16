"""
Multi-step test runner.
Loads YAML test files from the tests/ directory, runs each step sequentially
over SSH, and evaluates pass/fail per step.

Usage:
  python test_runner.py                     # run all tests
  python test_runner.py --list              # list available tests
  python test_runner.py --run TC_01,TC_02   # run specific tests
  python test_runner.py --config <path>     # use a custom config file
"""

import json
import os
import re
import sys
import glob
import yaml
from datetime import datetime

from ssh_connector import SSHConnector


class StepResult:
    """Result for a single step within a test case."""

    def __init__(self, name):
        self.name = name
        self.status = "not_run"
        self.start_time = None
        self.end_time = None
        self.commands_sent = []
        self.raw_output = ""   # stdout text
        self.stderr = ""       # stderr text
        self.termination_codes = []
        self.last_exit_code = None
        self.failures = []
        self.error_line = None

    def to_dict(self):
        return {
            "name": self.name,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self._duration(),
            "commands_sent": self.commands_sent,
            "stdout": self.raw_output,
            "stderr": self.stderr,
            "exit_code": self.last_exit_code,
            # kept for backward compatibility
            "raw_output": self.raw_output,
            "termination_codes": self.termination_codes,
            "last_exit_code": self.last_exit_code,
            "failures": self.failures,
            "error_line": self.error_line,
        }

    def _duration(self):
        if self.start_time and self.end_time:
            fmt = "%Y-%m-%d %H:%M:%S"
            start = datetime.strptime(self.start_time, fmt)
            end = datetime.strptime(self.end_time, fmt)
            return (end - start).total_seconds()
        return None


class TestCaseResult:
    """Result for a complete test case (multiple steps)."""

    def __init__(self, name, description=""):
        self.name = name
        self.description = description
        self.status = "not_run"
        self.start_time = None
        self.end_time = None
        self.steps = []
        self.tags = []

    def to_dict(self):
        passed_steps = sum(1 for s in self.steps if s.status == "passed")
        return {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self._duration(),
            "tags": self.tags,
            "total_steps": len(self.steps),
            "passed_steps": passed_steps,
            "failed_steps": len(self.steps) - passed_steps,
            "steps": [s.to_dict() for s in self.steps],
            # Flattened fields for pipeline compatibility
            "commands_sent": [cmd for s in self.steps for cmd in s.commands_sent],
            "raw_output": "\n".join(s.raw_output for s in self.steps),
            "termination_codes": [c for s in self.steps for c in s.termination_codes],
            "last_exit_code": self.steps[-1].last_exit_code if self.steps else None,
            "failures": [f for s in self.steps if s.status == "failed" for f in s.failures],
            "error_line": next((s.error_line for s in self.steps if s.error_line), None),
        }

    def _duration(self):
        if self.start_time and self.end_time:
            fmt = "%Y-%m-%d %H:%M:%S"
            start = datetime.strptime(self.start_time, fmt)
            end = datetime.strptime(self.end_time, fmt)
            return (end - start).total_seconds()
        return None


def load_config(config_path):
    """Load global configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def load_test_cases(tests_dir):
    """Load all test YAML files from directory, sorted by filename."""
    pattern = os.path.join(tests_dir, "*.yaml")
    files = sorted(glob.glob(pattern))
    test_cases = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fp:
            tc = yaml.safe_load(fp)
            tc["_file"] = f
            test_cases.append(tc)
    return test_cases


def evaluate_step(output_text, criteria):
    """Evaluate step output against criteria. Returns (passed, failures, error_line, term_codes, exit_code)."""
    failures = []
    error_line = None
    expect_failure = criteria.get("expect_failure", False)

    term_codes = re.findall(r"Termination Info\s*:\s*(\d+)", output_text)
    last_exit_code = int(term_codes[-1]) if term_codes else None

    expected_code = criteria.get("expected_exit_code")
    if expected_code is not None:
        if last_exit_code is None:
            failures.append("No 'Termination Info' found in output")
        elif last_exit_code != expected_code:
            failures.append(f"Expected exit code: {expected_code}, got: {last_exit_code}")

    success_msg = criteria.get("success_message")
    if success_msg and success_msg not in output_text:
        failures.append(f"Success message not found: '{success_msg}'")

    expected_output = criteria.get("expected_output")
    if expected_output:
        if expect_failure:
            if expected_output in output_text:
                return True, [], None, term_codes, last_exit_code
            else:
                failures.append(f"Expected failure output '{expected_output}' not found")
                return False, failures, None, term_codes, last_exit_code
        else:
            if expected_output not in output_text:
                failures.append(f"Expected output not found: '{expected_output}'")

    if not expect_failure:
        error_keywords = criteria.get("error_keywords", [])
        for keyword in error_keywords:
            for line in output_text.splitlines():
                if keyword.lower() in line.lower():
                    failures.append(f"Error keyword '{keyword}' found: {line.strip()}")
                    if error_line is None:
                        error_line = line.strip()
                    break

    passed = len(failures) == 0
    return passed, failures, error_line, term_codes, last_exit_code


def resolve_variables(text, variables):
    """Replace {{var_name}} placeholders with extracted values."""
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def extract_variables(output_text, extract_patterns):
    """Extract named variables from output using regex patterns.

    extract_patterns is a dict like:
        resource_id: "ID\\s*[:=]\\s*(\\S+)"
        pid:         "PID\\s*[:=]\\s*(\\d+)"
    """
    extracted = {}
    for var_name, pattern in extract_patterns.items():
        match = re.search(pattern, output_text)
        if match:
            extracted[var_name] = match.group(1)
    return extracted


_EXIT_PROBE = 'echo "__STRIDE_EXIT__=$?"'


def run_step(ssh, step_def, variables=None):
    """Run a single step and return (StepResult, extracted_vars)."""
    if variables is None:
        variables = {}

    result = StepResult(step_def["name"])

    raw_responses = step_def.get("responses", [])
    resolved = [resolve_variables(str(cmd), variables) for cmd in raw_responses]

    # Inject exit-code probe for bash/sh steps (before the final 'exit' if present)
    shell_cmd = step_def.get("command", "bash")
    probe_responses = list(resolved)
    if shell_cmd in ("bash", "sh"):
        if probe_responses and probe_responses[-1].strip() in ("exit", "exit 0"):
            probe_responses = probe_responses[:-1] + [_EXIT_PROBE, probe_responses[-1]]
        else:
            probe_responses = probe_responses + [_EXIT_PROBE]

    result.commands_sent = resolved  # report shows original commands, not the probe
    result.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not resolved:
        result.status = "passed"
        result.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result.raw_output = "(no commands to execute)"
        return result, {}

    try:
        stdout_lines, stderr_text, ssh_exit = ssh.execute_interactive_command(
            command=shell_cmd,
            responses=probe_responses,
            wait=step_def.get("wait", 5),
        )
        raw = "".join(stdout_lines)

        # Parse and strip the injected probe line
        probe_match = re.search(r'__STRIDE_EXIT__=(\d+)', raw)
        if probe_match:
            result.last_exit_code = int(probe_match.group(1))
            raw = re.sub(r'__STRIDE_EXIT__=\d+\r?\n?', '', raw)
        else:
            result.last_exit_code = ssh_exit if ssh_exit is not None else None

        result.raw_output = raw
        result.stderr = stderr_text
    except Exception as e:
        result.status = "error"
        result.failures = [f"Exception: {str(e)}"]
        result.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return result, {}

    result.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    extract_patterns = step_def.get("extract", {})
    new_vars = extract_variables(result.raw_output, extract_patterns)
    if new_vars:
        print(f"      extracted: {new_vars}")

    criteria = step_def.get("criteria", {})
    passed, failures, error_line, term_codes, last_exit = evaluate_step(
        result.raw_output, criteria
    )

    # Also honour expected_exit_code against the real captured exit code
    if criteria.get("expected_exit_code") is not None and result.last_exit_code is not None:
        expected_ec = criteria["expected_exit_code"]
        if result.last_exit_code != expected_ec:
            if not any("exit code" in f.lower() for f in failures):
                failures.append(
                    f"Exit code: expected {expected_ec}, got {result.last_exit_code}"
                )
            passed = False

    result.status = "passed" if (len(failures) == 0) else "failed"
    result.failures = failures
    result.error_line = error_line
    result.termination_codes = [int(c) for c in term_codes]
    # Prefer probe-captured exit code; fall back to NonStop termination code
    if result.last_exit_code is None:
        result.last_exit_code = last_exit

    return result, new_vars


def ensure_ssh(ssh):
    """Ensure SSH connection is active, reconnect if needed."""
    try:
        transport = ssh.client.get_transport() if ssh.client else None
        if not transport or not transport.is_active():
            raise Exception("inactive")
    except Exception:
        print("      (reconnecting SSH...)")
        try:
            ssh.disconnect()
        except Exception:
            pass
        ssh.connect()


def run_test_case(ssh, test_case):
    """Run all steps in a test case and return TestCaseResult."""
    tc_result = TestCaseResult(
        name=test_case["name"],
        description=test_case.get("description", "")
    )
    tc_result.tags = test_case.get("tags", [])
    tc_result.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    steps = test_case.get("steps", [])
    all_passed = True
    variables = {}

    for i, step_def in enumerate(steps, 1):
        print(f"    Step {i}/{len(steps)}: {step_def['name']}")

        ensure_ssh(ssh)
        step_result, new_vars = run_step(ssh, step_def, variables)
        variables.update(new_vars)
        tc_result.steps.append(step_result)

        if step_result.status == "passed":
            print(f"      -> PASS")
        elif step_result.status == "error":
            print(f"      -> ERROR: {step_result.failures[0]}")
            all_passed = False
        else:
            print(f"      -> FAIL: {step_result.failures[0] if step_result.failures else 'unknown'}")
            all_passed = False

    tc_result.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tc_result.status = "passed" if all_passed else "failed"
    return tc_result


def run_all_tests(config_path, selected_tests=None):
    """Run test cases and produce a JSON report.

    Args:
        config_path:    Path to config.yaml.
        selected_tests: List of test file basenames to run (e.g. ['TC_01']).
                        None = run all.
    """
    config = load_config(config_path)
    host_config = config["host"]

    config_dir = os.path.dirname(os.path.abspath(config_path))
    tests_dir = os.path.normpath(os.path.join(config_dir, config.get("tests_dir", "../tests")))

    password = os.environ.get("NS_PASSWORD", host_config.get("password", ""))
    if not password:
        print("ERROR: No password. Set the NS_PASSWORD environment variable.")
        sys.exit(1)

    test_cases = load_test_cases(tests_dir)
    if not test_cases:
        print(f"ERROR: No test files found in {tests_dir}/")
        sys.exit(1)

    if selected_tests:
        test_cases = [
            tc for tc in test_cases
            if any(sel in os.path.basename(tc["_file"]) for sel in selected_tests)
        ]
        if not test_cases:
            print(f"ERROR: No matching tests found for: {selected_tests}")
            sys.exit(1)

    print(f"Running {len(test_cases)} test case(s) from {tests_dir}/")
    results = []
    run_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with SSHConnector(
        hostname=host_config["hostname"],
        username=host_config["username"],
        password=password,
        port=host_config.get("port", 22),
    ) as ssh:
        for tc in test_cases:
            print(f"\n{'='*60}")
            print(f"TEST CASE: {tc['name']}")
            print(f"{'='*60}")

            try:
                tc_result = run_test_case(ssh, tc)
            except Exception as e:
                print(f"  >> CRASH: {e}")
                tc_result = TestCaseResult(name=tc["name"], description=tc.get("description", ""))
                tc_result.status = "error"
                tc_result.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                tc_result.end_time = tc_result.start_time
                tc_result.steps.append(StepResult("(crashed)"))
                tc_result.steps[0].status = "error"
                tc_result.steps[0].failures = [f"Unhandled exception: {str(e)}"]

            results.append(tc_result)

            passed_steps = sum(1 for s in tc_result.steps if s.status == "passed")
            total_steps = len(tc_result.steps)
            status_icon = "PASS" if tc_result.status == "passed" else "FAIL"
            print(f"\n  >> {status_icon} ({passed_steps}/{total_steps} steps)")

    run_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report = {
        "run_timestamp": run_start,
        "run_end": run_end,
        "host": host_config["hostname"],
        "total_tests": len(results),
        "passed": sum(1 for r in results if r.status == "passed"),
        "failed": sum(1 for r in results if r.status == "failed"),
        "errors": sum(1 for r in results if r.status == "error"),
        "results": [r.to_dict() for r in results],
    }

    report_path = f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {report['passed']}/{report['total_tests']} test cases passed")
    if report["failed"] > 0:
        print(f"FAILED TEST CASES:")
        for r in results:
            if r.status == "failed":
                tc_dict = r.to_dict()
                print(f"  - {r.name}: {tc_dict['passed_steps']}/{tc_dict['total_steps']} steps passed")
                for s in r.steps:
                    if s.status == "failed":
                        print(f"    Step FAILED: {s.name}")
                        if s.failures:
                            print(f"      {s.failures[0]}")
    print(f"{'='*60}")
    print(f"Report saved: {report_path}")

    return report


if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_file = os.path.join(project_root, "config", "config.yaml")

    args = sys.argv[1:]
    selected = None

    if "--help" in args or "-h" in args:
        print("Usage:")
        print("  python test_runner.py                    # Run all tests")
        print("  python test_runner.py --list             # List available tests")
        print("  python test_runner.py --run TC_01,TC_02  # Run specific tests")
        print("  python test_runner.py --config <path>    # Use custom config")
        sys.exit(0)

    if "--config" in args:
        idx = args.index("--config")
        config_file = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    if "--list" in args:
        config = load_config(config_file)
        config_dir = os.path.dirname(os.path.abspath(config_file))
        tests_dir = os.path.normpath(os.path.join(config_dir, config.get("tests_dir", "../tests")))
        test_cases = load_test_cases(tests_dir)
        print(f"\nAvailable tests in {tests_dir}/:\n")
        print(f"{'#':<4} {'File':<30} {'Name'}")
        print("-" * 80)
        for i, tc in enumerate(test_cases, 1):
            fname = os.path.basename(tc["_file"]).replace(".yaml", "")
            print(f"{i:<4} {fname:<30} {tc['name']}")
        print(f"\nUse --run TC_01,TC_03 or --run mytest to run specific tests.")
        sys.exit(0)

    if "--run" in args:
        idx = args.index("--run")
        selection = args[idx + 1]
        selected = [p.strip() for p in selection.split(",")]

    report = run_all_tests(config_file, selected_tests=selected)
    sys.exit(0 if report["failed"] == 0 else 1)

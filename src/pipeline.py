"""
Single-command pipeline: Run tests → Collect logs → Analyze → Generate report.
No manual steps. Just run it.

Usage:
  python pipeline.py                        # run all tests
  python pipeline.py --run TC_01,TC_02      # run specific tests
  python pipeline.py <path/to/config.yaml>  # use a custom config
"""

import os
import sys
import json
import glob
from datetime import datetime

from test_runner import run_all_tests
import ai_rca


def find_latest_report():
    reports = glob.glob("test_report_*.json")
    if not reports:
        return None
    return max(reports, key=os.path.getmtime)


def analyze(report):
    """Deep-analyze failures at the step level. Returns structured analysis."""
    analysis = []
    for result in report["results"]:
        if result["status"] in ("failed", "error"):
            entry = {
                "test_name": result["name"],
                "status": result["status"],
                "total_steps": result.get("total_steps", 0),
                "passed_steps": result.get("passed_steps", 0),
                "failed_steps_detail": [],
            }

            for step in result.get("steps", []):
                if step["status"] in ("failed", "error"):
                    step_detail = {
                        "step_name": step["name"],
                        "commands_sent": step["commands_sent"],
                        "failures": step["failures"],
                        "error_line": step.get("error_line"),
                        "failed_at_command": None,
                        "output_context": None,
                        # captured for AI RCA
                        "raw_output": step.get("stdout") or step.get("raw_output", ""),
                        "stderr":     step.get("stderr", ""),
                        "exit_code":  step.get("exit_code"),
                    }

                    output = step.get("raw_output", "")
                    if output and step.get("error_line"):
                        err_line = step["error_line"]
                        output_lines = output.splitlines()
                        for line_num, line in enumerate(output_lines, 1):
                            if err_line in line:
                                step_detail["error_at_line"] = line_num
                                start = max(0, line_num - 3)
                                end = min(len(output_lines), line_num + 2)
                                step_detail["output_context"] = output_lines[start:end]
                                break

                        for cmd in step["commands_sent"]:
                            cmd_pos = output.find(cmd)
                            err_pos = output.find(err_line)
                            if cmd_pos >= 0 and err_pos >= cmd_pos:
                                step_detail["failed_at_command"] = cmd
                                break

                    entry["failed_steps_detail"].append(step_detail)

            analysis.append(entry)

    return analysis


def generate_report(report, analysis, ai_rcas=None):
    """Generate a detailed failure report with per-step, per-line breakdown."""
    lines = []
    lines.append("=" * 70)
    lines.append(" STRIDE - TEST AUTOMATION PIPELINE REPORT")
    lines.append(f" Host: {report['host']}")
    lines.append(f" Run:  {report['run_timestamp']} -> {report['run_end']}")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f" RESULTS: {report['passed']}/{report['total_tests']} passed | "
                 f"{report['failed']} failed | {report['errors']} errors")
    lines.append("")

    lines.append("-" * 70)
    lines.append(f" {'#':<3} {'Test Name':<40} {'Status':<8} {'Steps':<12}")
    lines.append("-" * 70)
    for i, r in enumerate(report["results"], 1):
        status = "PASS" if r["status"] == "passed" else "FAIL"
        steps_info = f"{r.get('passed_steps', '?')}/{r.get('total_steps', '?')}"
        lines.append(f" {i:<3} {r['name'][:40]:<40} {status:<8} {steps_info}")
    lines.append("-" * 70)

    if analysis:
        lines.append("")
        lines.append("=" * 70)
        lines.append(" FAILURE ANALYSIS (per-step breakdown)")
        lines.append("=" * 70)

        for entry in analysis:
            lines.append("")
            lines.append(f" TEST: {entry['test_name']}")
            lines.append(f"   Steps: {entry['passed_steps']}/{entry['total_steps']} passed")
            lines.append("")

            for step_detail in entry["failed_steps_detail"]:
                lines.append(f"   FAILED STEP: {step_detail['step_name']}")

                if step_detail["failed_at_command"]:
                    lines.append(f"   Command that failed: {step_detail['failed_at_command']}")

                if step_detail.get("error_at_line"):
                    lines.append(f"   Error at output line: {step_detail['error_at_line']}")

                if step_detail["error_line"]:
                    lines.append(f"   Error text: {step_detail['error_line']}")

                for f in step_detail["failures"]:
                    lines.append(f"     - {f}")

                if step_detail.get("output_context"):
                    lines.append(f"   Output context:")
                    for ctx_line in step_detail["output_context"]:
                        marker = " >>>" if step_detail.get("error_line") and step_detail["error_line"] in ctx_line else "    "
                        lines.append(f"   {marker} {ctx_line.rstrip()}")

                lines.append("")

        # AI-powered RCA (shown only when available)
        if ai_rcas:
            lines.append("")
            lines.append("=" * 70)
            lines.append(" AI ROOT CAUSE ANALYSIS")
            lines.append("=" * 70)
            for test_name, step_rcas in ai_rcas.items():
                lines.append(f"")
                lines.append(f" {test_name}:")
                for rca in step_rcas:
                    conf = rca.get("confidence", "?").upper()
                    lines.append(f"   Step [{rca['step_name']}]  [{conf} confidence]")
                    lines.append(f"   Root cause  : {rca.get('root_cause', '')}")
                    if rca.get("explanation"):
                        lines.append(f"   Explanation : {rca['explanation']}")
                    if rca.get("fix"):
                        lines.append(f"   Fix         : {rca['fix']}")
                    lines.append("")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def _diagnose_root_cause(step_detail):
    """Classify the root cause from error patterns."""
    import re as _re
    error = (step_detail.get("error_line") or "").lower()
    failures = step_detail.get("failures", [])
    all_text = " ".join(failures).lower() + " " + error

    # Extract the expected value from the failure message for specific diagnosis
    _exp_match = _re.search(r"expected output not found: '([^']+)'", all_text)
    expected_val = _exp_match.group(1) if _exp_match else ""

    if "pin_incorrect" in all_text or "incorrect pin" in all_text:
        return "Authentication failure — incorrect PIN or credentials"

    # Service-state checks (SVC_UP / SVC_DOWN tokens)
    elif expected_val == "svc_up" or ("svc_up" in all_text and "not found" in all_text):
        return "Service is not running — not installed or systemd unit is inactive/failed"
    # Missing resource / socket / file (MISSING token)
    elif "missing" in all_text and "not found" in all_text:
        step = step_detail.get("step_name", "").lower()
        if any(k in step for k in ("socket", "sock", "permission", "perm")):
            return "Socket or file does not exist — dependent service has never started on this host"
        return "Expected resource is missing — dependent service may not be installed"
    # Log-file existence checks
    elif expected_val == "log_exists":
        return "Log file does not exist — the dependent service has never run on this host"

    # Generic count-is-1 checks (grep -c, systemctl count, etc.)
    elif expected_val == "1" and "not found" in all_text:
        step = step_detail.get("step_name", "").lower()
        if any(k in step for k in ("active", "running", "service", "daemon")):
            return "Service/process count is 0 — not installed or not running"
        elif any(k in step for k in ("port", "bound", "listen")):
            return "Port is not bound — dependent service is not listening"
        elif any(k in step for k in ("rule", "audit", "policy")):
            return "Security rule/policy count is 0 — configuration not applied"
        elif any(k in step for k in ("metric", "endpoint", "reachable")):
            return "Metrics endpoint unreachable — monitoring agent not running"
        else:
            return "Expected count of 1 not met — feature, service, or config entry is absent"

    # Zero-result checks (expect nothing present)
    elif expected_val == "0" and "not found" in all_text:
        return "Expected zero occurrences but found entries — review unexpected services or permissions"

    # TLS / certificate checks
    elif expected_val in ("valid_1y", "cert_ok") or "valid" in expected_val:
        return "Certificate validity check failed — cert may be expired, near-expiry, or not generated correctly"

    # Size / threshold checks
    elif expected_val in ("size_ok", "file_ok", "within_threshold"):
        return "Threshold check failed — measured value exceeded the configured limit"

    # Explicit 'Expected output not found' catch-all (must be after specific checks)
    elif "expected output not found" in all_text:
        return f"Command output did not contain '{expected_val}' — check command syntax or system state"

    elif "not configured" in all_text or "not found" in all_text:
        return "Resource not configured or does not exist on this system"
    elif "permission" in all_text or "denied" in all_text or "not authorized" in all_text:
        return "Insufficient permissions for this operation"
    elif "connection" in all_text or "timeout" in all_text:
        return "Connection/communication failure with target process"
    elif "syntax" in all_text or "illegal" in all_text:
        return "Invalid command syntax - check command format"
    elif "not running" in all_text or "stopped" in all_text:
        return "Target process is not running"
    elif "expect" in all_text and "not found" in all_text:
        return "Expected output string was not present in command response"
    elif "error keyword" in all_text:
        return "Error keyword detected in output - command produced an error"
    elif step_detail.get("failed_at_command"):
        return f"Command '{step_detail['failed_at_command']}' produced unexpected output"
    else:
        return "Unknown - review raw output in logs"


def generate_html_report(report, analysis, ai_rcas=None):
    """Generate a self-contained HTML report with expandable per-step detail."""
    import html as _html

    def _e(s):
        return _html.escape(str(s) if s is not None else "")

    def _badge(status):
        if status == "passed":
            return '<span class="badge badge-pass">PASS</span>'
        if status == "failed":
            return '<span class="badge badge-fail">FAIL</span>'
        return '<span class="badge badge-error">ERR</span>'

    def _exit_chip(code):
        if code is None:
            return '<span class="chip chip-neutral">exit ?</span>'
        cls = "chip-pass" if code == 0 else "chip-fail"
        return f'<span class="chip {cls}">exit {_e(code)}</span>'

    # Build test cards
    cards_html = []
    failed_ids = {e["test_name"] for e in analysis}
    for i, r in enumerate(report["results"]):
        status = r["status"]
        steps_label = f"{r.get('passed_steps','?')}/{r.get('total_steps','?')} steps"
        dur = f"{r.get('duration_seconds','?')}s"
        header_class = "test-header-fail" if status != "passed" else ""

        # Build step rows
        step_rows = []
        for j, s in enumerate(r.get("steps", []), 1):
            sc = "step-pass" if s["status"] == "passed" else "step-fail" if s["status"] == "failed" else "step-error"
            cmds_html = "".join(f'<span class="cmd-chip">{_e(c)}</span>' for c in s.get("commands_sent", []))
            stdout_block = f'<div class="out-label">stdout</div><pre>{_e(s.get("stdout") or s.get("raw_output",""))}</pre>' if (s.get("stdout") or s.get("raw_output")) else ""
            stderr_block = f'<div class="out-label err-label">stderr</div><pre class="pre-err">{_e(s.get("stderr",""))}</pre>' if s.get("stderr") else ""
            failures_block = "".join(f'<div class="failure-line">✗ {_e(f)}</div>' for f in s.get("failures", []))
            step_rows.append(f"""
            <div class="step {sc}">
              <div class="step-row">
                {_badge(s["status"])}
                <span class="step-name">{_e(s["name"])}</span>
                {_exit_chip(s.get("exit_code") if s.get("exit_code") is not None else s.get("last_exit_code"))}
                <span class="step-dur">{s.get("duration_seconds","?")}s</span>
              </div>
              <div class="cmds">{cmds_html}</div>
              {failures_block}
              {stdout_block}
              {stderr_block}
            </div>""")

        steps_html = "".join(step_rows)

        # AI RCA block (only for failed tests that have AI results)
        ai_block = ""
        if ai_rcas and r["name"] in ai_rcas:
            step_rca_rows = []
            for rca in ai_rcas[r["name"]]:
                conf = rca.get("confidence", "low").lower()
                conf_class = f"conf-{conf}"
                fix_html = f'<div class="ai-row"><span class="ai-label">Fix</span><span class="ai-fix">{_e(rca.get("fix",""))}</span></div>' if rca.get("fix") else ""
                expl_html = f'<div class="ai-row"><span class="ai-label">Why</span><span class="ai-value">{_e(rca.get("explanation",""))}</span></div>' if rca.get("explanation") else ""
                step_rca_rows.append(f"""
                <div class="ai-step-block">
                  <div class="ai-step-title">{_e(rca["step_name"])} &nbsp;<span class="{conf_class}">({conf} confidence)</span></div>
                  <div class="ai-row"><span class="ai-label">Root cause</span><span class="ai-value">{_e(rca.get("root_cause",""))}</span></div>
                  {expl_html}
                  {fix_html}
                </div>""")
            ai_block = f"""
            <div class="ai-section">
              <div class="ai-header"><span class="ai-badge">✦ AI RCA</span></div>
              {"\n".join(step_rca_rows)}
            </div>"""

        cards_html.append(f"""
        <div class="test-card">
          <div class="test-header {header_class}" onclick="toggle(this)">
            {_badge(status)}
            <span class="test-name">{_e(r["name"])}</span>
            <span class="test-meta">{steps_label} &nbsp;·&nbsp; {dur}</span>
            <span class="chevron">▶</span>
          </div>
          <div class="steps-panel">{steps_html}{ai_block}</div>
        </div>""")

    cards = "\n".join(cards_html)
    total = report["total_tests"]
    passed = report["passed"]
    failed = report["failed"]
    errors = report["errors"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>STRIDE Report</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e2e2;font-size:14px}}
  .header{{background:#161926;padding:24px 32px;border-bottom:1px solid #252840}}
  .title{{font-size:22px;font-weight:700;color:#fff;letter-spacing:.5px}}
  .subtitle{{font-size:12px;color:#6b7280;margin-top:4px}}
  .stats{{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap}}
  .stat{{background:#1e2235;padding:12px 20px;border-radius:8px;min-width:90px}}
  .stat-num{{font-size:26px;font-weight:700}}
  .stat-label{{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}}
  .n-total{{color:#94a3b8}}.n-pass{{color:#22c55e}}.n-fail{{color:#ef4444}}.n-err{{color:#f59e0b}}
  .content{{padding:24px 32px;max-width:1100px}}
  .test-card{{background:#161926;border-radius:8px;margin-bottom:10px;border:1px solid #252840;overflow:hidden}}
  .test-header{{display:flex;align-items:center;gap:14px;padding:13px 18px;cursor:pointer;user-select:none;transition:background .15s}}
  .test-header:hover{{background:#1e2235}}
  .test-header-fail{{border-left:3px solid #ef4444}}
  .test-name{{flex:1;font-weight:500}}
  .test-meta{{font-size:12px;color:#6b7280}}
  .chevron{{font-size:10px;color:#6b7280;transition:transform .2s}}
  .chevron.open{{transform:rotate(90deg)}}
  .badge{{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap}}
  .badge-pass{{background:#14532d;color:#22c55e}}.badge-fail{{background:#450a0a;color:#ef4444}}.badge-error{{background:#451a03;color:#f59e0b}}
  .steps-panel{{display:none;padding:0 18px 14px;border-top:1px solid #252840}}
  .steps-panel.open{{display:block}}
  .step{{margin-top:10px;padding:10px 12px;background:#0f1117;border-radius:6px;border-left:3px solid #252840}}
  .step-pass{{border-left-color:#22c55e}}.step-fail{{border-left-color:#ef4444}}.step-error{{border-left-color:#f59e0b}}
  .step-row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
  .step-name{{flex:1;font-size:13px;font-weight:500}}
  .step-dur{{font-size:11px;color:#6b7280}}
  .cmds{{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}}
  .cmd-chip{{background:#1e2235;padding:2px 8px;border-radius:4px;font-size:11px;font-family:monospace;color:#94a3b8}}
  .chip{{padding:2px 8px;border-radius:4px;font-size:11px;font-family:monospace}}
  .chip-pass{{background:#14532d;color:#22c55e}}.chip-fail{{background:#450a0a;color:#ef4444}}.chip-neutral{{background:#1e2235;color:#6b7280}}
  .out-label{{font-size:10px;color:#6b7280;text-transform:uppercase;margin-top:8px;margin-bottom:3px;letter-spacing:.4px}}
  .err-label{{color:#f59e0b}}
  pre{{background:#090b0f;padding:9px;border-radius:4px;font-size:11px;color:#94a3b8;white-space:pre-wrap;word-break:break-all;max-height:180px;overflow-y:auto;margin-top:0}}
  pre.pre-err{{color:#f59e0b}}
  .failure-line{{color:#ef4444;font-size:12px;margin-top:5px;padding-left:2px}}
  .ai-section{{margin-top:12px;background:#0d1117;border:1px solid #2a2060;border-radius:6px;padding:12px 14px}}
  .ai-header{{display:flex;align-items:center;gap:8px;margin-bottom:10px}}
  .ai-badge{{background:#2a2060;color:#a78bfa;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;white-space:nowrap}}
  .ai-step-title{{font-size:12px;color:#a78bfa;font-weight:600}}
  .ai-row{{margin-top:6px;font-size:12px;line-height:1.6}}
  .ai-label{{color:#6b7280;min-width:90px;display:inline-block;font-size:11px;text-transform:uppercase;letter-spacing:.3px}}
  .ai-value{{color:#e2e2e2}}
  .ai-fix{{color:#4ade80}}
  .conf-high{{color:#22c55e}}.conf-medium{{color:#f59e0b}}.conf-low{{color:#ef4444}}
  .ai-step-block{{margin-top:8px;padding-top:8px;border-top:1px solid #1e1a40}}
  .ai-step-block:first-child{{margin-top:0;padding-top:0;border-top:none}}
</style>
</head>
<body>
<div class="header">
  <div class="title">STRIDE — Test Automation Report</div>
  <div class="subtitle">Host: {_e(report["host"])} &nbsp;·&nbsp; {_e(report["run_timestamp"])} → {_e(report["run_end"])}</div>
  <div class="stats">
    <div class="stat"><div class="stat-num n-total">{total}</div><div class="stat-label">Total</div></div>
    <div class="stat"><div class="stat-num n-pass">{passed}</div><div class="stat-label">Passed</div></div>
    <div class="stat"><div class="stat-num n-fail">{failed}</div><div class="stat-label">Failed</div></div>
    <div class="stat"><div class="stat-num n-err">{errors}</div><div class="stat-label">Errors</div></div>
  </div>
</div>
<div class="content">
{cards}
</div>
<script>
function toggle(header){{
  var panel=header.nextElementSibling;
  var chev=header.querySelector('.chevron');
  panel.classList.toggle('open');
  chev.classList.toggle('open');
}}
// Auto-expand failed tests
document.querySelectorAll('.test-header-fail').forEach(function(h){{toggle(h);}});
</script>
</body>
</html>"""


def collect_logs(report):
    """Save per-step logs to a timestamped run directory."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_dir = os.path.join("logs", f"run_{timestamp}")
    os.makedirs(log_dir, exist_ok=True)

    log_files = []
    for i, result in enumerate(report["results"], 1):
        safe_name = result["name"].replace(" ", "_").replace("/", "_").replace("\\", "_")[:50]
        log_file = os.path.join(log_dir, f"{i:02d}_{safe_name}.log")

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"{'='*70}\n")
            f.write(f"Test: {result['name']}\n")
            f.write(f"Status: {result['status'].upper()}\n")
            f.write(f"Host: {report['host']}\n")
            f.write(f"Start: {result['start_time']} | End: {result['end_time']} | Duration: {result['duration_seconds']}s\n")
            f.write(f"Steps: {result.get('passed_steps', '?')}/{result.get('total_steps', '?')} passed\n")
            f.write(f"{'='*70}\n\n")

            for j, step in enumerate(result.get("steps", []), 1):
                status_icon = "PASS" if step["status"] == "passed" else "FAIL" if step["status"] == "failed" else "ERR"
                f.write(f"--- Step {j}: [{status_icon}] {step['name']} ---\n")
                f.write(f"  Commands: {step['commands_sent']}\n")
                f.write(f"  Duration: {step.get('duration_seconds', '?')}s\n")

                if step.get("raw_output"):
                    f.write(f"  Output:\n")
                    for line_num, line in enumerate(step["raw_output"].splitlines(), 1):
                        marker = ">>>" if step.get("error_line") and step["error_line"] in line else "   "
                        f.write(f"    {line_num:>4} {marker} {line}\n")

                if step["status"] != "passed":
                    f.write(f"  FAILURES:\n")
                    for failure in step.get("failures", []):
                        f.write(f"    - {failure}\n")
                    if step.get("error_line"):
                        f.write(f"  ERROR LINE: {step['error_line']}\n")

                f.write(f"\n")

        log_files.append(log_file)

    analysis_file = os.path.join(log_dir, "analysis.json")
    with open(analysis_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    log_files.append(analysis_file)

    return log_dir, log_files


def main():
    print("=" * 70)
    print(" STRIDE - SINGLE COMMAND TEST PIPELINE")
    print("=" * 70)
    print()

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_file = os.path.join(project_root, "config", "config.yaml")

    args = sys.argv[1:]
    selected = None
    if "--run" in args:
        idx = args.index("--run")
        selection = args[idx + 1]
        selected = [p.strip() for p in selection.split(",")]
        args = args[:idx] + args[idx + 2:]
    if args:
        config_file = args[0]

    print("[1/4] Running tests...")
    print("-" * 70)
    report = run_all_tests(config_file, selected_tests=selected)
    print()

    print("[2/4] Collecting logs...")
    log_dir, log_files = collect_logs(report)
    print(f"     Log directory: {log_dir}")
    for lf in log_files:
        print(f"       - {lf}")
    print()

    print("[3/4] Analyzing failures...")
    analysis = analyze(report)
    print(f"     Found {len(analysis)} failed test(s)")
    print()

    print("[4/4] Generating report (+ AI RCA)...")
    ai_rcas = ai_rca.analyze_failures(analysis)
    final_report = generate_report(report, analysis, ai_rcas=ai_rcas)
    print()
    print(final_report)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    report_file = f"pipeline_report_{ts}.txt"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(final_report)

    html_file = f"pipeline_report_{ts}.html"
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(generate_html_report(report, analysis, ai_rcas=ai_rcas))

    print(f"\nText report : {report_file}")
    print(f"HTML report : {html_file}   ← open in browser")
    print(f"Logs        : {log_dir}/")

    sys.exit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()

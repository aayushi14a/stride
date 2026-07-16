# STRIDE
### SSH Test Runner with Intelligent Diagnosis Engine

> One command. Zero human steps. Full failure diagnosis.

![Python](https://img.shields.io/badge/python-3.9+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
[![Demo report](https://img.shields.io/badge/demo-report.html-purple)](https://github.com/aayushi14a/stride/blob/main/demo/report.html)

STRIDE automates multi-step SSH test execution, real-time log collection, chained failure detection, and AI-powered root-cause analysis — built in Python, runs from any machine, requires no agent on the target host.

---

## What it does

> 📄 **[View a sample HTML report →](demo/report.html)** (download and open in browser)

```
$ python src/pipeline.py

[1/4] Running tests...       SSH into target, execute YAML-defined step sequences
[2/4] Collecting logs...     Per-step output saved to timestamped log directory
[3/4] Analyzing failures...  Detect which step failed and why
[4/4] Generating report...   AI-powered root-cause + remediation per failed step
      [AI RCA] OK  6.1 PostgreSQL service is active  (319 tokens)
      [AI RCA] OK  6.2 PostgreSQL Unix socket exists  (363 tokens)
      [AI RCA] OK  8.1 node_exporter service is active  (321 tokens)

HTML report : pipeline_report_20260716.html   ← open in browser
```

---

## Architecture

```
tests/*.yaml          config/config.yaml
     │                       │
     ▼                       ▼
┌─────────────────────────────────────┐
│           test_runner.py            │
│  SSH connect → step-by-step exec    │
│  Variable extraction ({{var}})      │
│  Crash-resilient per-test isolation │
└──────────────────┬──────────────────┘
                   │  test_report_*.json
                   ▼
┌─────────────────────────────────────┐
│             pipeline.py             │
│  Log collector  →  Failure analyzer │
│  AI RCA (Groq / OpenAI / Copilot)  │
│  HTML + text report generation      │
└──────────────────┬──────────────────┘
                   │
          pipeline_report_*.html
               logs/run_*/
```

---

## Key capabilities

| | |
|---|---|
| **Multi-step SSH execution** | Each test runs N ordered commands over a single SSH session |
| **Variable chaining** | Regex-extract runtime values (paths, IDs, checksums) → inject via `{{var}}` into later steps |
| **Chained failure detection** | A single unavailable service causes a clean 3–4 step cascade — AI traces all failures to one root cause |
| **Crash resilience** | If a test crashes or SSH drops, the framework reconnects and continues remaining tests |
| **Exit code capture** | Real exit codes captured via injected probe; displayed per step |
| **stdout / stderr split** | Each step stores stdout and stderr separately for clean diagnosis |
| **AI root-cause analysis** | Failed steps sent to LLM (Groq/OpenAI/Copilot) — returns root cause, explanation, and fix |
| **HTML report** | Dark-theme interactive report with expandable steps, AI RCA badges, exit code chips |
| **Excel → YAML** | Convert existing Excel test specs to executable YAML in one command |
| **MCP server** | Exposes `run_tests`, `analyze_logs`, `generate_report` as tools for AI agent orchestration |

---

## Test format

```yaml
name: Resource Lifecycle with Integrity Verification
tags: [variable-chaining, lifecycle]
steps:

- name: Create resource and capture path
  command: bash
  responses:
  - tmpfile=$(mktemp /tmp/stride_XXXXXX) && echo "RESOURCE_PATH=$tmpfile"
  - exit
  wait: 5
  extract:
    res_path: "RESOURCE_PATH=(/tmp/stride_\\S+)"   # ← captured at runtime
  criteria:
    expected_output: RESOURCE_PATH=

- name: Write and verify checksum
  command: bash
  responses:
  - echo 'payload' > {{res_path}} && sha256sum {{res_path}}   # ← variable injected
  - exit
  wait: 5
  extract:
    checksum: "([a-f0-9]{64})"
  criteria:
    expected_output: stride_
```

**`extract:`** — named regex patterns applied to stdout; values stored in shared context  
**`{{variable}}`** — substituted from prior step extractions across the full test  
**`criteria.expected_output`** — must appear in stdout to pass  
**`criteria.expect_failure`** — marks a step expected to produce an error  
**`criteria.error_keywords`** — any match triggers automatic failure  

---

## Quick start

```bash
# 1. Clone and install
git clone https://github.com/aayushi14a/stride
cd stride
pip install -r requirements.txt

# 2. Configure your target host
#    Edit config/config.yaml
host:
  hostname: "192.168.1.100"
  port: 22
  username: "admin"

# 3. Set credentials
export NS_PASSWORD="yourpassword"

# 4. (Optional) AI root-cause analysis — pick one:
export GROQ_API_KEY="gsk_..."          # free tier — console.groq.com
export OPENAI_API_KEY="sk-..."         # OpenAI
# export STRIDE_AI_VERIFY_SSL=false    # add if behind a corporate HTTPS-inspection proxy

# 5. Run
python src/pipeline.py
```

---

## Usage reference

```
python src/pipeline.py                          Run all tests
python src/pipeline.py --run TC_06,TC_10        Run specific tests by name fragment
python src/pipeline.py config/other.yaml        Use a different config file

python src/test_runner.py --list                List all available test cases
python src/test_runner.py --run TC_01           Run a single test directly (no AI RCA)

python src/excel_to_yaml.py my_tests.xlsx       Convert all Excel rows → YAML files
python src/excel_to_yaml.py my_tests.xlsx tests/ --select 1,3,5   Convert specific rows
python src/excel_to_yaml.py --sample            Generate a sample Excel template

python server/mcp_server.py                     Start MCP server for AI agent integration
```

**Environment variables**

| Variable | Purpose | Default |
|---|---|---|
| `NS_PASSWORD` | SSH password for target host | required |
| `GROQ_API_KEY` | Groq AI provider (free tier) | — |
| `OPENAI_API_KEY` | OpenAI provider | — |
| `GITHUB_TOKEN` | GitHub Copilot provider (needs `copilot` scope) | — |
| `STRIDE_AI_API_KEY` + `STRIDE_AI_BASE_URL` | Any OpenAI-compatible endpoint (Ollama, Azure…) | — |
| `STRIDE_AI_MODEL` | Override model name | `gpt-4o-mini` / `llama-3.1-8b-instant` |
| `STRIDE_AI_VERIFY_SSL` | Set `false` to skip SSL cert check (corporate proxy) | `true` |

---

## Included test suite (13 test cases, 61 steps)

| Test | Focus | Demonstrates |
|------|-------|--------------|
| TC_01 SSH Security | CIS SSH hardening checks | Real security audit failures |
| TC_02 Firewall | UFW + fail2ban + port exposure | Expected service failures |
| TC_03 File System | Critical file permission audit | `stat` permission checks |
| TC_04 Process Chain | Live process inspection | Variable chaining: `{{ssh_pid}}` → 4 steps |
| TC_05 Resource Lifecycle | Create → checksum → delete | Two variables: `{{res_path}}` + `{{checksum}}` |
| TC_06 DB Chain | PostgreSQL availability | **Chained failure** — 4 steps, 1 root cause |
| TC_07 Docker Chain | Container runtime security | **Chained failure** — 4 steps, 1 root cause |
| TC_08 Monitoring Chain | Prometheus node_exporter | **Chained failure** — 4 steps, 1 root cause |
| TC_09 Log Audit | SSH auth failure analysis | Variable: `{{fail_count}}` → threshold check |
| TC_10 TLS Lifecycle | Certificate generation + CN verify | Three variables chained across 5 steps |
| TC_11 Audit Chain | auditd + audit rules | **Chained failure** — 5 steps, 1 root cause |
| TC_12 Journal Analysis | journald error analysis | Variable: `{{top_unit}}` → targeted follow-up |
| TC_13 Log Health | rsyslog + log rotation | Variables: `{{log_size_kb}}` + `{{largest_log}}` |

---

## AI root-cause analysis

STRIDE sends only **pre-analyzed metadata** to the LLM — never raw logs. Each failed step is summarised into ~400 tokens before the API call, achieving a **97% token reduction** vs uploading full logs.

```
Step [6.1 PostgreSQL service is active]  (HIGH confidence)
Root cause  : PostgreSQL is not installed on this system.
Explanation : The systemctl command returned SVC_DOWN because no postgresql
              unit exists. The socket and port checks in steps 6.2-6.4
              subsequently fail for the same reason.
Fix         : Install PostgreSQL: sudo apt install postgresql && sudo systemctl enable --now postgresql
```

**Provider priority** (set whichever env var you have):

| Env var | Provider | Notes |
|---------|----------|-------|
| `GROQ_API_KEY` | Groq — llama-3.1-8b-instant | Free tier, fast |
| `OPENAI_API_KEY` | OpenAI | Paid |
| `GITHUB_TOKEN` | GitHub Copilot | Needs `copilot` scope PAT |
| `STRIDE_AI_API_KEY` + `STRIDE_AI_BASE_URL` | Any OpenAI-compatible endpoint | Ollama, Azure, etc. |

---

## Tech stack

- **Python 3.9+** — no compiled dependencies
- **Paramiko** — SSH2 protocol, interactive stdin/stdout execution
- **OpenAI SDK** — unified client for Groq, OpenAI, Copilot, and compatible endpoints
- **httpx** — async HTTP with per-client SSL configuration
- **PyYAML** — test specification format
- **openpyxl** — Excel → YAML conversion
- **MCP SDK** — Model Context Protocol server for AI agent integration

---

## Project structure

```
STRIDE-portfolio/
├── src/
│   ├── pipeline.py        # orchestrator: run → collect → analyze → report
│   ├── test_runner.py     # SSH multi-step engine + variable extraction
│   ├── ssh_connector.py   # Paramiko wrapper with idle-based output detection
│   ├── ai_rca.py          # LLM root-cause analysis (multi-provider)
│   └── excel_to_yaml.py   # Excel test spec converter
├── tests/
│   └── TC_01 … TC_13      # 13 YAML test cases, 61 steps
├── server/
│   └── mcp_server.py      # MCP tools: run_tests, analyze_logs, generate_report
└── config/
    └── config.yaml        # host, port, credentials config
```

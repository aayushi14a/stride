import os, json, re

_SYSTEM_PROMPT = "You are a test automation expert diagnosing SSH command failures. You receive structured metadata from an automated pipeline (no raw logs). Be concise, specific, and actionable."

_USER_TEMPLATE = """A test step failed. Provide a root-cause analysis.

Test case : {test_name}
Step      : {step_name}
Commands  : {commands}
Exit code : {exit_code}
Stdout    : {stdout}
Stderr    : {stderr}
Failures  : {failures}

Return ONLY a JSON object with these exact keys (no markdown fences):
{{"root_cause":"<one sentence>","explanation":"<2-3 sentences>","fix":"<1-2 sentences>","confidence":"high|medium|low"}}"""


def _exchange_copilot_token(github_token):
    import urllib.request, urllib.error
    req = urllib.request.Request(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": "token " + github_token,
            "Accept": "application/json",
            "User-Agent": "STRIDE/1.0",
            "Editor-Version": "vscode/1.85.0",
            "Editor-Plugin-Version": "copilot-chat/0.12.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            token = data.get("token")
            if token:
                print(f"     [AI RCA] Copilot session token obtained (expires: {data.get('expires_at','?')})")
                return token
            print(f"     [AI RCA] Token exchange response had no 'token' field: {list(data.keys())}")
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:200]
        except: pass
        print(f"     [AI RCA] Copilot token exchange HTTP {e.code}: {body}")
    except urllib.error.URLError as e:
        print(f"     [AI RCA] Copilot token exchange network error: {e.reason}")
        print(f"     [AI RCA] Hint: if behind a proxy, set HTTPS_PROXY=http://host:port")
    except Exception as e:
        print(f"     [AI RCA] Copilot token exchange error: {e}")
    return None


def _gh_auth_token():
    """Try to get OAuth token from the gh CLI (more reliable than PAT for Copilot)."""
    import subprocess
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        token = result.stdout.strip()
        if token and result.returncode == 0:
            return token
    except Exception:
        pass
    return None


def _build_client():
    """Return (openai_client, model, description) or (None, None, reason).

    Provider priority:
      1. GITHUB_TOKEN  → Copilot session token → api.githubcopilot.com
      2. OPENAI_API_KEY → api.openai.com
      3. GROQ_API_KEY   → api.groq.com  (free tier, llama-3.1-8b-instant)
      4. STRIDE_AI_API_KEY + STRIDE_AI_BASE_URL → any OpenAI-compatible endpoint
    """
    try:
        import openai as _oa
    except ImportError:
        return None, None, "openai not installed — run: pip install openai"

    model    = os.environ.get("STRIDE_AI_MODEL", "").strip()
    base_url = os.environ.get("STRIDE_AI_BASE_URL", "").strip()
    gh       = os.environ.get("GITHUB_TOKEN", "").strip()
    oa       = os.environ.get("OPENAI_API_KEY", "").strip()
    groq     = os.environ.get("GROQ_API_KEY", "").strip()
    custom_k = os.environ.get("STRIDE_AI_API_KEY", "").strip()

    # 1. GitHub Copilot (enterprise-friendly via token exchange)
    if gh and not base_url:
        print("     [AI RCA] Exchanging GitHub token for Copilot session token...")
        ct = _exchange_copilot_token(gh)
        if not ct:
            # Classic PAT sometimes fails — try the gh CLI OAuth token instead
            print("     [AI RCA] Trying gh CLI OAuth token (more reliable for Copilot)...")
            oauth = _gh_auth_token()
            if oauth and oauth != gh:
                ct = _exchange_copilot_token(oauth)
        if ct:
            c = _make_openai_client(_oa, ct,
                base_url="https://api.githubcopilot.com",
                extra_headers={"Editor-Version": "vscode/1.85.0", "Copilot-Integration-Id": "copilot-chat"})
            return c, model or "gpt-4o-mini", "GitHub Copilot / " + (model or "gpt-4o-mini")
        print("     [AI RCA] Copilot token exchange failed — trying other providers...")

    # 2. OpenAI
    if oa:
        return _make_openai_client(_oa, oa, base_url=base_url or None), model or "gpt-4o-mini", "OpenAI / " + (model or "gpt-4o-mini")

    # 3. Groq (free tier — https://console.groq.com)
    if groq:
        return (
            _make_openai_client(_oa, groq, base_url="https://api.groq.com/openai/v1"),
            model or "llama-3.1-8b-instant",
            "Groq / " + (model or "llama-3.1-8b-instant"),
        )

    # 4. Custom OpenAI-compatible endpoint (e.g. Ollama, Azure OpenAI, LM Studio)
    if custom_k and base_url:
        return _make_openai_client(_oa, custom_k, base_url=base_url), model or "default", f"Custom / {model or 'default'}"

    # 5. GitHub Models as last resort (may be blocked by enterprise policy)
    if gh:
        return (
            _make_openai_client(_oa, gh, base_url="https://models.inference.ai.azure.com"),
            model or "gpt-4o-mini",
            "GitHub Models / " + (model or "gpt-4o-mini"),
        )

    return None, None, (
        "No AI provider configured. Set one of:\n"
        "     OPENAI_API_KEY   — OpenAI API\n"
        "     GROQ_API_KEY     — Groq (free tier, fast): https://console.groq.com\n"
        "     GITHUB_TOKEN     — GitHub Copilot (needs 'copilot' scope PAT)\n"
        "     STRIDE_AI_API_KEY + STRIDE_AI_BASE_URL  — any OpenAI-compatible endpoint"
    )


def _make_openai_client(_oa, api_key, base_url=None, extra_headers=None):
    """Build an OpenAI client, respecting STRIDE_AI_VERIFY_SSL=false for
    corporate networks that intercept HTTPS with a self-signed CA."""
    import httpx
    verify = os.environ.get("STRIDE_AI_VERIFY_SSL", "true").strip().lower() != "false"
    if not verify:
        import warnings
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        http_client = httpx.Client(verify=False)
    else:
        http_client = None

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if extra_headers:
        kwargs["default_headers"] = extra_headers
    if http_client:
        kwargs["http_client"] = http_client
    return _oa.OpenAI(**kwargs)


def _extract_json(text):
    """Extract JSON from LLM response, handling markdown fences and trailing text."""
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find the first { and last } to extract just the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in response: " + text[:200])
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # LLM sometimes adds trailing commas or unescaped characters — try with relaxed parsing
        # Remove trailing commas before } or ]
        candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
        # Replace unescaped newlines inside string values
        candidate = re.sub(r'(?<!\\)\n', ' ', candidate)
        return json.loads(candidate)


def analyze_failures(analysis):
    if not analysis:
        return {}
    client, model, info = _build_client()
    if client is None:
        print(f"     [AI RCA] {info}")
        return {}
    print(f"     [AI RCA] Using {info}")
    results = {}
    for entry in analysis:
        test_name = entry["test_name"]
        step_rcas = []
        for step in entry.get("failed_steps_detail", []):
            ctx = step.get("output_context") or []
            stdout_s = "\n".join(ctx)[:600] if ctx else (step.get("raw_output") or "")[:600]
            prompt = _USER_TEMPLATE.format(
                test_name=test_name, step_name=step["step_name"],
                commands=", ".join(step.get("commands_sent", [])),
                exit_code=step.get("exit_code", step.get("last_exit_code", "?")),
                stdout=stdout_s or "(empty)",
                stderr=(step.get("stderr") or "")[:300] or "(empty)",
                failures="; ".join(step.get("failures", [])),
            )
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                    max_tokens=350, temperature=0.2,
                )
                rca = _extract_json(resp.choices[0].message.content)
                rca["step_name"] = step["step_name"]
                tokens = resp.usage.total_tokens if resp.usage else "?"
                print(f"     [AI RCA] OK  {step['step_name']}  ({tokens} tokens)")
            except Exception as exc:
                exc_str = str(exc)
                # Give actionable hints for common network failures
                if "connection error" in exc_str.lower() or "connecterror" in exc_str.lower():
                    hint = ("Network unreachable — try: export HTTPS_PROXY=http://your-proxy:port  "
                            "or use Ollama locally: STRIDE_AI_BASE_URL=http://localhost:11434/v1")
                    print(f"     [AI RCA] NETWORK ERR — {hint}")
                    # Abort remaining calls — same network will fail
                    break
                elif "401" in exc_str or "unauthorized" in exc_str.lower():
                    hint = "Auth failed — check your API key or token scope"
                    print(f"     [AI RCA] AUTH ERR — {hint}: {exc_str[:120]}")
                elif "disabled" in exc_str.lower():
                    print(f"     [AI RCA] PROVIDER DISABLED — {exc_str[:200]}")
                    break
                else:
                    print(f"     [AI RCA] ERR {step['step_name']}: {exc_str[:120]}")
                rca = {"step_name": step["step_name"], "root_cause": f"AI unavailable: {exc_str[:120]}",
                       "explanation": "", "fix": "", "confidence": "low"}
            step_rcas.append(rca)
        if step_rcas:
            results[test_name] = step_rcas
    return results

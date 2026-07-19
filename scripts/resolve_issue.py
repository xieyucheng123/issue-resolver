#!/usr/bin/env python3
"""
Issue Resolver — Direct LLM approach.

Reads a GitHub issue, uses GLM-5.2 to generate a unified diff,
applies it, runs tests, and creates a pull request.

No sandbox isolation issues — all file operations happen in the host workspace.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path


def get_env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise ValueError(f"{name} environment variable is required")
    return v


def gh_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com/repos/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def llm_chat(prompt: str, api_key: str, model: str, base_url: str) -> str:
    """Call LLM via OpenAI-compatible API."""
    url = f"{base_url}/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an expert software engineer. You output ONLY a unified diff, nothing else."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 8192,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def collect_repo_context(max_files: int = 30) -> str:
    """Collect relevant source files for LLM context."""
    extensions = {".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java",
                  ".json", ".toml", ".yaml", ".yml", ".md", ".sql", ".sh", ".css"}
    skip_dirs = {"target", "node_modules", ".git", "dist", "build", ".next", "__pycache__"}

    files = []
    for root, dirs, fs in os.walk("."):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in fs:
            p = Path(root) / f
            if p.suffix in extensions and p.stat().st_size < 50000:
                files.append(p)
    files = sorted(files)[:max_files]

    parts = []
    for p in files:
        try:
            content = p.read_text(errors="replace")[:3000]
            parts.append(f"--- {p} ---\n{content}")
        except Exception:
            pass
    return "\n\n".join(parts)


def extract_diff(llm_output: str) -> str:
    """Extract unified diff from LLM output."""
    # Try to find diff in code blocks
    m = re.search(r"```(?:diff)?\n(.*?)```", llm_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Otherwise look for diff headers
    lines = llm_output.split("\n")
    diff_lines = []
    in_diff = False
    for line in lines:
        if line.startswith("diff --git") or line.startswith("--- ") or line.startswith("+++ "):
            in_diff = True
        if in_diff:
            diff_lines.append(line)
    if diff_lines:
        return "\n".join(diff_lines)
    return llm_output.strip()


def apply_diff(diff: str) -> bool:
    """Apply unified diff using git apply."""
    diff_path = "/tmp/agent_fix.diff"
    Path(diff_path).write_text(diff)
    result = subprocess.run(["git", "apply", "--whitespace=fix", diff_path],
                          capture_output=True, text=True)
    if result.returncode != 0:
        print(f"git apply failed: {result.stderr}")
        # Try with --3way
        result = subprocess.run(["git", "apply", "--3way", diff_path],
                              capture_output=True, text=True)
        if result.returncode != 0:
            print(f"git apply --3way also failed: {result.stderr}")
            return False
    return True


def run_tests() -> bool:
    if Path("Cargo.toml").exists():
        r = subprocess.run(["cargo", "test", "--", "--nocapture"],
                         capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    if Path("package.json").exists():
        r = subprocess.run(["npm", "test", "--", "--passWithNoTests"],
                         capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    return True


def main():
    print("=" * 60)
    print("Issue Resolver (Direct LLM)")
    print("=" * 60)

    api_key = get_env("LLM_API_KEY")
    model = get_env("LLM_MODEL", "openai/glm-5.2")
    base_url = get_env("LLM_BASE_URL", "https://api.modelarts-maas.com/v2")
    github_token = get_env("GITHUB_TOKEN")
    issue_number = int(get_env("ISSUE_NUMBER"))
    issue_type = get_env("ISSUE_TYPE", "issue")
    repo_name = get_env("REPO_NAME")

    # Strip provider prefix for API call
    api_model = model.split("/", 1)[-1] if "/" in model else model

    print(f"Repo: {repo_name}, Issue: #{issue_number}, Model: {api_model}")

    # Fetch issue
    issue = gh_api("GET", f"{repo_name}/issues/{issue_number}", github_token)
    title = issue["title"]
    body = issue.get("body", "") or "(no description)"
    print(f"Title: {title}")

    # Comment: started
    gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
           {"body": "🤖 Agent started working on this issue using GLM-5.2."})

    # Collect repo context
    ctx = collect_repo_context()
    print(f"Collected {len(ctx)} chars of repo context")

    # Build prompt
    prompt = f"""## Issue
**Title**: {title}
**Description**:
{body}

## Current Code
Here are the relevant files in the repository:

{ctx}

## Task
Generate a unified diff to fix this issue. The diff should:
- Make minimal, focused changes
- Follow existing code style
- Not break existing functionality

Output ONLY the unified diff (as `diff --git a/... b/...` format), no explanation.
"""

    print("Calling GLM-5.2...")
    try:
        response = llm_chat(prompt, api_key, api_model, base_url)
    except Exception as e:
        print(f"LLM call failed: {e}")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": f"❌ LLM call failed: {e}"})
        sys.exit(1)

    print(f"LLM response: {len(response)} chars")

    # Extract and apply diff
    diff = extract_diff(response)
    if not diff or "diff --git" not in diff:
        print("No valid diff in LLM response")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": "⚠️ Agent could not generate a valid fix. Please handle manually."})
        sys.exit(0)

    print(f"Diff:\n{diff[:500]}...")

    if not apply_diff(diff):
        print("Failed to apply diff")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": "⚠️ Agent generated a fix but it could not be applied automatically."})
        sys.exit(1)

    # Check changes
    r = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    if not r.stdout.strip():
        print("No changes after applying diff")
        gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
               {"body": "⚠️ Agent analyzed the issue but no changes were needed."})
        sys.exit(0)

    print(f"Changes:\n{r.stdout}")

    # Create branch, commit, push
    branch = f"agent/fix-{issue_type}-{issue_number}"
    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", f"Fix #{issue_number}: {title}\n\nGenerated by AI agent using GLM-5.2."], check=True)

    # Push using PAT
    remote_url = subprocess.run(["git", "remote", "get-url", "origin"],
                              capture_output=True, text=True).stdout.strip()
    # Use token in URL for push
    push_url = f"https://x-access-token:{github_token}@github.com/{repo_name}.git"
    subprocess.run(["git", "push", push_url, branch], check=True)

    # Run tests
    print("Running tests...")
    tests_ok = run_tests()
    print(f"Tests: {'passed' if tests_ok else 'failed'}")

    # Create PR
    pr = gh_api("POST", f"{repo_name}/pulls", github_token, {
        "title": f"Fix #{issue_number}: {title}",
        "body": f"## Automated Fix\n\n{diff[:2000]}\n\nCloses #{issue_number}\n\n---\nGenerated by AI agent using GLM-5.2 via MAAS",
        "head": branch,
        "base": "main",
    })
    pr_url = pr["html_url"]
    pr_num = pr["number"]
    print(f"PR created: {pr_url}")

    # Comment on issue
    emoji = "✅" if tests_ok else "⚠️"
    gh_api("POST", f"{repo_name}/issues/{issue_number}/comments", github_token,
           {"body": f"{emoji} Agent created PR #{pr_num}: {pr_url}\n\n**Tests**: {'passed' if tests_ok else 'failed'}\n**Model**: GLM-5.2"})

    print(f"\n✅ Done! PR: {pr_url}")


if __name__ == "__main__":
    main()

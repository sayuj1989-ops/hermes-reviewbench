# ReviewBench contract adapter for the Hermes agent (AGENT_CONTRACT.md).
# Runs one headless Hermes review per pull request and writes RB_OUT findings.
import json
import os
import re
import subprocess
import sys
import tempfile

RB = {k: os.environ.get(k, "") for k in
      ("RB_NWO", "RB_PR_NUMBER", "RB_BASE", "RB_HEAD", "RB_AGENT",
       "RB_DIFF", "RB_PR_JSON", "RB_OUT", "RB_MODEL_BASE_URL", "RB_ATTEMPT")}
MODEL = os.environ.get("RB_CONFIG_MODEL") or "z-ai/glm-5.3"
BASE_URL = RB["RB_MODEL_BASE_URL"] or "https://integrate.api.nvidia.com/v1"
API_KEY = os.environ.get("NVIDIA_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
REPO = "/work/repo"
BUDGET_S = 1500  # keeps one hermes run under the 1800s per-PR default

PROMPT = """You are a meticulous senior code reviewer. Review pull request \
{pr_number} of {nwo}.

The repository is checked out at the PR head commit ({head}) at /work/repo. \
The exact change under review is the three-dot diff at {diff_path} \
(identical to what GitHub shows). Start by reading that diff, then read \
surrounding code in the repository to understand context before reporting.

Report ONLY real issues introduced or exposed by this change: bugs, security \
problems, race conditions, resource leaks, incorrect logic or edge cases, \
reliability and performance problems, and genuine maintainability or API-design \
concerns. Do not report pre-existing issues the change did not touch, \
stylistic trivia, or speculation you have not grounded in the code.

For each finding, confirm the file path (repo-relative) and the line numbers \
in the file at the HEAD commit (check the file, not the diff hunks). One entry \
per logical issue, spanning its full line range.

When done, reply with ONLY a JSON array (no prose around it), at most 15 \
entries, newest concern first:
[{{"file": "path/to/file.py", "start_line": 42, "end_line": 45, \
"message": "what is wrong and why it matters"}}]
If you find nothing real, reply with [].
""".format(pr_number=RB["RB_PR_NUMBER"], nwo=RB["RB_NWO"], head=RB["RB_HEAD"][:12],
           diff_path=RB["RB_DIFF"] or "/work/pr/diff.patch")


def load_pr_meta():
    try:
        with open(RB["RB_PR_JSON"]) as fh:
            return json.load(fh)
    except Exception:
        return {}


def run_hermes():
    home = tempfile.mkdtemp(prefix="hermes_home_")
    cfg = os.path.join(home, "config.yaml")
    with open(cfg, "w") as fh:
        fh.write("model:\n  provider: custom\n  base_url: %s\n  model: %s\n"
                 "  api_key: %s\nagent:\n  run_budget_seconds: %d\n"
                 % (BASE_URL, MODEL, API_KEY, BUDGET_S))
    env = dict(os.environ, HERMES_HOME=home, HOME=home,
               GIT_CONFIG_COUNT="1",
               GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0=REPO)
    cmd = ["hermes", "chat", "-q", PROMPT, "--oneshot", "-Q",
           "--format", "text", "--ignore-rules", "-t", "terminal",
           "--no-restore-cwd"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=BUDGET_S + 60, cwd=REPO, env=env)
    tail = (out.stdout or "") + "\n" + (out.stderr or "")
    if out.returncode != 0:
        sys.stderr.write("hermes rc=%d tail=%s\n" % (out.returncode, tail[-2000:]))
        return None
    return out.stdout


def extract_findings(text):
    """Pull the last JSON array out of the agent's answer."""
    candidates = re.findall(r"\[[\s\S]*?\](?=\s*(?:```|$))", text) or []
    candidates.append(text)
    for chunk in reversed(candidates):
        chunk = chunk.strip().strip("`")
        chunk = re.sub(r"^json\s*", "", chunk)
        try:
            parsed = json.loads(chunk)
        except Exception:
            continue
        if isinstance(parsed, list):
            return parsed
    # brace scan for a single object wrapped loosely
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return None


def normalize(items):
    seen, out_items = set(), []
    for it in items[:40]:
        if not isinstance(it, dict):
            continue
        f = str(it.get("file") or "").replace("\\", "/").lstrip("./").strip("/")
        msg = str(it.get("message") or "").strip()
        if not f or not msg:
            continue
        try:
            s = int(it.get("start_line") or it.get("end_line") or 1)
            e = int(it.get("end_line") or s)
        except (TypeError, ValueError):
            continue
        s, e = max(1, s), max(max(1, s), e)
        key = (f, s, e, msg[:80])
        if key in seen:
            continue
        seen.add(key)
        out_items.append({"file": f, "start_line": s, "end_line": e,
                          "message": msg, "producer": RB["RB_AGENT"] or "hermes"})
    return out_items


def main():
    if not API_KEY:
        sys.stderr.write("no model API key in environment\n")
        return 1
    answer = run_hermes()
    if answer is None:
        return 1
    items = extract_findings(answer)
    if items is None:
        # answer produced no parseable array; fail so the run retries rather
        # than silently reporting an empty review
        sys.stderr.write("no parseable findings array in agent answer\n"
                         + answer[-1500:])
        return 1
    meta = load_pr_meta()
    doc = {
        "pr": {
            "repo": meta.get("repo") or ("https://github.com/" + RB["RB_NWO"]),
            "pr_number": int(meta.get("pr_number") or RB["RB_PR_NUMBER"]),
            "base": meta.get("base") or RB["RB_BASE"],
            "head": meta.get("head") or RB["RB_HEAD"],
        },
        "agent": RB["RB_AGENT"] or "hermes",
        "findings": normalize(items),
    }
    # the contract fails a PR on head/number mismatch; prefer the injected truth
    doc["pr"]["head"] = RB["RB_HEAD"] or doc["pr"]["head"]
    doc["pr"]["pr_number"] = int(RB["RB_PR_NUMBER"] or doc["pr"]["pr_number"])
    tmp = RB["RB_OUT"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, RB["RB_OUT"])
    sys.stderr.write("wrote %d findings model=%s\n" % (len(doc["findings"]), MODEL))
    return 0


if __name__ == "__main__":
    sys.exit(main())

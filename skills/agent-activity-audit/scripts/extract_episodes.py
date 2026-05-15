#!/usr/bin/env python3
"""Extract target-tool "episodes" from a transcript: each call plus surrounding
context (recent user messages, preceding assistant text, tool result).

Usage:
  extract_episodes.py --target NAME <outdir> file1.jsonl file2.jsonl ...

Output: one markdown summary per input file written as
  <outdir>/<client>-<date>-<short>.md
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from pathlib import Path


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("text", "input_text") and "text" in item:
                    parts.append(item["text"])
                elif "content" in item:
                    parts.append(extract_text(item["content"]))
        return "\n".join(parts)
    return ""


def make_matchers(target: str) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    safe = re.escape(target)
    cli = re.compile(rf"\b{safe}d?\b", re.IGNORECASE)
    mcp = re.compile(rf"^mcp__{safe}__|^{safe}_[a-z]+$", re.IGNORECASE)
    skill = re.compile(rf"^{safe}$|^/{safe}$|{safe}-", re.IGNORECASE)
    return cli, mcp, skill


def is_target_call(name: str, inp, cli_re, mcp_re, skill_re) -> tuple[bool, str, str]:
    if not isinstance(name, str):
        return (False, "", "")
    if mcp_re.match(name):
        cmd = ""
        if isinstance(inp, dict):
            for k in ("query", "task_id", "title", "content", "command", "cmd"):
                if k in inp and isinstance(inp[k], str):
                    cmd = inp[k]
                    break
        return (True, "mcp", f"{name} {cmd}".strip())
    if name in ("Bash", "shell", "exec_command", "local_shell", "container.exec"):
        if isinstance(inp, dict):
            cmd = inp.get("command", inp.get("cmd", ""))
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if isinstance(cmd, str) and cli_re.search(cmd):
                return (True, "cli", cmd)
    if name in ("Skill", "skill", "AgentSkill"):
        if isinstance(inp, dict):
            sk = inp.get("skill") or inp.get("name") or ""
            if isinstance(sk, str) and skill_re.search(sk):
                return (True, "skill", f"/{sk} {inp.get('args', '')}".strip())
    return (False, "", "")


def process_file(path: Path, outdir: Path, target: str, cli_re, mcp_re, skill_re) -> dict:
    is_claude = "/.claude/" in str(path)
    client = "claude" if is_claude else "codex"

    last_user: deque[dict] = deque(maxlen=2)
    last_assistant_text = ""
    pending: dict[str, dict] = {}

    episodes: list[dict] = []
    session_date = ""
    session_branch = ""
    session_cwd = ""

    try:
        with path.open("r", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue

                ts = rec.get("timestamp", "")
                if not session_date and ts:
                    session_date = ts[:10]
                if rec.get("cwd"):
                    session_cwd = rec["cwd"]
                if rec.get("gitBranch"):
                    session_branch = rec["gitBranch"]

                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else None
                rtype = rec.get("type")
                if payload:
                    rtype = payload.get("type", rtype)
                rec_eff = payload if payload else rec

                msg = rec.get("message") if isinstance(rec.get("message"), dict) else None

                # User input
                if payload and payload.get("type") == "user_message":
                    text = payload.get("message", "") or ""
                    if text and not text.startswith("# AGENTS.md"):
                        last_user.append({"ts": ts, "text": text[:1500]})
                elif msg and msg.get("role") == "user":
                    text = extract_text(msg.get("content", ""))
                    if text and not text.startswith(("# AGENTS.md", "<INSTRUCTIONS>")):
                        last_user.append({"ts": ts, "text": text[:1500]})
                elif payload and payload.get("type") == "message" and payload.get("role") == "user":
                    text = extract_text(payload.get("content", ""))
                    if text and not text.startswith(("# AGENTS.md", "<INSTRUCTIONS>")):
                        last_user.append({"ts": ts, "text": text[:1500]})

                # Assistant text
                if msg and msg.get("role") == "assistant":
                    text = extract_text(msg.get("content", ""))
                    if text:
                        last_assistant_text = text[:600]
                if (
                    payload
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                ):
                    text = extract_text(payload.get("content", ""))
                    if text:
                        last_assistant_text = text[:600]
                if payload and payload.get("type") == "agent_message":
                    text = payload.get("message", "")
                    if text:
                        last_assistant_text = text[:600]

                # Tool call (Claude)
                if msg and isinstance(msg.get("content"), list):
                    for item in msg["content"]:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_use":
                            ok, cat, cmd = is_target_call(
                                item.get("name", ""),
                                item.get("input", {}),
                                cli_re,
                                mcp_re,
                                skill_re,
                            )
                            if ok:
                                tid = item.get("id", "")
                                pending[tid] = {
                                    "cat": cat,
                                    "cmd": cmd,
                                    "ts": ts,
                                    "user": list(last_user),
                                    "assistant": last_assistant_text,
                                }
                        elif item.get("type") == "tool_result":
                            tid = item.get("tool_use_id", "")
                            if tid in pending:
                                output = extract_text(item.get("content", ""))
                                is_err = item.get("is_error") is True
                                ep = pending.pop(tid)
                                ep["output"] = output[:3000]
                                ep["is_error"] = is_err or any(
                                    s in output.lower()[:400]
                                    for s in [
                                        "error",
                                        "failed",
                                        "traceback",
                                        "not authenticated",
                                        "unauthorized",
                                    ]
                                )
                                episodes.append(ep)

                # Tool call (Codex)
                if rtype == "function_call":
                    name = rec_eff.get("name", "")
                    args = rec_eff.get("arguments", rec_eff.get("input", {}))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"_raw": args}
                    if not isinstance(args, dict):
                        args = {}
                    ok, cat, cmd = is_target_call(name, args, cli_re, mcp_re, skill_re)
                    if ok:
                        call_id = rec_eff.get("call_id") or rec_eff.get("id", "")
                        pending[call_id] = {
                            "cat": cat,
                            "cmd": cmd,
                            "ts": ts,
                            "user": list(last_user),
                            "assistant": last_assistant_text,
                        }
                if rtype == "function_call_output":
                    call_id = rec_eff.get("call_id") or rec_eff.get("id", "")
                    if call_id in pending:
                        output = rec_eff.get("output", "")
                        if isinstance(output, dict):
                            output = (
                                output.get("content")
                                or output.get("output")
                                or json.dumps(output)[:3000]
                            )
                        if not isinstance(output, str):
                            output = str(output)
                        ep = pending.pop(call_id)
                        ep["output"] = output[:3000]
                        ep["is_error"] = (
                            any(
                                s in output.lower()[:600]
                                for s in [
                                    "✗",
                                    "error",
                                    "failed",
                                    "traceback",
                                    "not authenticated",
                                    "unauthorized",
                                    "process exited with code 1",
                                    "process exited with code 2",
                                ]
                            )
                            and "code 0" not in output.lower()[:400]
                        )
                        episodes.append(ep)
    except Exception as e:
        return {"path": str(path), "error": repr(e)}

    if not episodes:
        return {"path": str(path), "episodes": 0}

    short_id = path.stem.split("-")[-1] if "-" in path.stem else path.stem[:12]
    outname = f"{client}-{session_date or 'unknown'}-{short_id[:8]}.md"
    outpath = outdir / outname

    with outpath.open("w") as fp:
        fp.write(f"# {client} session {session_date} ({path.name})\n\n")
        fp.write(f"cwd: `{session_cwd}` | branch: `{session_branch}`\n")
        fp.write(f"target: `{target}`\n")
        fp.write(f"total episodes: {len(episodes)}\n")
        err_n = sum(1 for e in episodes if e.get("is_error"))
        fp.write(f"errored episodes: {err_n}\n\n")

        for i, ep in enumerate(episodes, 1):
            fp.write(f"\n## Episode {i} [{ep['cat']}] {ep['ts']}\n")
            for u in ep["user"]:
                fp.write(f"\n**User**: {u['text'][:600]}\n")
            if ep["assistant"]:
                fp.write(f"\n**Assistant text (preceding)**: {ep['assistant'][:300]}\n")
            fp.write(f"\n**Target call** ({ep['cat']}):\n```\n{ep['cmd'][:600]}\n```\n")
            if ep.get("is_error"):
                fp.write(f"\n**OUTPUT (ERROR)**:\n```\n{ep.get('output', '')[:1500]}\n```\n")
            else:
                fp.write(f"\n**Output**:\n```\n{ep.get('output', '')[:1500]}\n```\n")

    return {"path": str(path), "episodes": len(episodes), "errors": err_n, "out": str(outpath)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="Target tool name (e.g. 'sibyl').")
    parser.add_argument("outdir")
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cli_re, mcp_re, skill_re = make_matchers(args.target)

    for f in args.files:
        try:
            result = process_file(Path(f), outdir, args.target, cli_re, mcp_re, skill_re)
        except Exception as e:
            result = {"path": f, "error": repr(e)}
        print(json.dumps(result))


if __name__ == "__main__":
    main()

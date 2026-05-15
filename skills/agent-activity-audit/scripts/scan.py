#!/usr/bin/env python3
"""Scan a JSONL conversation transcript for target-tool activity.

Usage:
  scan.py --target NAME file1.jsonl file2.jsonl ...

Outputs one JSON line per input file with:
  path, client (claude|codex), session_date, total_events,
  target_cli_count, target_mcp_count, target_skill_count,
  target_total, tool_error_count, tool_error_samples,
  user_corrections_count, user_correction_samples,
  first_ts, last_ts, cwd, branch

Handles Claude tool_use schema and Codex response_item.payload.function_call schema.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def looks_like_error(text: str) -> bool:
    t = text.lower()
    return any(
        s in t
        for s in [
            "error",
            "failed",
            "traceback",
            "exception",
            "timed out",
            "unauthorized",
            "forbidden",
            "not found",
            "no such",
            "connection refused",
            "rate limit",
            "expired",
        ]
    )


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
    """Return (cli_regex, mcp_regex, skill_regex) for the given target name."""
    safe = re.escape(target)
    cli = re.compile(rf"\b{safe}d?\b", re.IGNORECASE)
    mcp = re.compile(rf"^mcp__{safe}__|^{safe}_[a-z]+$", re.IGNORECASE)
    skill = re.compile(rf"^{safe}$|^/{safe}$|{safe}-", re.IGNORECASE)
    return cli, mcp, skill


def is_target_tool_call(
    name: str,
    inp,
    cli_re: re.Pattern,
    mcp_re: re.Pattern,
    skill_re: re.Pattern,
) -> tuple[str, str]:
    if not isinstance(name, str):
        return ("", "")
    if mcp_re.match(name):
        cmd = ""
        if isinstance(inp, dict):
            for k in ("query", "task_id", "title", "content", "command", "cmd"):
                if k in inp and isinstance(inp[k], str):
                    cmd = inp[k][:200]
                    break
        return ("mcp", f"{name} {cmd}".strip())
    if name in ("Bash", "shell", "exec_command", "local_shell", "container.exec"):
        if isinstance(inp, dict):
            cmd = inp.get("command", inp.get("cmd", ""))
            if isinstance(cmd, list):
                cmd = " ".join(str(x) for x in cmd)
            if isinstance(cmd, str) and cli_re.search(cmd):
                return ("cli", cmd[:300])
    if name in ("Skill", "skill", "AgentSkill"):
        if isinstance(inp, dict):
            sk = inp.get("skill") or inp.get("name") or ""
            if isinstance(sk, str) and skill_re.search(sk):
                return ("skill", f"{sk} {inp.get('args', '')}"[:200])
    return ("", "")


def scan_file(path: Path, target: str, cli_re, mcp_re, skill_re) -> dict:
    client = "claude" if "/.claude/" in str(path) else "codex"
    out = {
        "path": str(path),
        "client": client,
        "size": path.stat().st_size,
        "total_events": 0,
        "target_cli_count": 0,
        "target_mcp_count": 0,
        "target_skill_count": 0,
        "target_tool_samples": [],
        "tool_error_count": 0,
        "tool_error_samples": [],
        "user_corrections_count": 0,
        "user_correction_samples": [],
        "first_ts": None,
        "last_ts": None,
        "cwd": None,
        "branch": None,
    }

    target_tool_ids: dict[str, str] = {}

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
                out["total_events"] += 1

                ts = rec.get("timestamp") or rec.get("created_at")
                if ts:
                    if out["first_ts"] is None:
                        out["first_ts"] = ts
                    out["last_ts"] = ts
                if not out["cwd"] and rec.get("cwd"):
                    out["cwd"] = rec["cwd"]
                if not out["branch"] and rec.get("gitBranch"):
                    out["branch"] = rec["gitBranch"]

                rtype = rec.get("type")
                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else None
                if payload:
                    rtype = payload.get("type", rtype)
                    rec_eff = payload
                else:
                    rec_eff = rec
                msg = rec.get("message") if isinstance(rec.get("message"), dict) else None

                # Claude format: tool_use / tool_result inside msg.content
                if msg and isinstance(msg.get("content"), list):
                    for item in msg["content"]:
                        if not isinstance(item, dict):
                            continue
                        itype = item.get("type")
                        if itype == "tool_use":
                            cat, sample = is_target_tool_call(
                                item.get("name", ""),
                                item.get("input", {}),
                                cli_re,
                                mcp_re,
                                skill_re,
                            )
                            if cat:
                                tid = item.get("id", "")
                                out[f"target_{cat}_count"] += 1
                                target_tool_ids[tid] = item.get("name", "")
                                if len(out["target_tool_samples"]) < 25:
                                    out["target_tool_samples"].append(
                                        {
                                            "cat": cat,
                                            "ts": ts,
                                            "name": item.get("name", ""),
                                            "snip": sample,
                                        }
                                    )
                        elif itype == "tool_result":
                            tid = item.get("tool_use_id", "")
                            content_text = extract_text(item.get("content", ""))
                            is_err = item.get("is_error") is True
                            if tid in target_tool_ids and (
                                is_err or looks_like_error(content_text[:500])
                            ):
                                out["tool_error_count"] += 1
                                if len(out["tool_error_samples"]) < 10:
                                    out["tool_error_samples"].append(
                                        {
                                            "ts": ts,
                                            "tool": target_tool_ids[tid],
                                            "snippet": content_text[:400],
                                        }
                                    )

                # Codex format: function_call / function_call_output
                if rtype == "function_call":
                    name = rec_eff.get("name", "")
                    args = rec_eff.get("arguments") or rec_eff.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"_raw": args[:200]}
                    cat, sample = is_target_tool_call(
                        name,
                        args if isinstance(args, dict) else {},
                        cli_re,
                        mcp_re,
                        skill_re,
                    )
                    if cat:
                        call_id = rec_eff.get("call_id") or rec_eff.get("id", "")
                        out[f"target_{cat}_count"] += 1
                        target_tool_ids[call_id] = (
                            name
                            + ":"
                            + (args.get("cmd", "")[:80] if isinstance(args, dict) else "")
                        )
                        if len(out["target_tool_samples"]) < 25:
                            out["target_tool_samples"].append(
                                {
                                    "cat": cat,
                                    "ts": ts,
                                    "name": name,
                                    "snip": sample,
                                }
                            )
                if rtype == "function_call_output":
                    call_id = rec_eff.get("call_id") or rec_eff.get("id", "")
                    output = rec_eff.get("output", "")
                    if isinstance(output, dict):
                        output = (
                            output.get("content")
                            or output.get("output")
                            or json.dumps(output)[:400]
                        )
                    if not isinstance(output, str):
                        output = str(output)[:400]
                    if call_id in target_tool_ids and looks_like_error(output[:500]):
                        out["tool_error_count"] += 1
                        if len(out["tool_error_samples"]) < 10:
                            out["tool_error_samples"].append(
                                {
                                    "ts": ts,
                                    "tool": target_tool_ids[call_id],
                                    "snippet": output[:400],
                                }
                            )

                # User messages — corrections / reactions
                user_text = ""
                if payload and payload.get("type") == "user_message":
                    user_text = payload.get("message", "") or ""
                elif payload and payload.get("type") == "message" and payload.get("role") == "user":
                    t = extract_text(payload.get("content", ""))
                    if (
                        t
                        and not t.lstrip().startswith(("# AGENTS.md", "<INSTRUCTIONS>"))
                        and len(t) < 6000
                    ):
                        user_text = t
                elif msg and msg.get("role") == "user":
                    t = extract_text(msg.get("content", ""))
                    if t and len(t) < 6000:
                        user_text = t

                if user_text:
                    low = user_text.lower()
                    if target.lower() in low and any(
                        k in low
                        for k in [
                            "don't",
                            "dont",
                            "stop",
                            "no ",
                            "wrong",
                            "instead",
                            "should have",
                            "use ",
                            "remember",
                            "skill",
                            "broken",
                            "not working",
                            "slow",
                            "didn't",
                            "didnt",
                            "hate",
                            "annoying",
                            "ugh",
                            "wtf",
                        ]
                    ):
                        out["user_corrections_count"] += 1
                        if len(out["user_correction_samples"]) < 5:
                            out["user_correction_samples"].append(
                                {
                                    "ts": ts,
                                    "snippet": user_text[:500],
                                }
                            )
    except Exception as e:
        out["scan_error"] = repr(e)

    out["target_total"] = (
        out["target_cli_count"] + out["target_mcp_count"] + out["target_skill_count"]
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        required=True,
        help="Target tool name (e.g. 'sibyl'). Used to build regex matchers.",
    )
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    cli_re, mcp_re, skill_re = make_matchers(args.target)

    for f in args.files:
        try:
            result = scan_file(Path(f), args.target, cli_re, mcp_re, skill_re)
        except Exception as e:
            result = {"path": f, "error": repr(e)}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

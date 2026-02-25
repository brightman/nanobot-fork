#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def sanitize_user_key(key: str | None) -> str:
    raw = (key or "").strip()
    if not raw:
        return "unknown"
    unsafe = '<>:"/\\|?*'
    for ch in unsafe:
        raw = raw.replace(ch, "_")
    raw = raw.replace("\n", "_").replace("\r", "_")
    return raw or "unknown"


def resolve_user_key(channel: str, sender_id: str | None) -> str:
    channel_norm = (channel or "").strip().lower()
    if channel_norm == "cli":
        return "cli"
    if channel_norm == "system":
        return "system"

    sid = (sender_id or "").strip()
    if not sid or sid.lower() in {"unknown", "user"}:
        return "unknown"

    if channel_norm == "telegram" and "|" in sid:
        sid = sid.split("|", 1)[0].strip() or sid

    return sanitize_user_key(f"{channel_norm}__{sid}")


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_user_dir(workspace: Path, channel: str, sender_id: str | None) -> tuple[Path, str]:
    user_key = resolve_user_key(channel, sender_id)
    udir = workspace / "memory" / "users" / user_key
    udir.mkdir(parents=True, exist_ok=True)
    return udir, user_key


def load_profile(profile_file: Path, template_file: Path) -> dict[str, Any]:
    if profile_file.exists():
        return json.loads(profile_file.read_text(encoding="utf-8"))
    return json.loads(template_file.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_history(history_file: Path, entry: str) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with history_file.open("a", encoding="utf-8") as f:
        f.write(f"[{now_ts()}] {entry.strip()}\n\n")


def merge_profile(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if k == "confidence" and isinstance(v, dict):
            conf = dict(out.get("confidence") or {})
            for ck, cv in v.items():
                conf[ck] = cv
            out["confidence"] = conf
        elif k in {"pains", "objections"} and isinstance(v, list):
            cleaned = [str(x).strip() for x in v if str(x).strip()]
            out[k] = cleaned
        elif k in {"company", "role", "budget_signal", "timeline", "next_step"}:
            out[k] = "" if v is None else str(v).strip()
        else:
            out[k] = v
    out["updated_at"] = now_iso()
    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SDR per-user profile manager")
    p.add_argument("--workspace", default="~/.nanobot/workspace")

    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ["resolve-key", "show-path", "ensure", "get-profile"]:
        s = sub.add_parser(name)
        s.add_argument("--channel", required=True)
        s.add_argument("--sender-id", default="")

    up = sub.add_parser("upsert-profile")
    up.add_argument("--channel", required=True)
    up.add_argument("--sender-id", default="")
    g = up.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", dest="json_text")
    g.add_argument("--file", dest="json_file")

    ah = sub.add_parser("append-history")
    ah.add_argument("--channel", required=True)
    ah.add_argument("--sender-id", default="")
    ah.add_argument("--entry", required=True)

    sn = sub.add_parser("set-next-step")
    sn.add_argument("--channel", required=True)
    sn.add_argument("--sender-id", default="")
    sn.add_argument("--next-step", required=True)

    return p.parse_args()


def main() -> int:
    args = parse_args()
    workspace = Path(args.workspace).expanduser().resolve()
    skill_dir = Path(__file__).resolve().parent.parent
    template_file = skill_dir / "templates" / "PROFILE.json"

    if args.cmd == "resolve-key":
        print(resolve_user_key(args.channel, args.sender_id))
        return 0

    udir, user_key = ensure_user_dir(workspace, args.channel, args.sender_id)
    profile_file = udir / "PROFILE.json"
    history_file = udir / "HISTORY.md"
    memory_file = udir / "MEMORY.md"

    if args.cmd == "show-path":
        print(f"USER_KEY={user_key}")
        print(f"USER_DIR={udir}")
        print(f"PROFILE_FILE={profile_file}")
        print(f"MEMORY_FILE={memory_file}")
        print(f"HISTORY_FILE={history_file}")
        return 0

    if args.cmd == "ensure":
        if not profile_file.exists():
            atomic_write_json(profile_file, load_profile(profile_file, template_file))
        if not history_file.exists():
            history_file.write_text("# User History\n\n", encoding="utf-8")
        if not memory_file.exists():
            memory_file.write_text("# User Memory\n\n", encoding="utf-8")
        print(f"USER_KEY={user_key}")
        print(f"PROFILE_FILE={profile_file}")
        return 0

    if args.cmd == "get-profile":
        data = load_profile(profile_file, template_file)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "upsert-profile":
        base = load_profile(profile_file, template_file)
        if args.json_text:
            patch = json.loads(args.json_text)
        else:
            patch = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        merged = merge_profile(base, patch)
        atomic_write_json(profile_file, merged)
        print(json.dumps(merged, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "append-history":
        append_history(history_file, args.entry)
        print(f"HISTORY_FILE={history_file}")
        return 0

    if args.cmd == "set-next-step":
        base = load_profile(profile_file, template_file)
        merged = merge_profile(base, {"next_step": args.next_step})
        atomic_write_json(profile_file, merged)
        append_history(history_file, f"next_step updated: {args.next_step}")
        print(json.dumps(merged, ensure_ascii=False, indent=2))
        return 0

    raise ValueError(f"unknown cmd: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())

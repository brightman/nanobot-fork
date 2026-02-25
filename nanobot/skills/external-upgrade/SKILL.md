---
name: external-upgrade
description: "Delegate nanobot core self-upgrade tasks to external nb-supervisor. Use for core code changes requiring isolated build, verify, deploy, and rollback."
metadata: {"nanobot":{"always":true}}
---

# External Upgrade Skill

Use this skill when the user requests self-upgrade of nanobot core behavior.

## Rule

- Do not implement core upgrade logic inside nanobot.
- Submit core-upgrade tasks to external supervisor and report task IDs/status.

## External Supervisor Path

`/Users/yong.feng/Bright/Project/nanobot/nb-supervisor/nb_supervisor.py`

## Commands

Submit core task:

```bash
python3 /Users/yong.feng/Bright/Project/nanobot/nb-supervisor/nb_supervisor.py \
  --workspace ~/.nanobot/workspace \
  --repo /Users/yong.feng/Bright/Project/nanobot/nanobot \
  submit --scope core --title "<short-title>" --prompt "<full-task>" --by "nanobot"
```

Check status:

```bash
python3 /Users/yong.feng/Bright/Project/nanobot/nb-supervisor/nb_supervisor.py \
  --workspace ~/.nanobot/workspace \
  --repo /Users/yong.feng/Bright/Project/nanobot/nanobot \
  status
```

## Skill Tasks (non-core)

- If request is purely skill installation/creation, handle directly in `workspace/skills/` and do not submit to external supervisor.
- If request touches core code, provider routing, channel runtime, or deployment behavior, submit to external supervisor.

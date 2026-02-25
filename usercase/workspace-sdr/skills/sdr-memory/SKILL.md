---
name: sdr-memory
description: Per-customer structured SDR profile (PROFILE.json) bound to per-user memory directory via canonical user_key.
always: true
---

# SDR Memory

Use this skill to persist structured sales context per customer and keep it aligned with per-user memory.

## Goal

For each customer session, record and maintain these fields in `PROFILE.json`:

- `company`
- `role`
- `pains`
- `budget_signal`
- `timeline`
- `objections`
- `next_step`

## Association Rule (user_key)

This skill uses the same user-key rules as nanobot core memory:

- `channel == "cli"` -> `user_key = "cli"`
- `channel == "system"` -> `user_key = "system"`
- empty/unknown sender -> `user_key = "unknown"`
- telegram sender like `7981415175|username` -> use numeric id -> `telegram__7981415175`
- otherwise -> `user_key = "<channel>__<sender_id>"` (sanitized for filesystem safety)

## Storage Layout

All customer-specific files are colocated under one user directory:

- `memory/users/<user_key>/PROFILE.json`
- `memory/users/<user_key>/MEMORY.md`
- `memory/users/<user_key>/HISTORY.md`

This prevents cross-customer memory mixing and keeps one source of truth per user.

## Commands

Base script:

```bash
SCRIPT=~/.nanobot/workspace/skills/sdr-memory/scripts/sdr_memory.py
```

Resolve user key:

```bash
python3 "$SCRIPT" resolve-key --channel <channel> --sender-id <sender_id>
```

Ensure per-user files:

```bash
python3 "$SCRIPT" ensure --channel <channel> --sender-id <sender_id>
```

Show current user paths:

```bash
python3 "$SCRIPT" show-path --channel <channel> --sender-id <sender_id>
```

Read structured profile:

```bash
python3 "$SCRIPT" get-profile --channel <channel> --sender-id <sender_id>
```

Update structured fields from JSON patch:

```bash
python3 "$SCRIPT" upsert-profile --channel <channel> --sender-id <sender_id> \
  --json '{"company":"ACME","role":"CTO","pains":["slow lead routing"],"next_step":"Book demo Friday"}'
```

Append a traceable event into user history:

```bash
python3 "$SCRIPT" append-history --channel <channel> --sender-id <sender_id> \
  --entry "Customer asked about pricing and SOC2 evidence."
```

Set next-step quickly:

```bash
python3 "$SCRIPT" set-next-step --channel <channel> --sender-id <sender_id> \
  --next-step "Send ROI template and propose Tue 10:00 demo"
```

## Per-turn Update Policy

At the end of each customer turn:

1. `ensure` user files.
2. Extract new evidence from the turn.
3. `upsert-profile` only changed fields (do not erase unknown fields).
4. `append-history` with one concise event line.
5. Keep `MEMORY.md` as human-readable narrative; keep `PROFILE.json` as machine-readable source.

## Guardrails

- Never read or write another `user_key` in a customer turn.
- Do not fabricate missing fields; leave unknown as empty.
- Use `next_step` as mandatory outbound action when possible.

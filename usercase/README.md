# AI SDR Usercase (nanobot)

This usercase demonstrates how to run nanobot as an AI SDR with per-customer isolated memory.

## Included updates

1. AI SDR scenario prompt and skills definition under `usercase/workspace-sdr/`:
- `AGENTS.md`: SDR operating policy, qualification fields, handoff rules
- `SOUL.md`: SDR identity, values, communication style
- `USER.md`: product profile for selling `nanobot` (Data Intelligence Lab @ HKU)
- `skills/sdr-memory/`: structured profile skill and scripts

2. Per-user memory + sales conversion demonstration:
- Structured customer profile fields are enforced in `PROFILE.json`:
  - `company`, `role`, `pains`, `budget_signal`, `timeline`, `objections`, `next_step`
- Data is isolated by user key under:
  - `memory/users/<user_key>/PROFILE.json`
  - `memory/users/<user_key>/MEMORY.md`
  - `memory/users/<user_key>/HISTORY.md`
- The SDR flow uses these fields to track qualification and advance conversion via explicit `next_step`.

## Quick test

```bash
cd nanobot
SCRIPT=usercase/workspace-sdr/skills/sdr-memory/scripts/sdr_memory.py

python3 "$SCRIPT" ensure --workspace usercase/workspace-sdr --channel telegram --sender-id "10001|alice"
python3 "$SCRIPT" upsert-profile --workspace usercase/workspace-sdr --channel telegram --sender-id "10001|alice" \
  --json '{"company":"ACME","role":"Head of Sales","pains":["manual follow-ups"],"budget_signal":"active","timeline":"this quarter","objections":["integration effort"],"next_step":"book 30-min demo"}'
python3 "$SCRIPT" get-profile --workspace usercase/workspace-sdr --channel telegram --sender-id "10001|alice"
```

## What and why

<!-- What changes, and the reason. The diff already shows what; explain why. -->

## Invariants

These are the reason Locum can exist without violating a vendor's terms. Confirm
the change keeps all four, or say which one it touches and why that is safe.

- [ ] Never reads credential files, keychains, or OAuth tokens
- [ ] Never calls a vendor API directly
- [ ] Only spawns the official `claude` / `codex` binaries
- [ ] Stays single-operator

## Testing

- [ ] `python3 test_oauth.py` passes
- [ ] `bash -n setup-tunnel.sh install-service.sh` passes

If you touched `_run`, `_child_env`, or the event parsers, the OAuth suite does
not cover you. Delegate something real and say what happened:

- Agent and version tested against:
- What you delegated:
- What `check_job` returned:

## If this fixes a bug

What was the misleading symptom? The Troubleshooting section exists because
three separate bugs all looked like "everything returns 404", and the symptom is
often more useful to the next person than the cause.

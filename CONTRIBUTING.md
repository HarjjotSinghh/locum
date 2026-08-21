# Contributing to Locum

Thanks for looking. Locum is small on purpose: one Python file, one test file,
two shell scripts. Keep it that way where you can.

## The four invariants

These are not style preferences. They are the reason this project can exist
without violating a vendor's terms, and a pull request that breaks one will be
closed regardless of how well it works.

1. **Never read credential files, keychains, or OAuth tokens.**
2. **Never call `api.anthropic.com` or `api.openai.com` directly.**
3. **Only ever spawn the official `claude` / `codex` binaries.**
4. **Single-operator.** One token, one allowlist of workspace roots.

If a feature seems to require breaking one, open an issue first and describe
what you are trying to do. There is usually another way, and if there isn't,
the answer is that Locum should not do it.

## Development setup

You need [uv](https://docs.astral.sh/uv/), and `claude` or `codex` signed in if
you want to exercise delegation.

```bash
git clone https://github.com/HarjjotSinghh/locum
cd locum
cp .env.example .env      # set a token and your workspace roots
set -a && source .env && set +a
uv run server.py
```

Dependencies are declared inline with [PEP 723](https://peps.python.org/pep-0723/)
at the top of `server.py`, so there is no lockfile and no virtualenv to manage.

## Running the tests

```bash
python3 test_oauth.py
```

Boots a throwaway server on port 8799 and exercises discovery, dynamic client
registration, the consent gate, PKCE enforcement, single-use codes, token
exchange, refresh, and an authenticated MCP `initialize`. Thirteen assertions,
stdlib only, no `claude` installed required.

Shell scripts are checked with `bash -n`:

```bash
bash -n setup-tunnel.sh install-service.sh
```

## Testing against a real client

The OAuth suite covers the auth layer. Delegation needs a real agent, so verify
by hand:

1. Start the server and a tunnel.
2. Register the connector in your MCP client.
3. Delegate something trivial, `"count the Python files here"`, and poll
   `check_job` until it reports `done`.

If you changed anything in `_run`, `_child_env`, or the event parsers, say in
the pull request which agent you tested against and what it returned.

## Pull requests

- One change per pull request. A bug fix and a refactor are two pull requests.
- Explain **why** in the commit message, not what. The diff shows what.
- If you fixed a bug that took real debugging, write down the misleading symptom
  as well as the cause. The Troubleshooting section of the README exists because
  three separate bugs all presented as "everything returns 404".
- Match the surrounding style. The code favours short functions, explicit names,
  and comments that explain a decision rather than restate the line below.
- No new dependencies without a reason that survives the question "could this be
  twenty lines of stdlib?"

## Reporting bugs

Use the issue templates. For anything that looks like it could be exploited, do
not open an issue at all: see [SECURITY.md](SECURITY.md).

Include your OS, the output of `claude --version` or `codex --version`, and the
`stderr_tail` from `check_job` if a delegated job failed. That field exists
specifically so bug reports can carry the real error.

## Code of conduct

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

# CLAUDE.md

Guidance for coding agents. `README.md` covers what the skill does and
`CONTRIBUTING.md` covers PR conventions — this file covers only what those, the
`Makefile` and the workflows leave out.

## Toolchain

This is a **uv** repo: `uv sync`, then `source .venv/bin/activate`.

Do not `pip install` into `.venv`. CI gates on `uv lock --check`, and an
out-of-band install is not reflected in `uv.lock`. After changing any dependency,
run `uv lock`.

Linting and tests run through **tomte**, which renders a canonical `tox.ini` at
invocation. Always `tomte tox -e <env>`; `tomte tox -l` lists the envs.

- **Bare `tox` silently does nothing.** It reads this repo's `tox.ini`, finds no
  `[tox]`/`[testenv]` sections, and exits 0 with empty output — indistinguishable
  from a pass.
- **Bare `tomte tox` with no `-e` runs the whole default envlist**, including the
  full cross-platform test matrix. Always pass `-e`.

The repo's `tox.ini` is an *extension* consumed by tomte — `[tomte-extensions]`,
`[pytest]`, mypy overrides, liccheck `[Authorized Packages]` — not a standalone
tox config.

## Verification

```bash
make format            # writes
make code-checks       # also writes: runs black and isort, not just linters
make security
make common-checks-1   # writes: calls tomte format-copyright
make common-checks-2
```

**Not every CI gate has a `make` target.** `liccheck` and
`check-third-party-hashes` are enforced in CI but are not in the common-checks
targets, so a branch can be green locally and red in CI. Run them directly
before pushing:

```bash
tomte tox -e liccheck
tomte tox -e check-third-party-hashes
```

If you touched `packages/`, run `make generators` before `make common-checks-1` —
package edits change IPFS hashes, and `check-hash` fails until
`autonomy packages lock` (the last step of `generators`) has rewritten
`packages/packages.json`.

`make abci-docstrings` and `make copyright` are named in `CONTRIBUTING.md` but
are not Makefile targets. Use `make generators` and
`tomte check-copyright --author valory`.

## Deliberate absences — do not "fix" these

**No `handlers.py`, and `check-handlers` is a no-op.** `funds_manager` is a
utility skill that does not speak ABCI/HTTP/signing, so there is no handler to
lint; `skill.yaml` declares `handlers: {}`. `check_handlers_ignores` in
`pyproject.toml` makes this explicit. The env runs in `make common-checks-2` and
is intentionally absent from CI. Do not add a handler or wire it into a workflow.

**No service and no agent packages.** `packages/packages.json` ships one dev
skill plus third-party dependencies. Service-shaped tooling — notably tomte's
`analyse-service` — does not apply here.

**`check-generate-all-protocols` is deliberately not wired** into the `Makefile`
or CI, though tomte renders it. Leave it that way.

**The tomte version is pinned in `pyproject.toml` and
`.github/workflows/common_checks.yaml`, in more than one place each.** Bump them
together, then run `uv lock`, or the installed tomte and the rendered tox config
disagree.

## Testing

Tests are scoped to the one dev skill and run across Python 3.10–3.14 on Linux,
macOS and Windows:

```bash
tomte tox -e py3.10-linux    # platform suffix: -linux / -darwin / -win
```

## Keep this file updated

If you hit a trap in this repo that cost you time — a command that does not work
as documented, a check that fails for reasons unrelated to your change, a local
failure CI never sees, or a CI failure you cannot reproduce locally — add it
above before you finish. The next agent has no memory of your run.

Verify before you write. Several entries here replaced plausible second-hand
claims that turned out not to hold for this repo. Prefer facts about this repo's
structure and workflow, which survive a tool upgrade, over specifics of how a
particular tool version currently behaves, which rot silently.

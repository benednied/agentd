# Development setup

The project targets Python 3.12 or newer and uses `uv` for locked dependency
management.

```bash
uv sync --frozen
uv run agentd --help
```

The test suite uses temporary Git repositories, SQLite databases, fake nodes and
quota pools, and fake processes. It does not invoke an LLM or real Codex process.

The package is under `src/agentd`; tests are organized by unit, scheduling,
application, integration, harness, deployment, runtime, and review-regression
behavior. Keep changes focused and preserve the existing Python API and state
compatibility unless a change explicitly documents otherwise.

The project uses `ty` for static type checking and Ruff for linting and formatting.
See [testing](testing.md) for the authoritative commands.

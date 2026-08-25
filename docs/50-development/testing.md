# Testing

Run the pass/fail local checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=agentd --cov-report=term-missing
```

Run the separate complexity inventory when changing lifecycle code:

```bash
uv run ruff check src/agentd --select C901
```

The complexity inventory is currently a documented nonzero baseline with eleven
lifecycle hotspots. It is not a pass/fail gate. Refactor those functions only when
ordering and crash invariants have direct regression coverage.

Focused deployment checks are available with:

```bash
uv run pytest tests/deployment -q
```

Before merging, report what was actually tested, including relevant manual
verification and any untested risk.

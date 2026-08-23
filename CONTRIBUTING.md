# Contributing

Oracle is currently a solo portfolio project, not actively seeking outside
contributions — but issues, bug reports, and ideas are welcome via the
[issue templates](.github/ISSUE_TEMPLATE).

If you'd like to send a pull request anyway:

1. Fork the repo and create a branch off `main`.
2. `pip install -e ".[dev]"` and make sure `pytest -v` passes before and after
   your change. For `web/` changes, `npm ci && npm run build` must also pass.
3. Keep changes scoped — a bug fix doesn't need surrounding refactors.
4. Open a PR using the template; describe what changed and why.

There's no formal code of conduct beyond the obvious: be respectful, and keep
discussion focused on the project.

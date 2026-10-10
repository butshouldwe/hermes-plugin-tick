# Contributing

Thanks for wanting to improve tick. Bug reports, fixes and new settings are
all welcome.

## Before you start

- For a bug, open an issue with the `[tick]` line you saw, what you expected,
  and your Hermes version (`hermes --version`).
- For a new behaviour or setting, open an issue first so we can agree on the
  shape. tick stays generic: a behaviour for one use case ships as a setting,
  never as a default.

## Proposing a change

1. Fork, branch from `main`, and keep the pull request to one concern.
2. Add a test that fails without your change. In the pull request, say how
   you saw it fail.
3. Declare any new setting in `plugin.yaml` and document it in `README.md`.
4. Run the checks:

   ```bash
   python3 -m pytest -q tests/
   node --test 'tests/js/*.test.mjs'
   hermes plugins validate .
   ```

5. Open the pull request against `main`. CI runs the same checks, with
   `hermes plugins validate` inside the pinned Hermes image. CI also fails on
   any validate warning, for example a declared tool that `register()` never
   registers.

Commit messages use conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`,
`ci:`, `chore:`) with a short imperative subject.

## How your pull request lands

Maintainers review and merge in our own review system, then publish the
result to GitHub. Your commits keep your name and email as author. When the
change lands, we close the pull request with a link to the commit on `main`.
If we need changes, we ask on the pull request.

`AGENTS.md` and `docs/design.md` explain the rules the code follows. Coding
agents should read `AGENTS.md` first.

## License

By contributing, you agree that your contribution is licensed under the MIT
License in `LICENSE`.

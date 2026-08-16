# Git hooks

Secret-scanning hooks (`pre-commit`, `pre-push`) that run [gitleaks](https://github.com/gitleaks/gitleaks)
via Docker — no local install needed beyond Docker itself. Rules live in
[`.gitleaks.toml`](../.gitleaks.toml) at the repo root.

Git doesn't enable tracked hook scripts automatically (that'd let a clone
run arbitrary code on checkout), so each clone needs to opt in once:

```sh
git config core.hooksPath .githooks
```

- `pre-commit` scans staged changes only — fast, catches secrets before they
  ever enter history.
- `pre-push` scans the full repo history as a second safety net (e.g. if a
  commit was made with `--no-verify`).

Bypass in an emergency with `git commit --no-verify` / `git push --no-verify`.

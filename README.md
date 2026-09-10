# Git Pull All

Update many git repositories under one parent directory in one pass.

For each repo, the tool updates:

- the **currently checked-out branch** (if it has an upstream), and
- local **trunk** branches that track a remote (`main`, `master`, and `develop` by default)

It skips unnecessary network work when local remote-tracking refs already match the remote (confirmed with `ls-remote`), then uses a targeted fetch and merges/rebases from the local upstream ref (no second fetch on update).

## Requirements

- **Python** 3.9+ (uses `list[str]` / `dict[str, …]` style annotations and `str.removeprefix`)
- **Git** available on `PATH`
- Network access and credentials for your remotes (SSH agent, HTTPS credential helper, etc.)
- Optional **TOML config**: Python 3.11+ (`tomllib`) or install `tomli` on 3.9/3.10

Runtime has **no third-party Python dependencies**. Dev/test deps are listed in `requirements-dev.txt`.

## Setup

Clone this repository (it is independent of the playground repo):

```bash
git clone https://github.com/MYOB-TomH/git-pull-all.git
cd git-pull-all
```

The repository is private, so you need access to `MYOB-TomH/git-pull-all` and a GitHub credential that can read it (SSH agent, `gh auth`, or HTTPS credential helper).

```bash
# Optional but recommended: isolate test tooling
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install test dependencies
pip install -r requirements-dev.txt
```

Make the script executable if you prefer running it directly:

```bash
chmod +x git_pull_all.py
```

## Usage

Scan the current directory’s immediate subfolders:

```bash
python3 git_pull_all.py
# or
./git_pull_all.py
```

Scan a specific parent directory (typical layout: `~/code/repo-a`, `~/code/repo-b`, …):

```bash
python3 git_pull_all.py -d ~/code
```

### Flag examples

| Goal | Example |
|------|---------|
| Preview without changing anything | `python3 git_pull_all.py -d ~/code --dry-run` |
| Parallel workers (default is `min(8, max(4, CPU count))`) | `python3 git_pull_all.py -d ~/code -p 8` |
| Serial / easier to read output | `python3 git_pull_all.py -d ~/code -p 1` |
| Fast-forward only; skip diverged trunks | `python3 git_pull_all.py -d ~/code --ff-only` |
| Rebase current branch onto upstream | `python3 git_pull_all.py -d ~/code --rebase` |
| Custom trunk branches | `python3 git_pull_all.py -d ~/code --trunk main --trunk release` |
| Only some repos (basename globs) | `python3 git_pull_all.py -d ~/code --only 'myob-*' --only payments` |
| Skip noisy repos | `python3 git_pull_all.py -d ~/code --exclude 'scratch-*' --exclude temp` |
| Don’t auto-stash dirty tracked files | `python3 git_pull_all.py -d ~/code --no-stash` |
| Quieter success lines (skips numstat; uses name-only) | `python3 git_pull_all.py -d ~/code -q` |
| Skip ls-remote; always targeted-fetch | `python3 git_pull_all.py -d ~/code --fetch-always` |
| Skip network if FETCH_HEAD is recent | `python3 git_pull_all.py -d ~/code --max-age 900` |
| Retry failed fetches | `python3 git_pull_all.py -d ~/code --retry 2` |
| JSON summary on stdout | `python3 git_pull_all.py -d ~/code --json` |
| Write JSON summary to a file | `python3 git_pull_all.py -d ~/code --json-file /tmp/pull.json` |
| Print every git command | `python3 git_pull_all.py -d ~/code -v` |
| Disable ANSI colors | `python3 git_pull_all.py -d ~/code --no-color` |

`--ff-only` and `--rebase` are mutually exclusive.

Full help:

```bash
python3 git_pull_all.py --help
```

## Config file

Optional TOML defaults (CLI flags always win):

1. `~/.config/git-pull-all.toml`
2. `<scan-directory>/.git-pull-all.toml`

Example:

```toml
directory = "~/code"
parallel = 8
trunk = ["main", "develop"]
exclude = ["scratch-*"]
max_age = 900
retry = 1
ff_only = true
```

Supported keys: `directory`, `parallel`, `trunk`, `only`, `exclude`, `dry_run`, `ff_only`, `rebase`, `no_stash`, `quiet`, `verbose`, `fetch_always`, `max_age`, `retry`, `no_color`, `json`, `json_file`.

## SSH multiplexing (same host)

For ~20 repos on one host, reusing SSH connections often beats raising `-p`. Add something like this to `~/.ssh/config`:

```sshconfig
Host github.com
  ControlMaster auto
  ControlPath ~/.ssh/cm-%r@%h:%p
  ControlPersist 10m
```

Adjust `Host` to your Git host. No script changes are required.

## Behaviour

### Discovery

Only **immediate** subdirectories of `-d` / the current directory are considered. A folder counts as a git repo if it contains a `.git` directory or gitfile (including linked worktrees). Nested repos deeper than one level are not scanned.

`--only` / `--exclude` match the repository **basename** with shell-style globs (`fnmatch`).

### Branch selection

Per repo, selected branches are:

1. the current branch, if it has upstream tracking, and
2. any configured trunk branch that exists locally **and** has upstream tracking

Default trunks: `main`, `master`, `develop`. Override with repeated `--trunk` flags (replaces the default set).

Tracking status is queried only for `{current} ∪ trunks` (not every local branch).

### Skip-fetch optimisation

1. Read local ahead/behind from one scoped `for-each-ref` walk.
2. If nothing looks behind and `--max-age` is set with a fresh `FETCH_HEAD`, **skip network**.
3. Else if nothing looks behind and `--fetch-always` is set, targeted-fetch.
4. Else if nothing looks behind, confirm with `git ls-remote` against the upstream tips.
5. If everything still matches, **skip fetch and updates** for that repo.
6. Otherwise run a **targeted** `git fetch --prune <remote> <refs…>` (with optional `--retry`), then update only branches that are behind.

### Updating branches

- **Current branch:** merge (default), `merge --ff-only`, or rebase onto the already-fetched upstream ref.
- **Other trunk branches:** fast-forward the local ref with `update-ref` when possible (no checkout). If that is not possible and `--ff-only` is set, the branch is skipped; otherwise the tool checks out, updates, and restores your original branch.

### Dirty working trees

By default, tracked (and when needed, blocking untracked) changes are **stashed** before a mutating update, then restored with `stash pop` if the update succeeds. With `--no-stash`, repos that have uncommitted tracked changes are skipped with an error instead.

If an update fails after a stash, the stash is left in place and you are told to run `git stash pop` manually.

### Output and exit codes

Per-repo output is buffered and flushed when each repo finishes (cleaner with `-p` > 1). The summary includes per-repo timings.

| Code | Meaning |
|------|---------|
| `0` | All processed repos succeeded |
| `1` | Total failure (or interrupt with no usable partial success) |
| `2` | Partial success (some repos/branches updated or skipped, some failed) |

## Project layout

| Path | Role |
|------|------|
| `git_pull_all.py` | CLI entrypoint, orchestration, printing |
| `git_pull_lib.py` | Git helpers, branch selection, result types, config |
| `tests/` | Unit tests (mocked git runner + formatting/exit-code helpers) |
| `requirements-dev.txt` | pytest for local development |

## Running tests

From the repository root (with the venv activated if you use one):

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

Run a single module or test:

```bash
python3 -m pytest tests/test_git_pull_lib.py -q
python3 -m pytest tests/test_git_pull_all.py::test_exit_code_partial_success -q
```

Verbose failure output:

```bash
python3 -m pytest tests/ -vv
```

Tests do not need network access; git commands are mocked.

## Security notes

- Git is invoked with explicit argument lists only (no shell).
- Repo paths come from directory listing and basename globs—not from free-form shell paths in patterns.
- Config values are validated (ranges/types); CLI overrides config.
- JSON output does not include remote URLs (avoids leaking embedded credentials in summaries).
- Verbose mode prints command argv only; do not expect it to redact remote URLs that contain embedded credentials (prefer SSH or a credential helper).
- Auth to remotes is entirely your environment’s responsibility (SSH keys, tokens, etc.).

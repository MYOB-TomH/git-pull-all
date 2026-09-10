"""Core helpers for git_pull_all — branch selection, git operations, and result tracking."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

DEFAULT_TRUNK_BRANCHES = frozenset({"main", "master", "develop"})
# Network-bound: prefer a modest parallel default over CPU count alone.
DEFAULT_PARALLEL_WORKERS = min(8, max(4, os.cpu_count() or 4))

CONFIG_BASENAME = ".git-pull-all.toml"
USER_CONFIG_PATH = os.path.expanduser("~/.config/git-pull-all.toml")

_TRACK_BEHIND_RE = re.compile(r"behind (\d+)")
_TRACK_GONE_RE = re.compile(r"\[gone\]")

GitRunner = Callable[[List[str], str], Tuple[bool, str, str]]


@dataclass
class GitError:
    """Information about an error encountered during git operations."""

    repo_path: str
    branch: str
    error_type: str
    message: str


@dataclass(frozen=True)
class FileChange:
    """Line change stats for a single file between two commits."""

    path: str
    insertions: int
    deletions: int


@dataclass
class RepoResult:
    """Outcome of processing a single repository."""

    repo_path: str
    updated: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    interrupted: bool = False
    changes_by_branch: Dict[str, List[FileChange]] = field(default_factory=dict)
    duration_seconds: Optional[float] = None

    @property
    def success(self) -> bool:
        return not self.failed and not self.interrupted

    @property
    def partial(self) -> bool:
        return bool(self.failed) and bool(self.updated or self.skipped)

    @property
    def files_changed_by_branch(self) -> Dict[str, int]:
        return {branch: len(changes) for branch, changes in self.changes_by_branch.items()}

    @property
    def total_files_changed(self) -> int:
        return sum(len(changes) for changes in self.changes_by_branch.values())


@dataclass
class PullOptions:
    """Runtime options affecting pull behavior."""

    dry_run: bool = False
    ff_only: bool = False
    rebase: bool = False
    no_stash: bool = False
    quiet: bool = False
    verbose: bool = False
    trunk_branches: frozenset[str] = DEFAULT_TRUNK_BRANCHES
    fetch_always: bool = False
    max_age: Optional[int] = None
    retry: int = 0


def default_git_runner(command: List[str], cwd: str, timeout: int = 300) -> Tuple[bool, str, str]:
    """Execute a git command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout} seconds"
    except OSError as exc:
        return False, "", str(exc)


def parse_upstream(remote_branch: str) -> Tuple[str, str]:
    """Parse 'origin/main' into ('origin', 'main')."""
    remote, _, ref = remote_branch.partition("/")
    return remote, ref


def parse_upstream_track(track: str) -> Tuple[Optional[int], bool]:
    """
    Parse %(upstream:track) into (behind_count, ok).

    Empty / ahead-only => behind 0. '[gone]' or unknown => (None, False).
    """
    track = track.strip()
    if not track:
        return 0, True
    if _TRACK_GONE_RE.search(track):
        return None, False
    match = _TRACK_BEHIND_RE.search(track)
    if match:
        return int(match.group(1)), True
    if "ahead" in track:
        return 0, True
    return None, False


def path_is_git_repo(path: str) -> bool:
    """True if path has a .git directory or gitfile (worktree/submodule)."""
    git_path = os.path.join(path, ".git")
    return os.path.isdir(git_path) or os.path.isfile(git_path)


def select_branches_to_update(
    tracking_branches: Dict[str, str],
    current_branch: str,
    trunk_branches: frozenset[str] = DEFAULT_TRUNK_BRANCHES,
) -> Dict[str, str]:
    """Return current branch plus local trunk branches that have upstream tracking."""
    names = {current_branch} | (trunk_branches & tracking_branches.keys())
    return {branch: tracking_branches[branch] for branch in sorted(names) if branch in tracking_branches}


def describe_targeted_fetch(branches: Dict[str, str]) -> str:
    """Human-readable summary of targeted fetch commands."""
    refs_by_remote: Dict[str, Set[str]] = {}
    for remote_branch in branches.values():
        remote, ref = parse_upstream(remote_branch)
        refs_by_remote.setdefault(remote, set()).add(ref)
    parts = [
        f"git fetch --prune {remote} {' '.join(sorted(refs))}"
        for remote, refs in sorted(refs_by_remote.items())
    ]
    return "; ".join(parts)


def filter_repos(
    repos: List[str],
    only_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> List[str]:
    """Filter repositories by basename glob patterns."""
    filtered = repos

    if only_patterns:
        filtered = [
            repo
            for repo in filtered
            if any(fnmatch.fnmatch(os.path.basename(repo), pattern) for pattern in only_patterns)
        ]

    if exclude_patterns:
        filtered = [
            repo
            for repo in filtered
            if not any(fnmatch.fnmatch(os.path.basename(repo), pattern) for pattern in exclude_patterns)
        ]

    return filtered


def candidate_tracking_branches(
    current_branch: str,
    trunk_branches: frozenset[str] = DEFAULT_TRUNK_BRANCHES,
) -> List[str]:
    """Local branch names to query for tracking status (current + configured trunks)."""
    return sorted({current_branch} | set(trunk_branches))


def parse_ls_remote_output(stdout: str) -> Dict[str, str]:
    """Parse ls-remote output into ref_name -> sha."""
    remote_shas: Dict[str, str] = {}
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        sha, ref_path = line.split("\t", 1)
        remote_shas[ref_path.removeprefix("refs/heads/")] = sha
    return remote_shas


def _load_toml_file(path: str) -> Dict:
    """Load a TOML file. Returns {} if tomllib/tomli unavailable or file unreadable."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}

    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def config_search_paths(scan_directory: str) -> List[str]:
    """Return config paths in merge order (user then local)."""
    paths: List[str] = []
    if os.path.isfile(USER_CONFIG_PATH):
        paths.append(USER_CONFIG_PATH)
    local = os.path.join(scan_directory, CONFIG_BASENAME)
    if os.path.isfile(local):
        paths.append(local)
    return paths


def load_merged_config(scan_directory: str) -> Dict:
    """
    Load and merge config files for scan_directory.

    Later files override earlier keys. Values are lightly validated/normalized.
    """
    merged: Dict = {}
    for path in config_search_paths(scan_directory):
        merged.update(_load_toml_file(path))
    return normalize_config(merged)


def normalize_config(raw: Dict) -> Dict:
    """Validate and normalize config keys used by the CLI."""
    out: Dict = {}

    if "directory" in raw and isinstance(raw["directory"], str) and raw["directory"].strip():
        out["directory"] = os.path.expanduser(raw["directory"].strip())

    if "parallel" in raw:
        try:
            parallel = int(raw["parallel"])
            if parallel >= 1:
                out["parallel"] = parallel
        except (TypeError, ValueError):
            pass

    if "trunk" in raw:
        trunks = raw["trunk"]
        if isinstance(trunks, str):
            trunks = [trunks]
        if isinstance(trunks, list) and all(isinstance(t, str) and t.strip() for t in trunks):
            out["trunk"] = [t.strip() for t in trunks]

    for list_key in ("only", "exclude"):
        if list_key in raw:
            values = raw[list_key]
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values):
                out[list_key] = [v.strip() for v in values]

    for bool_key in (
        "dry_run",
        "ff_only",
        "rebase",
        "no_stash",
        "quiet",
        "verbose",
        "fetch_always",
        "no_color",
        "json",
    ):
        if bool_key in raw and isinstance(raw[bool_key], bool):
            out[bool_key] = raw[bool_key]

    if "max_age" in raw:
        try:
            max_age = int(raw["max_age"])
            if max_age >= 0:
                out["max_age"] = max_age
        except (TypeError, ValueError):
            pass

    if "retry" in raw:
        try:
            retry = int(raw["retry"])
            if retry >= 0:
                out["retry"] = retry
        except (TypeError, ValueError):
            pass

    if "json_file" in raw and isinstance(raw["json_file"], str) and raw["json_file"].strip():
        out["json_file"] = os.path.expanduser(raw["json_file"].strip())

    return out


def peek_directory_arg(argv: Optional[List[str]] = None) -> Optional[str]:
    """Extract -d/--directory from argv without full argparse (for config discovery)."""
    args = list(sys.argv[1:] if argv is None else argv)
    for i, arg in enumerate(args):
        if arg in ("-d", "--directory") and i + 1 < len(args):
            return os.path.expanduser(args[i + 1])
        if arg.startswith("--directory="):
            return os.path.expanduser(arg.split("=", 1)[1])
        if arg.startswith("-d=") and len(arg) > 3:
            return os.path.expanduser(arg[3:])
    return None


def results_to_summary_dict(
    errors: List[GitError],
    results: List[RepoResult],
    *,
    dry_run: bool = False,
) -> Dict:
    """Build a JSON-serializable summary (no remote URLs / secrets)."""
    sorted_results = sorted(results, key=lambda r: os.path.basename(r.repo_path).lower())
    return {
        "dry_run": dry_run,
        "repos": [
            {
                "path": r.repo_path,
                "name": os.path.basename(r.repo_path),
                "updated": list(r.updated),
                "skipped": list(r.skipped),
                "failed": list(r.failed),
                "interrupted": r.interrupted,
                "duration_seconds": r.duration_seconds,
                "files_changed_by_branch": dict(r.files_changed_by_branch),
                "total_files_changed": r.total_files_changed,
            }
            for r in sorted_results
        ],
        "errors": [
            {
                "repo": os.path.basename(e.repo_path),
                "path": e.repo_path,
                "branch": e.branch,
                "type": e.error_type,
                "message": e.message,
            }
            for e in errors
        ],
        "totals": {
            "repos": len(results),
            "branches_updated": sum(len(r.updated) for r in results),
            "files_changed": sum(r.total_files_changed for r in results),
            "skipped": sum(len(r.skipped) for r in results),
            "failed": sum(len(r.failed) for r in results),
        },
    }


class GitRepo:
    """Git operations for a single repository with injectable runner."""

    def __init__(self, repo_path: str, runner: GitRunner = default_git_runner, options: Optional[PullOptions] = None):
        self.repo_path = repo_path
        self.runner = runner
        self.options = options or PullOptions()

    def run(self, command: List[str]) -> Tuple[bool, str, str]:
        return self.runner(command, self.repo_path)

    def is_git_repo(self) -> bool:
        return path_is_git_repo(self.repo_path)

    def get_current_branch(self) -> Optional[str]:
        success, stdout, _ = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        if success and stdout and stdout != "HEAD":
            return stdout
        return None

    def rev_parse(self, ref: str) -> Optional[str]:
        success, stdout, _ = self.run(["git", "rev-parse", ref])
        if success and stdout:
            return stdout
        return None

    def rev_parse_many(self, refs: List[str]) -> Optional[List[str]]:
        """Resolve multiple refs in one call. Returns one SHA per ref, or None on failure."""
        if not refs:
            return []
        success, stdout, _ = self.run(["git", "rev-parse", *refs])
        if not success:
            return None
        lines = stdout.split("\n") if stdout else []
        if len(lines) != len(refs):
            return None
        return lines

    def _parse_numstat(self, stdout: str) -> List[FileChange]:
        changes: List[FileChange] = []
        for line in stdout.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            add_raw, del_raw, path = parts
            insertions = 0 if add_raw == "-" else int(add_raw)
            deletions = 0 if del_raw == "-" else int(del_raw)
            changes.append(FileChange(path=path, insertions=insertions, deletions=deletions))
        return sorted(changes, key=lambda c: c.path)

    def get_file_changes(self, old_sha: str, new_sha: str) -> List[FileChange]:
        if not old_sha or not new_sha or old_sha == new_sha:
            return []
        success, stdout, _ = self.run(["git", "diff", "--numstat", f"{old_sha}..{new_sha}"])
        if not success or not stdout:
            return []
        return self._parse_numstat(stdout)

    def get_changed_paths(self, old_sha: str, new_sha: str) -> List[FileChange]:
        """List changed paths without line stats (cheaper than numstat; used for -q)."""
        if not old_sha or not new_sha or old_sha == new_sha:
            return []
        success, stdout, _ = self.run(["git", "diff", "--name-only", f"{old_sha}..{new_sha}"])
        if not success or not stdout:
            return []
        paths = sorted(path for path in stdout.split("\n") if path)
        return [FileChange(path=path, insertions=0, deletions=0) for path in paths]

    def count_changed_files(self, old_sha: str, new_sha: str) -> int:
        return len(self.get_file_changes(old_sha, new_sha))

    def git_path(self, path: str) -> Optional[str]:
        """Resolve a path inside the git directory (works for linked worktrees)."""
        success, stdout, _ = self.run(["git", "rev-parse", "--git-path", path])
        if not success or not stdout:
            return None
        resolved = stdout
        if not os.path.isabs(resolved):
            resolved = os.path.join(self.repo_path, resolved)
        return resolved

    def fetch_cache_fresh(self, max_age_seconds: int, now: Optional[float] = None) -> bool:
        """
        True if FETCH_HEAD exists and its mtime is within max_age_seconds.

        Used with --max-age to skip ls-remote/fetch when a recent fetch already ran.
        """
        if max_age_seconds < 0:
            return False
        fetch_head = self.git_path("FETCH_HEAD")
        if not fetch_head or not os.path.isfile(fetch_head):
            return False
        try:
            mtime = os.path.getmtime(fetch_head)
        except OSError:
            return False
        current = time.time() if now is None else now
        return (current - mtime) <= max_age_seconds

    def _parse_for_each_ref_tracking(
        self, stdout: str
    ) -> Tuple[Dict[str, str], Dict[str, int], Set[str]]:
        """Parse for-each-ref tracking output into (tracking, behind, indeterminate_branches)."""
        tracking: Dict[str, str] = {}
        behind: Dict[str, int] = {}
        indeterminate_branches: Set[str] = set()

        for line in stdout.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not parts[1]:
                continue
            branch, upstream = parts[0], parts[1]
            track = parts[2] if len(parts) > 2 else ""
            tracking[branch] = upstream
            count, ok = parse_upstream_track(track)
            if not ok or count is None:
                indeterminate_branches.add(branch)
            elif count > 0:
                behind[branch] = count

        return tracking, behind, indeterminate_branches

    def list_branch_tracking_status(
        self, branches: Optional[Dict[str, str]] = None
    ) -> Tuple[Dict[str, str], Dict[str, int], Set[str]]:
        """
        One for-each-ref walk returning (tracking, behind, indeterminate_branches).

        If branches is provided, only those local branch names are queried.
        """
        command = [
            "git",
            "for-each-ref",
            "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
        ]
        if branches is not None:
            if not branches:
                return {}, {}, set()
            command.extend(f"refs/heads/{name}" for name in branches)
        else:
            command.append("refs/heads/")

        success, stdout, _ = self.run(command)
        if not success:
            if branches:
                return {}, {}, set(branches)
            return {}, {}, set()

        return self._parse_for_each_ref_tracking(stdout)

    def get_branches_with_tracking(self) -> Dict[str, str]:
        tracking, _, _ = self.list_branch_tracking_status()
        return tracking

    def commits_behind(self, branch: str, remote_branch: str) -> Optional[int]:
        success, stdout, _ = self.run(["git", "rev-list", "--count", f"{branch}..{remote_branch}"])
        if not success:
            return None
        try:
            return int(stdout)
        except ValueError:
            return None

    def branches_behind(self, branches: Dict[str, str]) -> Tuple[Dict[str, int], bool]:
        _, behind, indeterminate_branches = self.list_branch_tracking_status(branches)
        filtered = {branch: count for branch, count in behind.items() if branch in branches}
        indeterminate = bool(indeterminate_branches & set(branches))
        return filtered, indeterminate

    def check_remote_freshness(self, branches: Dict[str, str]) -> Tuple[bool, bool]:
        """Return (all_match, check_succeeded)."""
        by_remote: Dict[str, List[Tuple[str, str, str]]] = {}
        for _local, remote_branch in branches.items():
            remote, ref = parse_upstream(remote_branch)
            by_remote.setdefault(remote, []).append((_local, remote_branch, ref))

        for remote, refs in by_remote.items():
            ref_paths = [f"refs/heads/{ref}" for _, _, ref in refs]
            success, stdout, _ = self.run(["git", "ls-remote", remote, *ref_paths])
            if not success:
                return False, False

            remote_shas = parse_ls_remote_output(stdout)
            for _, remote_branch, ref in refs:
                if ref not in remote_shas:
                    return False, True

            remote_branch_names = [remote_branch for _, remote_branch, _ in refs]
            local_shas = self.rev_parse_many(remote_branch_names)
            if local_shas is None:
                return False, False

            for (_, remote_branch, ref), local_sha in zip(refs, local_shas):
                if local_sha != remote_shas[ref]:
                    return False, True

        return True, True

    def targeted_fetch(self, branches: Dict[str, str]) -> Tuple[bool, str]:
        refs_by_remote: Dict[str, Set[str]] = {}
        for remote_branch in branches.values():
            remote, ref = parse_upstream(remote_branch)
            refs_by_remote.setdefault(remote, set()).add(ref)

        for remote, refs in refs_by_remote.items():
            success, _, stderr = self.run(["git", "fetch", "--prune", remote, *sorted(refs)])
            if not success:
                return False, stderr
        return True, ""

    def targeted_fetch_with_retry(
        self,
        branches: Dict[str, str],
        retries: int = 0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> Tuple[bool, str]:
        """Run targeted_fetch, retrying on failure with exponential backoff (1s, 2s, 4s, …)."""
        attempts = max(0, retries) + 1
        last_stderr = ""
        for attempt in range(attempts):
            success, stderr = self.targeted_fetch(branches)
            if success:
                return True, ""
            last_stderr = stderr
            if attempt < attempts - 1:
                sleep_fn(float(2**attempt))
        return False, last_stderr

    def has_uncommitted_changes(self) -> bool:
        """True if there are staged or unstaged tracked changes (untracked ignored)."""
        success, stdout, _ = self.run(["git", "status", "--porcelain=v1", "-uno"])
        if not success:
            return True
        return bool(stdout.strip())

    def list_untracked_files(self) -> Set[str]:
        success, untracked_out, _ = self.run(["git", "ls-files", "--others", "--exclude-standard"])
        if not success or not untracked_out.strip():
            return set()
        return {path for path in untracked_out.split("\n") if path}

    def untracked_would_block_checkout(self, branch: str) -> Set[str]:
        """Return untracked paths that would block checkout of branch."""
        untracked = self.list_untracked_files()
        if not untracked:
            return set()

        success, branch_files_out, _ = self.run(["git", "ls-tree", "-r", "--name-only", branch])
        if not success:
            return set()

        branch_files = set(branch_files_out.split("\n")) if branch_files_out else set()
        return branch_files & untracked

    def stash_changes(
        self,
        repo_name: str,
        include_untracked: bool = False,
        *,
        has_tracked: Optional[bool] = None,
    ) -> Tuple[bool, bool]:
        if has_tracked is None:
            has_tracked = self.has_uncommitted_changes()

        has_untracked = False
        if include_untracked:
            has_untracked = bool(self.list_untracked_files())

        if not has_tracked and not has_untracked:
            return True, False

        command = [
            "git",
            "stash",
            "push",
            "-m",
            f"Auto-stash by git_pull_all ({repo_name})",
        ]
        if include_untracked:
            command.append("-u")
        success, _, _ = self.run(command)
        return success, True

    def pop_stash(self) -> bool:
        success, _, _ = self.run(["git", "stash", "pop"])
        return success

    def checkout(self, branch: str) -> Tuple[bool, str]:
        success, _, stderr = self.run(["git", "checkout", branch])
        return success, stderr

    def pull_current_branch(self, upstream: Optional[str] = None) -> Tuple[bool, str, str]:
        """
        Update the current branch from an already-fetched upstream ref.

        Uses merge/rebase against the local remote-tracking ref (default @{u})
        so a second network fetch is not performed.
        """
        target = upstream or "@{u}"
        if self.options.ff_only:
            command = ["git", "merge", "--ff-only", target]
        elif self.options.rebase:
            command = ["git", "rebase", target]
        else:
            command = ["git", "merge", target]
        return self.run(command)

    def merge_in_progress(self) -> bool:
        success, _, _ = self.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"])
        return success

    def rebase_in_progress(self) -> bool:
        success, _, _ = self.run(["git", "rev-parse", "-q", "--verify", "REBASE_HEAD"])
        return success

    def abort_merge(self) -> None:
        self.run(["git", "merge", "--abort"])

    def abort_rebase(self) -> None:
        self.run(["git", "rebase", "--abort"])

    def abort_update(self) -> None:
        """Abort an in-progress merge or rebase from a failed branch update."""
        if self.rebase_in_progress():
            self.abort_rebase()
        elif self.merge_in_progress():
            self.abort_merge()
        elif self.options.rebase:
            self.abort_rebase()
        else:
            self.abort_merge()

    def fast_forward_branch(
        self, branch: str, remote_branch: str
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        shas = self.rev_parse_many([branch, remote_branch])
        if shas is None:
            # Distinguish which ref failed for a clearer message.
            if self.rev_parse(branch) is None:
                return False, f"Could not resolve local ref {branch}", None, None
            return False, f"Could not resolve remote ref {remote_branch}", None, None

        local_sha, remote_sha = shas
        if local_sha == remote_sha:
            return True, "already up to date", local_sha, remote_sha

        success, _, _ = self.run(["git", "merge-base", "--is-ancestor", branch, remote_branch])
        if not success:
            return False, "cannot fast-forward (diverged)", None, None

        if self.options.dry_run:
            return True, "would fast-forward", local_sha, remote_sha

        success, _, stderr = self.run(
            ["git", "update-ref", f"refs/heads/{branch}", remote_sha, local_sha]
        )
        if not success:
            return False, f"update-ref failed: {stderr}", None, None

        return True, "fast-forwarded", local_sha, remote_sha

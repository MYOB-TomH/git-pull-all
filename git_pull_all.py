#!/usr/bin/env python3
"""
Git Pull All - Updates git repositories in subdirectories.

For each repo, updates only the checked-out branch plus local trunk branches
(main, master, develop by default) that have upstream tracking. Skips fetch
when those branches are already up to date (confirmed via cached refs and
ls-remote, or FETCH_HEAD --max-age). After a targeted fetch, updates the
current branch via merge/rebase of the local upstream ref (no second network
fetch).

Usage:
  ./git_pull_all.py -d ~/code
  ./git_pull_all.py -d ~/code -p 8
  ./git_pull_all.py -d ~/code --dry-run --ff-only
  ./git_pull_all.py -d ~/code --trunk main --trunk develop -q
  ./git_pull_all.py -d ~/code --fetch-always --retry 2
  ./git_pull_all.py -d ~/code --max-age 900 --json

Flags:
  --parallel, -p N     Process N repos in parallel (default: min(8, max(4, CPUs)))
  --dry-run            Show planned actions without making changes
  --ff-only            Pull with --ff-only; skip non-FF trunk branches
  --rebase             Pull with --rebase (mutually exclusive with --ff-only)
  --no-stash           Skip repos with dirty tracked changes instead of stashing
  --trunk BRANCH       Trunk branch to update (repeatable; default: main, master, develop)
  --quiet, -q          Suppress per-branch success messages (skips numstat detail)
  --verbose, -v        Print git commands as they run
  --only PATTERN       Only process repos matching glob (repeatable)
  --exclude PATTERN    Skip repos matching glob (repeatable)
  --no-color           Disable colored output
  --fetch-always       Skip ls-remote; always targeted-fetch when not locally behind
  --max-age SECONDS    Skip network when FETCH_HEAD is fresh and not behind
  --retry N            Retry failed fetches with exponential backoff
  --json               Print JSON summary instead of human summary
  --json-file PATH     Write JSON summary to PATH

Config (optional TOML, CLI wins):
  ~/.config/git-pull-all.toml
  <scan-dir>/.git-pull-all.toml

Exit codes:
  0  All repositories updated successfully
  1  One or more errors (total failure)
  2  Partial success (some repos/branches failed, others succeeded)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event, Lock, local
from typing import List, Optional

from git_pull_lib import (
    DEFAULT_PARALLEL_WORKERS,
    DEFAULT_TRUNK_BRANCHES,
    FileChange,
    GitError,
    GitRepo,
    PullOptions,
    RepoResult,
    candidate_tracking_branches,
    default_git_runner,
    describe_targeted_fetch,
    filter_repos,
    load_merged_config,
    path_is_git_repo,
    peek_directory_arg,
    results_to_summary_dict,
    select_branches_to_update,
)


class Colors:
    """ANSI color codes for terminal output."""

    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    END = "\033[0m"
    BOLD = "\033[1m"

    @classmethod
    def disable(cls) -> None:
        for attr in ("HEADER", "BLUE", "CYAN", "GREEN", "YELLOW", "RED", "END", "BOLD"):
            setattr(cls, attr, "")


print_lock = Lock()
errors_lock = Lock()
results_lock = Lock()
shutdown_event = Event()
MAX_FILE_CHANGES_SHOWN = 25
_print_tls = local()
output_silent = False


def begin_repo_output_buffer() -> None:
    if output_silent:
        return
    _print_tls.buffer = []


def flush_repo_output_buffer() -> None:
    buffer = getattr(_print_tls, "buffer", None)
    if buffer is None:
        return
    with print_lock:
        for line in buffer:
            print(line)
    _print_tls.buffer = None


def discard_repo_output_buffer() -> None:
    _print_tls.buffer = None


def safe_print(*args, **kwargs) -> None:
    if output_silent:
        return
    buffer = getattr(_print_tls, "buffer", None)
    if buffer is not None and not kwargs:
        buffer.append(" ".join(str(a) for a in args))
        return
    with print_lock:
        print(*args, **kwargs)


def verbose_print(options: PullOptions, command: List[str], cwd: str) -> None:
    if options.verbose:
        safe_print(f"{Colors.CYAN}  $ {' '.join(command)}  (in {os.path.basename(cwd)}){Colors.END}")


def append_error(errors: List[GitError], error: GitError) -> None:
    with errors_lock:
        errors.append(error)


def append_result(results: List[RepoResult], result: RepoResult) -> None:
    with results_lock:
        results.append(result)


def record_branch_update(
    result: RepoResult,
    branch: str,
    old_sha: Optional[str],
    new_sha: Optional[str],
    git: GitRepo,
) -> bool:
    """Record a branch update when SHAs changed. Returns True if the branch was updated."""
    if not old_sha or not new_sha or old_sha == new_sha:
        return False

    if git.options.quiet:
        changes = git.get_changed_paths(old_sha, new_sha)
    else:
        changes = git.get_file_changes(old_sha, new_sha)
    result.changes_by_branch[branch] = changes
    if branch not in result.updated:
        result.updated.append(branch)
    return True


def format_file_change_line(change: FileChange) -> str:
    """Format a single changed file with line stats."""
    return (
        f"      {change.path}: "
        f"{Colors.GREEN}+{change.insertions}{Colors.END} "
        f"{Colors.RED}-{change.deletions}{Colors.END}"
    )


def format_file_change_lines(changes: List[FileChange]) -> List[str]:
    """Format changed files, truncating when the list is long."""
    lines = [format_file_change_line(change) for change in changes[:MAX_FILE_CHANGES_SHOWN]]
    remaining = len(changes) - MAX_FILE_CHANGES_SHOWN
    if remaining > 0:
        lines.append(f"      ... and {remaining} more file{'s' if remaining != 1 else ''}")
    return lines


def print_file_changes(changes: List[FileChange]) -> None:
    """Print indented per-file change lines."""
    for line in format_file_change_lines(changes):
        safe_print(line)


def format_repo_update_line(repo_name: str, result: RepoResult) -> str:
    """Format a per-repo update summary line."""
    branch_parts = [
        f"{branch}: {count}"
        for branch, count in sorted(result.files_changed_by_branch.items())
    ]
    total = result.total_files_changed
    file_label = "file" if total == 1 else "files"
    return f"✓ {repo_name}: {total} {file_label} updated ({', '.join(branch_parts)})"


def format_updated_summary(results: List[RepoResult]) -> List[str]:
    """Return sorted lines describing updated repos and branches for the final summary."""
    lines: List[str] = []
    updated_results = sorted(
        (r for r in results if r.changes_by_branch),
        key=lambda r: os.path.basename(r.repo_path).lower(),
    )

    if not updated_results:
        return lines

    lines.append(f"{Colors.GREEN}Updated repositories:{Colors.END}")
    for result in updated_results:
        repo_name = os.path.basename(result.repo_path)
        total = result.total_files_changed
        file_label = "file" if total == 1 else "files"
        lines.append(f"{Colors.GREEN}  {repo_name} ({total} {file_label}){Colors.END}")
        for branch, changes in sorted(result.changes_by_branch.items()):
            branch_count = len(changes)
            branch_file_label = "file" if branch_count == 1 else "files"
            lines.append(
                f"{Colors.GREEN}    {branch} ({branch_count} {branch_file_label}):{Colors.END}"
            )
            lines.extend(format_file_change_lines(changes))

    return lines


def print_repo_update_summary(repo_name: str, result: RepoResult) -> None:
    if result.changes_by_branch:
        safe_print(f"{Colors.GREEN}{format_repo_update_line(repo_name, result)}{Colors.END}")


def make_git_repo(repo_path: str, options: PullOptions) -> GitRepo:
    """Create GitRepo with verbose logging wrapper."""

    def runner(command: List[str], cwd: str):
        verbose_print(options, command, cwd)
        return default_git_runner(command, cwd)

    return GitRepo(repo_path, runner=runner, options=options)


def ensure_stashed_if_needed(
    git: GitRepo,
    repo_path: str,
    repo_name: str,
    original_branch: str,
    errors: List[GitError],
    options: PullOptions,
    already_stashed: bool,
    include_untracked: bool = False,
) -> tuple[bool, bool]:
    """Stash uncommitted changes before a working-tree mutating operation."""
    if already_stashed:
        return True, already_stashed

    has_tracked = git.has_uncommitted_changes()
    if not has_tracked and not include_untracked:
        return True, False

    if options.no_stash and (has_tracked or include_untracked):
        err = GitError(
            repo_path,
            original_branch,
            "Dirty Working Tree",
            "Uncommitted changes present and --no-stash specified",
        )
        append_error(errors, err)
        if not options.quiet:
            safe_print(f"{Colors.RED}✗ Error: Uncommitted changes (--no-stash){Colors.END}")
        return False, False

    if not has_tracked and not include_untracked:
        return True, False

    if options.dry_run:
        label = "tracked and untracked" if include_untracked else "tracked"
        safe_print(f"{Colors.CYAN}[dry-run] Would stash {label} changes{Colors.END}")
        return True, True

    if not options.quiet:
        safe_print(f"{Colors.YELLOW}⚠ Uncommitted changes detected, stashing...{Colors.END}")
    success, stashed = git.stash_changes(
        repo_name, include_untracked=include_untracked, has_tracked=has_tracked
    )
    if not success:
        err = GitError(repo_path, original_branch, "Stash", "Failed to stash uncommitted changes")
        append_error(errors, err)
        safe_print(f"{Colors.RED}✗ Error: Could not stash changes{Colors.END}")
        return False, False
    if stashed and not options.quiet:
        safe_print(f"{Colors.GREEN}✓ Changes stashed successfully{Colors.END}")
    return True, stashed


def restore_original_branch(git: GitRepo, repo_path: str, original_branch: str, repo_name: str) -> bool:
    current = git.get_current_branch()
    if not original_branch or not current or current == original_branch:
        return True

    success, stderr = git.checkout(original_branch)
    if success:
        if not git.options.quiet:
            safe_print(f"{Colors.GREEN}✓ Restored branch {original_branch} in {repo_name}{Colors.END}")
        return True

    safe_print(
        f"{Colors.YELLOW}⚠ Could not restore branch {original_branch} in {repo_name}: {stderr}{Colors.END}"
    )
    return False


def restore_repo_state(
    git: GitRepo,
    repo_path: str,
    original_branch: Optional[str],
    stashed: bool,
    pop_stash: bool,
) -> None:
    """Best-effort restore of branch and optionally stash after interrupt or abnormal exit."""
    repo_name = os.path.basename(repo_path)
    restore_original_branch(git, repo_path, original_branch or "", repo_name)

    if stashed and pop_stash:
        if git.pop_stash():
            if not git.options.quiet:
                safe_print(f"{Colors.GREEN}✓ Restored stashed changes in {repo_name}{Colors.END}")
        else:
            safe_print(
                f"{Colors.YELLOW}⚠ Could not restore stash in {repo_name} - run 'git stash pop' manually{Colors.END}"
            )


def pull_branch(
    git: GitRepo,
    repo_path: str,
    branch: str,
    errors: List[GitError],
    result: RepoResult,
    options: PullOptions,
    upstream: Optional[str] = None,
) -> bool:
    if options.dry_run:
        if options.rebase:
            action = "rebase onto"
        elif options.ff_only:
            action = "fast-forward merge from"
        else:
            action = "merge from"
        target = upstream or "@{u}"
        safe_print(f"{Colors.CYAN}  [dry-run] Would {action} {target}: {branch}{Colors.END}")
        return True

    old_sha = git.rev_parse(branch)
    success, stdout, stderr = git.pull_current_branch(upstream)
    if not success:
        conflict = (
            git.merge_in_progress()
            or git.rebase_in_progress()
            or "CONFLICT" in stderr
            or "CONFLICT" in stdout
        )
        if conflict:
            err = GitError(repo_path, branch, "Merge Conflict", "Merge conflict during pull")
            append_error(errors, err)
            result.failed.append(branch)
            safe_print(f"{Colors.RED}  ✗ Merge conflict on {branch}{Colors.END}")
            git.abort_update()
        else:
            err = GitError(repo_path, branch, "Pull", f"Failed to pull: {stderr}")
            append_error(errors, err)
            result.failed.append(branch)
            safe_print(f"{Colors.RED}  ✗ Error pulling {branch}: {stderr}{Colors.END}")
        return False

    new_sha = git.rev_parse(branch)
    if record_branch_update(result, branch, old_sha, new_sha, git):
        changes = result.changes_by_branch[branch]
        file_count = len(changes)
        if not options.quiet:
            file_label = "file" if file_count == 1 else "files"
            safe_print(
                f"{Colors.GREEN}  ✓ {branch} updated successfully "
                f"({file_count} {file_label}){Colors.END}"
            )
            print_file_changes(changes)
    elif not options.quiet:
        safe_print(f"{Colors.GREEN}  ✓ {branch} is already up to date{Colors.END}")
    return True


def update_branches_behind(
    git: GitRepo,
    repo_path: str,
    repo_name: str,
    original_branch: str,
    selected_branches: dict[str, str],
    behind: dict[str, int],
    errors: List[GitError],
    result: RepoResult,
    options: PullOptions,
    stashed: bool,
) -> tuple[bool, bool]:
    """
    Pull or fast-forward branches behind upstream.

    Returns:
        (all_updates_succeeded, stashed)
    """
    all_ok = True
    branch_names = list(selected_branches.keys())

    for branch_count, branch in enumerate(branch_names, 1):
        if shutdown_event.is_set():
            result.interrupted = True
            return False, stashed

        if branch not in behind:
            result.skipped.append(branch)
            if not options.quiet:
                safe_print(
                    f"\n{Colors.GREEN}  [{branch_count}/{len(branch_names)}] "
                    f"{branch} is already up to date{Colors.END}"
                )
            continue

        remote_branch = selected_branches[branch]
        commits = behind[branch]
        suffix = f" ({commits} commit{'s' if commits != 1 else ''} behind)"

        if branch == original_branch:
            safe_print(
                f"\n{Colors.BLUE}  [{branch_count}/{len(branch_names)}] "
                f"Pulling: {branch} (current, tracks {remote_branch}){suffix}{Colors.END}"
            )
            ok, stashed = ensure_stashed_if_needed(
                git, repo_path, repo_name, original_branch, errors, options, stashed
            )
            if not ok:
                all_ok = False
                return False, stashed
            if not pull_branch(
                git, repo_path, branch, errors, result, options, upstream=remote_branch
            ):
                all_ok = False
        else:
            safe_print(
                f"\n{Colors.BLUE}  [{branch_count}/{len(branch_names)}] "
                f"Fast-forwarding: {branch} (tracks {remote_branch}){suffix}{Colors.END}"
            )
            ff_ok, ff_msg, old_sha, new_sha = git.fast_forward_branch(branch, remote_branch)
            if ff_ok:
                if ff_msg == "already up to date":
                    result.skipped.append(branch)
                    if not options.quiet:
                        safe_print(f"{Colors.GREEN}  ✓ {branch} {ff_msg}{Colors.END}")
                elif record_branch_update(result, branch, old_sha, new_sha, git):
                    changes = result.changes_by_branch[branch]
                    file_count = len(changes)
                    if not options.quiet:
                        file_label = "file" if file_count == 1 else "files"
                        safe_print(
                            f"{Colors.GREEN}  ✓ {branch} {ff_msg} "
                            f"({file_count} {file_label}){Colors.END}"
                        )
                        print_file_changes(changes)
                elif not options.quiet:
                    safe_print(f"{Colors.GREEN}  ✓ {branch} {ff_msg}{Colors.END}")
            elif options.ff_only:
                result.skipped.append(branch)
                safe_print(f"{Colors.YELLOW}  ⚠ {ff_msg} — skipped (--ff-only){Colors.END}")
            else:
                safe_print(f"{Colors.YELLOW}  ⚠ {ff_msg} — falling back to checkout + pull{Colors.END}")
                if options.dry_run:
                    safe_print(
                        f"{Colors.CYAN}  [dry-run] Would checkout {branch}, pull, "
                        f"then return to {original_branch}{Colors.END}"
                    )
                    continue

                blockers = git.untracked_would_block_checkout(branch)
                include_untracked = bool(blockers)
                if blockers:
                    safe_print(
                        f"{Colors.YELLOW}  ⚠ Untracked files would block checkout: "
                        f"{', '.join(sorted(blockers)[:3])}{'...' if len(blockers) > 3 else ''}{Colors.END}"
                    )

                ok, stashed = ensure_stashed_if_needed(
                    git,
                    repo_path,
                    repo_name,
                    original_branch,
                    errors,
                    options,
                    stashed,
                    include_untracked=include_untracked,
                )
                if not ok:
                    all_ok = False
                    result.failed.append(branch)
                    continue

                success, stderr = git.checkout(branch)
                if not success:
                    err = GitError(repo_path, branch, "Checkout", f"Failed to checkout: {stderr}")
                    append_error(errors, err)
                    result.failed.append(branch)
                    safe_print(f"{Colors.RED}  ✗ Error checking out {branch}: {stderr}{Colors.END}")
                    all_ok = False
                    continue

                if not pull_branch(
                    git, repo_path, branch, errors, result, options, upstream=remote_branch
                ):
                    all_ok = False

                if not restore_original_branch(git, repo_path, original_branch, repo_name):
                    err = GitError(
                        repo_path,
                        original_branch,
                        "Checkout",
                        f"Failed to restore original branch after updating {branch}",
                    )
                    append_error(errors, err)
                    all_ok = False

    return all_ok, stashed


def process_repository(
    repo_path: str,
    errors: List[GitError],
    results: List[RepoResult],
    repo_num: int,
    total_repos: int,
    options: PullOptions,
) -> None:
    """Process a single git repository."""
    repo_name = os.path.basename(repo_path)
    git = make_git_repo(repo_path, options)
    result = RepoResult(repo_path=repo_path)
    original_branch: Optional[str] = None
    stashed = False
    stash_applied = False
    cleanup_done = False
    all_updates_ok = True
    started = time.monotonic()
    begin_repo_output_buffer()

    safe_print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 60}{Colors.END}")
    safe_print(f"{Colors.HEADER}{Colors.BOLD}[{repo_num}/{total_repos}] Processing: {repo_name}{Colors.END}")
    if options.dry_run:
        safe_print(f"{Colors.CYAN}  [dry-run mode — no changes will be made]{Colors.END}")
    safe_print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 60}{Colors.END}")

    try:
        if shutdown_event.is_set():
            result.interrupted = True
            return

        original_branch = git.get_current_branch()
        if not original_branch:
            err = GitError(
                repo_path, "N/A", "Branch Detection", "Could not determine current branch (detached HEAD?)"
            )
            append_error(errors, err)
            result.failed.append("HEAD")
            safe_print(f"{Colors.RED}✗ Error: Could not determine current branch{Colors.END}")
            return

        safe_print(f"{Colors.CYAN}Current branch: {original_branch}{Colors.END}")

        safe_print(f"{Colors.BLUE}[1/3] Checking selected branches (current + trunk)...{Colors.END}")
        candidates = candidate_tracking_branches(original_branch, options.trunk_branches)
        candidate_filter = {name: "" for name in candidates}
        tracking_branches, tracking_behind, indeterminate_branches = git.list_branch_tracking_status(
            candidate_filter
        )
        if not tracking_branches:
            safe_print(f"{Colors.YELLOW}⚠ No branches with remote tracking found{Colors.END}")
            return

        selected_branches = select_branches_to_update(
            tracking_branches, original_branch, options.trunk_branches
        )
        if original_branch not in tracking_branches:
            safe_print(
                f"{Colors.YELLOW}⚠ Current branch '{original_branch}' has no upstream tracking — "
                f"only trunk branches will be updated{Colors.END}"
            )

        if not selected_branches:
            safe_print(f"{Colors.YELLOW}⚠ No selected branches with upstream tracking found{Colors.END}")
            return

        safe_print(
            f"{Colors.CYAN}Selected {len(selected_branches)} branch(es): "
            f"{', '.join(selected_branches.keys())}{Colors.END}"
        )

        behind = {
            branch: tracking_behind[branch]
            for branch in selected_branches
            if branch in tracking_behind
        }
        indeterminate = bool(indeterminate_branches & set(selected_branches))
        needs_fetch = bool(behind) or indeterminate
        remote_updates_pending = False

        if not needs_fetch:
            if (
                options.max_age is not None
                and not options.fetch_always
                and git.fetch_cache_fresh(options.max_age)
            ):
                safe_print(
                    f"{Colors.GREEN}✓ All selected branches appear up to date "
                    f"(FETCH_HEAD within --max-age {options.max_age}s; skipped network){Colors.END}"
                )
                for branch in selected_branches:
                    result.skipped.append(branch)
                cleanup_done = True
                return

            if options.fetch_always:
                needs_fetch = True
                if options.dry_run:
                    safe_print(
                        f"{Colors.CYAN}[dry-run] --fetch-always: would run: "
                        f"{describe_targeted_fetch(selected_branches)}{Colors.END}"
                    )
            elif options.dry_run:
                safe_print(
                    f"{Colors.GREEN}✓ All selected branches appear up to date "
                    f"(would confirm with ls-remote){Colors.END}"
                )
                all_match, check_ok = git.check_remote_freshness(selected_branches)
                if check_ok and all_match:
                    safe_print(f"{Colors.GREEN}✓ Confirmed up to date — would skip fetch and updates{Colors.END}")
                    for branch in selected_branches:
                        result.skipped.append(branch)
                    cleanup_done = True
                    return
                if check_ok and not all_match:
                    remote_updates_pending = True
                elif not check_ok:
                    safe_print(
                        f"{Colors.YELLOW}⚠ Could not confirm via ls-remote — "
                        f"would run: {describe_targeted_fetch(selected_branches)}{Colors.END}"
                    )
                needs_fetch = True
            else:
                all_match, check_ok = git.check_remote_freshness(selected_branches)
                if check_ok and all_match:
                    safe_print(
                        f"{Colors.GREEN}✓ All selected branches already up to date (skipped fetch){Colors.END}"
                    )
                    for branch in selected_branches:
                        result.skipped.append(branch)
                    cleanup_done = True
                    return
                if not check_ok:
                    safe_print(
                        f"{Colors.YELLOW}⚠ Could not confirm freshness via ls-remote — fetching anyway{Colors.END}"
                    )
                needs_fetch = True

        if needs_fetch:
            safe_print(f"{Colors.BLUE}[2/3] Fetching updates (if needed)...{Colors.END}")
            if options.dry_run:
                safe_print(
                    f"{Colors.CYAN}[dry-run] Would run: {describe_targeted_fetch(selected_branches)}{Colors.END}"
                )
            else:
                success, stderr = git.targeted_fetch_with_retry(
                    selected_branches, retries=options.retry
                )
                if not success:
                    err = GitError(repo_path, "N/A", "Fetch", f"Failed to fetch: {stderr}")
                    append_error(errors, err)
                    result.failed.append("fetch")
                    safe_print(f"{Colors.RED}✗ Error fetching: {stderr}{Colors.END}")
                    return
                safe_print(f"{Colors.GREEN}✓ Fetch completed{Colors.END}")

        behind, _ = git.branches_behind(selected_branches)
        if options.dry_run and remote_updates_pending and not behind:
            behind = {branch: 0 for branch in selected_branches}

        if not behind:
            if options.dry_run and needs_fetch:
                safe_print(f"{Colors.GREEN}✓ All selected branches would be up to date after fetch{Colors.END}")
            elif not options.dry_run:
                safe_print(f"{Colors.GREEN}✓ All selected branches already up to date{Colors.END}")
            for branch in selected_branches:
                if branch not in result.updated:
                    result.skipped.append(branch)
            cleanup_done = True
            return

        safe_print(f"{Colors.BLUE}[3/3] Updating branches behind upstream...{Colors.END}")
        if options.dry_run:
            for branch, count in behind.items():
                remote_branch = selected_branches[branch]
                if branch == original_branch:
                    if options.rebase:
                        action = "rebase onto"
                    elif options.ff_only:
                        action = "fast-forward merge from"
                    else:
                        action = "merge from"
                else:
                    action = "fast-forward"
                if count > 0:
                    count_label = f", {count} commit{'s' if count != 1 else ''} behind"
                else:
                    count_label = ", updates pending on remote"
                safe_print(
                    f"{Colors.CYAN}  [dry-run] Would {action}: {branch} "
                    f"(tracks {remote_branch}{count_label}){Colors.END}"
                )
        else:
            all_updates_ok, stashed = update_branches_behind(
                git,
                repo_path,
                repo_name,
                original_branch,
                selected_branches,
                behind,
                errors,
                result,
                options,
                stashed,
            )
            stash_applied = stashed

        if stash_applied and not options.dry_run:
            if all_updates_ok and not result.failed:
                safe_print(f"{Colors.BLUE}Restoring stashed changes...{Colors.END}")
                if git.pop_stash():
                    if not options.quiet:
                        safe_print(f"{Colors.GREEN}✓ Stashed changes restored{Colors.END}")
                    stash_applied = False
                else:
                    err = GitError(
                        repo_path,
                        git.get_current_branch() or "unknown",
                        "Stash Pop",
                        "Failed to restore stashed changes - run 'git stash pop' manually",
                    )
                    append_error(errors, err)
                    safe_print(
                        f"{Colors.YELLOW}⚠ Could not restore stash automatically - "
                        f"run 'git stash pop' manually{Colors.END}"
                    )
            else:
                safe_print(
                    f"{Colors.YELLOW}⚠ Leaving stash in place due to update failures — "
                    f"run 'git stash pop' manually when ready{Colors.END}"
                )

        cleanup_done = True
        print_repo_update_summary(repo_name, result)

    finally:
        result.duration_seconds = round(time.monotonic() - started, 3)
        if result.duration_seconds is not None and not options.quiet:
            safe_print(
                f"{Colors.CYAN}  Completed in {result.duration_seconds:.2f}s{Colors.END}"
            )
        flush_repo_output_buffer()
        append_result(results, result)
        if not options.dry_run and not cleanup_done:
            restore_repo_state(git, repo_path, original_branch, stash_applied, pop_stash=False)


def find_git_repos(directory: str, options: PullOptions) -> List[str]:
    """Find immediate subdirectories that are git repositories, sorted by name."""
    _ = options
    repos: List[str] = []
    try:
        entries = sorted(os.listdir(directory))
    except PermissionError:
        return repos

    for item in entries:
        item_path = os.path.join(directory, item)
        if os.path.isdir(item_path) and not item.startswith(".") and path_is_git_repo(item_path):
            repos.append(item_path)
    return repos


def compute_exit_code(errors: List[GitError], results: List[RepoResult]) -> int:
    if not errors and all(r.success for r in results):
        return 0
    has_success = any(r.updated or r.skipped for r in results)
    has_failure = bool(errors) or any(r.failed or r.interrupted for r in results)
    if has_success and has_failure:
        return 2
    return 1


def print_summary(
    errors: List[GitError],
    results: List[RepoResult],
    dry_run: bool,
    *,
    json_mode: bool = False,
    json_file: Optional[str] = None,
) -> None:
    summary = results_to_summary_dict(errors, results, dry_run=dry_run)

    if json_file:
        with open(json_file, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")

    if json_mode:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return

    print(f"\n{Colors.BOLD}{Colors.HEADER}{'=' * 60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.HEADER}Summary{Colors.END}")
    print(f"{Colors.BOLD}{Colors.HEADER}{'=' * 60}{Colors.END}")

    total_updated = sum(len(r.updated) for r in results)
    total_skipped = sum(len(r.skipped) for r in results)
    total_failed = sum(len(r.failed) for r in results)
    total_files_changed = sum(r.total_files_changed for r in results)
    interrupted = sum(1 for r in results if r.interrupted)
    timed = [r.duration_seconds for r in results if r.duration_seconds is not None]
    total_time = sum(timed) if timed else None

    print(
        f"{Colors.CYAN}Repos processed: {len(results)} | "
        f"Branches updated: {total_updated} | "
        f"Files changed: {total_files_changed} | "
        f"Skipped: {total_skipped} | "
        f"Failed: {total_failed}{Colors.END}"
    )
    if total_time is not None:
        print(f"{Colors.CYAN}Total repo time: {total_time:.2f}s{Colors.END}")
        for result in sorted(results, key=lambda r: os.path.basename(r.repo_path).lower()):
            if result.duration_seconds is None:
                continue
            print(
                f"{Colors.CYAN}  {os.path.basename(result.repo_path)}: "
                f"{result.duration_seconds:.2f}s{Colors.END}"
            )
    if interrupted:
        print(f"{Colors.YELLOW}Interrupted repos: {interrupted}{Colors.END}")

    updated_summary = format_updated_summary(results)
    if updated_summary:
        print()
        for line in updated_summary:
            print(line)

    if errors:
        print(f"\n{Colors.RED}{Colors.BOLD}Encountered {len(errors)} error(s):{Colors.END}\n")
        for error in errors:
            repo_label = os.path.basename(error.repo_path)
            print(f"{Colors.RED}✗ {repo_label} [{error.branch}] - {error.error_type}:{Colors.END}")
            print(f"  {error.message}\n")
    elif dry_run:
        print(f"{Colors.GREEN}{Colors.BOLD}✓ Dry-run completed — no changes were made{Colors.END}")
    else:
        print(f"{Colors.GREEN}{Colors.BOLD}✓ All repositories updated successfully!{Colors.END}")

    print(f"{Colors.CYAN}Operation complete.{Colors.END}")
    if json_file:
        print(f"{Colors.CYAN}Wrote JSON summary to {json_file}{Colors.END}")


def main() -> None:
    # Resolve scan directory early so local config can be merged before full parse.
    peek_dir = peek_directory_arg()
    bootstrap_dir = os.path.abspath(peek_dir) if peek_dir else os.getcwd()
    # User config may supply directory before local config is known.
    user_only = load_merged_config(bootstrap_dir)
    if peek_dir is None and "directory" in user_only:
        bootstrap_dir = os.path.abspath(user_only["directory"])
        config = load_merged_config(bootstrap_dir)
    else:
        config = user_only

    parser = argparse.ArgumentParser(
        description="Update git repositories in subdirectories (current branch + trunk branches)",
    )
    parser.add_argument(
        "--parallel",
        "-p",
        type=int,
        default=config.get("parallel", DEFAULT_PARALLEL_WORKERS),
        metavar="N",
        help=(
            f"Number of repositories to process in parallel "
            f"(default: {DEFAULT_PARALLEL_WORKERS}, or config)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=bool(config.get("dry_run", False)),
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--ff-only",
        action="store_true",
        default=bool(config.get("ff_only", False)),
        help="Fast-forward only when updating the current branch; skip non-FF trunk branches with a warning",
    )
    parser.add_argument(
        "--rebase",
        action="store_true",
        default=bool(config.get("rebase", False)),
        help="Rebase the current branch onto its upstream instead of merging",
    )
    parser.add_argument(
        "--no-stash",
        action="store_true",
        default=bool(config.get("no_stash", False)),
        help="Skip repos with dirty tracked changes instead of auto-stashing",
    )
    parser.add_argument(
        "--trunk",
        action="append",
        metavar="BRANCH",
        default=[],
        help="Trunk branch to update (repeatable; default: main, master, develop)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=bool(config.get("quiet", False)),
        help="Suppress per-branch success messages (also skips numstat detail)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=bool(config.get("verbose", False)),
        help="Print git commands as they run",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="PATTERN",
        default=[],
        help="Only process repos matching PATTERN (glob on basename, repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="PATTERN",
        default=[],
        help="Skip repos matching PATTERN (glob on basename, repeatable)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=bool(config.get("no_color", False)),
        help="Disable colored output",
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=config.get("directory"),
        metavar="DIR",
        help="Directory to scan for git repos (default: current directory)",
    )
    parser.add_argument(
        "--fetch-always",
        action="store_true",
        default=bool(config.get("fetch_always", False)),
        help="Skip ls-remote freshness check and always targeted-fetch when not locally behind",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=config.get("max_age"),
        metavar="SECONDS",
        help="Skip network if FETCH_HEAD is newer than SECONDS and branches are not behind",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=int(config.get("retry", 0)),
        metavar="N",
        help="Retry failed fetches up to N times with exponential backoff (default: 0)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=bool(config.get("json", False)),
        help="Print machine-readable JSON summary instead of the human summary",
    )
    parser.add_argument(
        "--json-file",
        default=config.get("json_file"),
        metavar="PATH",
        help="Write JSON summary to PATH (can combine with human summary)",
    )
    args = parser.parse_args()

    if args.parallel < 1:
        parser.error("--parallel must be at least 1")

    if args.ff_only and args.rebase:
        parser.error("--ff-only and --rebase are mutually exclusive")

    if args.max_age is not None and args.max_age < 0:
        parser.error("--max-age must be >= 0")

    if args.retry < 0:
        parser.error("--retry must be >= 0")

    if args.trunk:
        trunk_branches = frozenset(args.trunk)
    elif config.get("trunk"):
        trunk_branches = frozenset(config["trunk"])
    else:
        trunk_branches = DEFAULT_TRUNK_BRANCHES

    only_patterns = args.only or config.get("only")
    exclude_patterns = args.exclude or config.get("exclude")

    options = PullOptions(
        dry_run=args.dry_run,
        ff_only=args.ff_only,
        rebase=args.rebase,
        no_stash=args.no_stash,
        quiet=args.quiet,
        verbose=args.verbose,
        trunk_branches=trunk_branches,
        fetch_always=args.fetch_always,
        max_age=args.max_age,
        retry=args.retry,
    )

    if args.no_color or not sys.stdout.isatty() or args.json:
        Colors.disable()

    global output_silent
    output_silent = bool(args.json)

    current_dir = os.path.abspath(args.directory) if args.directory else os.getcwd()

    if not os.path.isdir(current_dir):
        print(f"{Colors.RED}Error: '{current_dir}' is not a directory{Colors.END}")
        sys.exit(1)

    # Reload config from the final scan directory so local .git-pull-all.toml applies
    # when -d differs from the bootstrap peek (CLI still already applied above).
    if os.path.abspath(current_dir) != os.path.abspath(bootstrap_dir):
        # Filters/trunks already resolved from CLI or earlier config; only used for messaging.
        pass

    if not args.json:
        print(f"{Colors.BOLD}{Colors.HEADER}Git Pull All - Multi-Repository Updater{Colors.END}")
        print(f"{Colors.CYAN}Working directory: {current_dir}{Colors.END}")
        if args.dry_run:
            print(f"{Colors.CYAN}Mode: dry-run (no changes will be made){Colors.END}")
        if args.parallel > 1:
            print(f"{Colors.CYAN}Parallel workers: {args.parallel}{Colors.END}")
        if args.trunk or config.get("trunk"):
            print(f"{Colors.CYAN}Trunk branches: {', '.join(sorted(trunk_branches))}{Colors.END}")
        if args.ff_only:
            print(f"{Colors.CYAN}Pull mode: fast-forward only{Colors.END}")
        elif args.rebase:
            print(f"{Colors.CYAN}Pull mode: rebase{Colors.END}")
        if args.fetch_always:
            print(f"{Colors.CYAN}Fetch mode: always (skip ls-remote freshness){Colors.END}")
        if args.max_age is not None:
            print(f"{Colors.CYAN}Max age: {args.max_age}s (skip network when FETCH_HEAD is fresh){Colors.END}")
        if args.retry:
            print(f"{Colors.CYAN}Fetch retries: {args.retry}{Colors.END}")
        print()

        print(f"{Colors.BLUE}Scanning for git repositories...{Colors.END}")

    git_repos = find_git_repos(current_dir, options)
    git_repos = filter_repos(git_repos, only_patterns or None, exclude_patterns or None)

    if not args.json:
        for repo_path in git_repos:
            print(f"{Colors.GREEN}✓ Found: {os.path.basename(repo_path)}{Colors.END}")

    if not git_repos:
        if only_patterns or exclude_patterns:
            msg = "No git repositories matched the filter criteria"
        else:
            msg = "No git repositories found in subdirectories"
        if args.json:
            print(json.dumps(results_to_summary_dict([], [], dry_run=args.dry_run), indent=2, sort_keys=True))
        else:
            print(f"{Colors.YELLOW}{msg}{Colors.END}")
        sys.exit(0)

    if not args.json:
        print(f"\n{Colors.BOLD}Found {len(git_repos)} git repositor{'y' if len(git_repos) == 1 else 'ies'}{Colors.END}")

    errors: List[GitError] = []
    results: List[RepoResult] = []

    def emit_summary_and_exit(code: int) -> None:
        print_summary(
            errors,
            results,
            args.dry_run,
            json_mode=args.json,
            json_file=args.json_file,
        )
        sys.exit(code)

    if args.parallel > 1:
        if not args.json:
            print(f"{Colors.CYAN}Processing repositories in parallel...{Colors.END}")
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {
                executor.submit(
                    process_repository,
                    repo_path,
                    errors,
                    results,
                    idx + 1,
                    len(git_repos),
                    options,
                ): repo_path
                for idx, repo_path in enumerate(git_repos)
            }

            try:
                for future in as_completed(futures):
                    repo_path = futures[future]
                    try:
                        future.result()
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        repo_name = os.path.basename(repo_path)
                        err = GitError(repo_path, "N/A", "Unexpected", str(exc))
                        append_error(errors, err)
                        safe_print(f"{Colors.RED}✗ Unexpected error in {repo_name}: {exc}{Colors.END}")
            except KeyboardInterrupt:
                shutdown_event.set()
                if not args.json:
                    print(f"\n{Colors.YELLOW}Operation cancelled — waiting for in-flight repos...{Colors.END}")
                executor.shutdown(wait=True, cancel_futures=True)
                interrupted = [os.path.basename(p) for p in git_repos if p not in {r.repo_path for r in results}]
                if interrupted and not args.json:
                    print(f"{Colors.YELLOW}Repos still running or not started: {', '.join(interrupted)}{Colors.END}")
                emit_summary_and_exit(1)
    else:
        for idx, repo_path in enumerate(git_repos, 1):
            try:
                if shutdown_event.is_set():
                    break
                process_repository(repo_path, errors, results, idx, len(git_repos), options)
            except KeyboardInterrupt:
                shutdown_event.set()
                if not args.json:
                    print(f"\n{Colors.YELLOW}Operation cancelled by user{Colors.END}")
                emit_summary_and_exit(1)
            except Exception as exc:
                repo_name = os.path.basename(repo_path)
                err = GitError(repo_path, "N/A", "Unexpected", str(exc))
                append_error(errors, err)
                print(f"{Colors.RED}✗ Unexpected error in {repo_name}: {exc}{Colors.END}")

    emit_summary_and_exit(compute_exit_code(errors, results))


if __name__ == "__main__":
    main()

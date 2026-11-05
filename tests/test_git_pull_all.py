"""Tests for git_pull_all CLI helpers."""

import json
from datetime import datetime

from git_pull_all import (
    Colors,
    compute_exit_code,
    format_file_change_line,
    format_repo_update_line,
    format_run_timestamp,
    format_updated_summary,
    print_summary,
    record_branch_update,
)
from git_pull_lib import FileChange, GitError, GitRepo, PullOptions, RepoResult


def _stats(insertions: int, deletions: int) -> str:
    return (
        f"{Colors.GREEN}+{insertions}{Colors.END} "
        f"{Colors.RED}-{deletions}{Colors.END}"
    )


def test_exit_code_all_success():
    results = [RepoResult(repo_path="/a", updated=["main"])]
    assert compute_exit_code([], results) == 0


def test_exit_code_total_failure():
    results = [RepoResult(repo_path="/a", failed=["main"])]
    errors = [GitError("/a", "main", "Pull", "failed")]
    assert compute_exit_code(errors, results) == 1


def test_exit_code_partial_success():
    results = [
        RepoResult(repo_path="/a", updated=["main"]),
        RepoResult(repo_path="/b", failed=["develop"]),
    ]
    errors = [GitError("/b", "develop", "Pull", "failed")]
    assert compute_exit_code(errors, results) == 2


def test_format_file_change_line():
    change = FileChange(path="src/a.py", insertions=10, deletions=2)
    assert format_file_change_line(change) == f"      src/a.py: {_stats(10, 2)}"


def test_format_repo_update_line():
    result = RepoResult(
        repo_path="/code/my-repo",
        updated=["main", "develop"],
        changes_by_branch={
            "main": [FileChange("a.py", 5, 3), FileChange("b.py", 3, 1)],
            "develop": [FileChange("c.py", 4, 0)],
        },
    )
    line = format_repo_update_line("my-repo", result)
    assert line == "✓ my-repo: 3 files updated (develop: 1, main: 2)"


def test_format_updated_summary_sorted():
    results = [
        RepoResult(
            repo_path="/code/zeta",
            updated=["main"],
            changes_by_branch={"main": [FileChange("z.py", 1, 0)]},
        ),
        RepoResult(
            repo_path="/code/alpha",
            updated=["develop", "main"],
            changes_by_branch={
                "develop": [FileChange("d.py", 3, 0)],
                "main": [FileChange("a.py", 5, 0), FileChange("b.py", 0, 0)],
            },
        ),
    ]
    lines = format_updated_summary(results)
    assert lines == [
        f"{Colors.GREEN}Updated repositories:{Colors.END}",
        f"{Colors.GREEN}  alpha (3 files){Colors.END}",
        f"{Colors.GREEN}    develop (1 file):{Colors.END}",
        f"      d.py: {_stats(3, 0)}",
        f"{Colors.GREEN}    main (2 files):{Colors.END}",
        f"      a.py: {_stats(5, 0)}",
        f"      b.py: {_stats(0, 0)}",
        f"{Colors.GREEN}  zeta (1 file){Colors.END}",
        f"{Colors.GREEN}    main (1 file):{Colors.END}",
        f"      z.py: {_stats(1, 0)}",
    ]


def test_format_updated_summary_empty():
    assert format_updated_summary([RepoResult(repo_path="/code/foo")]) == []


class _Runner:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, command, cwd):
        self.calls.append(command)
        key = tuple(command)
        for prefix, response in self.responses.items():
            if key[: len(prefix)] == prefix:
                return response
        return False, "", f"unexpected: {command}"


def test_record_branch_update_quiet_uses_name_only():
    runner = _Runner(
        {
            ("git", "diff", "--name-only", "aaa..bbb"): (True, "a.py\n", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner, options=PullOptions(quiet=True))
    result = RepoResult(repo_path="/tmp/repo")
    assert record_branch_update(result, "main", "aaa", "bbb", git) is True
    assert result.updated == ["main"]
    assert result.changes_by_branch["main"] == [FileChange("a.py", 0, 0)]
    assert not any("--numstat" in " ".join(c) for c in runner.calls)


def test_record_branch_update_default_uses_numstat():
    runner = _Runner(
        {
            ("git", "diff", "--numstat", "aaa..bbb"): (True, "2\t1\ta.py\n", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner, options=PullOptions(quiet=False))
    result = RepoResult(repo_path="/tmp/repo")
    assert record_branch_update(result, "main", "aaa", "bbb", git) is True
    assert result.changes_by_branch["main"] == [FileChange("a.py", 2, 1)]


def test_format_run_timestamp_local_clock():
    assert format_run_timestamp(datetime(2026, 11, 6, 10, 15)) == "10:15 AM, 06/11/26"
    assert format_run_timestamp(datetime(2026, 11, 6, 22, 15)) == "10:15 PM, 06/11/26"
    assert format_run_timestamp(datetime(2026, 1, 6, 9, 5)) == "09:05 AM, 06/01/26"
    assert format_run_timestamp(datetime(2026, 11, 6, 0, 0)) == "12:00 AM, 06/11/26"
    assert format_run_timestamp(datetime(2026, 11, 6, 12, 0)) == "12:00 PM, 06/11/26"


def test_print_summary_outcomes_include_end_timestamp(capsys, monkeypatch):
    monkeypatch.setattr(
        "git_pull_all.format_run_timestamp",
        lambda moment=None: "10:15 AM, 06/11/26",
    )
    stamp = "(10:15 AM, 06/11/26)"
    success = [RepoResult(repo_path="/code/foo", updated=["main"])]
    print_summary([], success, dry_run=False)
    assert f"✓ All repositories updated successfully {stamp}" in capsys.readouterr().out

    print_summary([], success, dry_run=True)
    assert f"✓ Dry-run completed — no changes were made {stamp}" in capsys.readouterr().out

    errors = [GitError("/code/foo", "main", "Pull", "failed")]
    print_summary(errors, [RepoResult(repo_path="/code/foo", failed=["main"])], dry_run=False)
    assert f"Encountered 1 error(s) {stamp}:" in capsys.readouterr().out


def test_print_summary_json_mode(capsys):
    results = [
        RepoResult(
            repo_path="/code/foo",
            updated=["main"],
            duration_seconds=0.5,
            changes_by_branch={"main": [FileChange("a.py", 1, 0)]},
        )
    ]
    print_summary([], results, dry_run=False, json_mode=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["repos"][0]["name"] == "foo"
    assert payload["totals"]["files_changed"] == 1


def test_print_summary_json_file(tmp_path, capsys):
    path = tmp_path / "out.json"
    results = [RepoResult(repo_path="/code/foo", skipped=["main"], duration_seconds=0.1)]
    print_summary([], results, dry_run=True, json_mode=False, json_file=str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["dry_run"] is True
    assert "Wrote JSON summary" in capsys.readouterr().out

"""Tests for git_pull_lib helpers."""

from __future__ import annotations

from typing import Dict, List, Tuple

from git_pull_lib import (
    FileChange,
    GitError,
    GitRepo,
    PullOptions,
    RepoResult,
    candidate_tracking_branches,
    describe_targeted_fetch,
    filter_repos,
    normalize_config,
    parse_ls_remote_output,
    parse_upstream,
    parse_upstream_track,
    path_is_git_repo,
    peek_directory_arg,
    results_to_summary_dict,
    select_branches_to_update,
)


def test_parse_upstream_simple():
    assert parse_upstream("origin/main") == ("origin", "main")


def test_parse_upstream_nested_ref():
    assert parse_upstream("upstream/feature/foo") == ("upstream", "feature/foo")


def test_parse_upstream_track_empty():
    assert parse_upstream_track("") == (0, True)


def test_parse_upstream_track_behind():
    assert parse_upstream_track("[behind 3]") == (3, True)
    assert parse_upstream_track("[ahead 1, behind 2]") == (2, True)


def test_parse_upstream_track_ahead_only():
    assert parse_upstream_track("[ahead 4]") == (0, True)


def test_parse_upstream_track_gone():
    assert parse_upstream_track("[gone]") == (None, False)


def test_select_branches_current_only():
    tracking = {"feature-x": "origin/feature-x"}
    selected = select_branches_to_update(tracking, "feature-x")
    assert selected == {"feature-x": "origin/feature-x"}


def test_select_branches_current_plus_trunks():
    tracking = {
        "main": "origin/main",
        "develop": "origin/develop",
        "feature-x": "origin/feature-x",
    }
    selected = select_branches_to_update(tracking, "feature-x")
    assert set(selected.keys()) == {"develop", "feature-x", "main"}


def test_select_branches_custom_trunk():
    tracking = {"release": "origin/release", "feature-x": "origin/feature-x"}
    selected = select_branches_to_update(tracking, "feature-x", frozenset({"release"}))
    assert set(selected.keys()) == {"feature-x", "release"}


def test_select_branches_no_upstream_for_current():
    tracking = {"main": "origin/main"}
    selected = select_branches_to_update(tracking, "orphan")
    assert selected == {"main": "origin/main"}


def test_describe_targeted_fetch():
    branches = {"main": "origin/main", "develop": "origin/develop", "x": "upstream/x"}
    description = describe_targeted_fetch(branches)
    assert "git fetch --prune origin develop main" in description
    assert "git fetch --prune upstream x" in description


def test_filter_repos_only():
    repos = ["/code/foo", "/code/bar", "/code/baz"]
    filtered = filter_repos(repos, only_patterns=["foo", "bar"])
    assert filtered == ["/code/foo", "/code/bar"]


def test_filter_repos_exclude():
    repos = ["/code/foo", "/code/bar", "/code/baz"]
    filtered = filter_repos(repos, exclude_patterns=["ba*"])
    assert filtered == ["/code/foo"]


def test_parse_ls_remote_output():
    stdout = "abc123\trefs/heads/main\ndef456\trefs/heads/develop\n"
    parsed = parse_ls_remote_output(stdout)
    assert parsed == {"main": "abc123", "develop": "def456"}


def test_path_is_git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert path_is_git_repo(str(repo)) is False
    (repo / ".git").mkdir()
    assert path_is_git_repo(str(repo)) is True

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /tmp/somewhere\n", encoding="utf-8")
    assert path_is_git_repo(str(worktree)) is True


def test_repo_result_success_and_partial():
    ok = RepoResult(repo_path="/code/foo", updated=["main"])
    assert ok.success is True
    assert ok.partial is False

    partial = RepoResult(repo_path="/code/foo", updated=["main"], failed=["develop"])
    assert partial.success is False
    assert partial.partial is True


def test_repo_result_total_files_changed():
    result = RepoResult(
        repo_path="/code/foo",
        updated=["main", "develop"],
        changes_by_branch={
            "main": [
                FileChange("a.py", 5, 2),
                FileChange("b.py", 3, 1),
            ],
            "develop": [
                FileChange("c.py", 4, 0),
            ],
        },
    )
    assert result.total_files_changed == 3
    assert result.files_changed_by_branch == {"main": 2, "develop": 1}


def test_count_changed_files_equal_shas():
    runner = MockRunner({})
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.count_changed_files("abc", "abc") == 0
    assert runner.calls == []


def test_count_changed_files_counts_lines():
    runner = MockRunner(
        {
            ("git", "diff", "--numstat", "aaa..bbb"): (
                True,
                "5\t2\tsrc/a.py\n3\t1\tsrc/b.py\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.count_changed_files("aaa", "bbb") == 2


def test_get_file_changes_parses_numstat():
    runner = MockRunner(
        {
            ("git", "diff", "--numstat", "aaa..bbb"): (
                True,
                "10\t2\tsrc/a.py\n-\t-\tbin.dat\n1\t0\tsrc/b.py\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    changes = git.get_file_changes("aaa", "bbb")
    assert changes == [
        FileChange(path="bin.dat", insertions=0, deletions=0),
        FileChange(path="src/a.py", insertions=10, deletions=2),
        FileChange(path="src/b.py", insertions=1, deletions=0),
    ]


def test_rev_parse():
    runner = MockRunner({("git", "rev-parse", "main"): (True, "deadbeef", "")})
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.rev_parse("main") == "deadbeef"


class MockRunner:
    """Configurable mock git runner keyed by command prefix."""

    def __init__(self, responses: Dict[Tuple[str, ...], Tuple[bool, str, str]]):
        self.responses = responses
        self.calls: List[List[str]] = []

    def __call__(self, command: List[str], cwd: str) -> Tuple[bool, str, str]:
        self.calls.append(command)
        key = tuple(command)
        for prefix, response in self.responses.items():
            if key[: len(prefix)] == prefix:
                return response
        return False, "", f"unexpected command: {command}"


_FOR_EACH_REF_FMT = (
    "git",
    "for-each-ref",
    "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
)


def test_branches_behind_all_current():
    runner = MockRunner(
        {
            _FOR_EACH_REF_FMT: (
                True,
                "main\torigin/main\t\ndevelop\torigin/develop\t\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    behind, indeterminate = git.branches_behind({"main": "origin/main", "develop": "origin/develop"})
    assert behind == {}
    assert indeterminate is False
    assert len(runner.calls) == 1


def test_branches_behind_some_behind():
    runner = MockRunner(
        {
            _FOR_EACH_REF_FMT: (
                True,
                "main\torigin/main\t[behind 3]\ndevelop\torigin/develop\t\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    behind, indeterminate = git.branches_behind({"main": "origin/main", "develop": "origin/develop"})
    assert behind == {"main": 3}
    assert indeterminate is False


def test_branches_behind_indeterminate():
    runner = MockRunner(
        {
            _FOR_EACH_REF_FMT: (
                True,
                "main\torigin/main\t[gone]\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    behind, indeterminate = git.branches_behind({"main": "origin/main"})
    assert behind == {}
    assert indeterminate is True


def test_list_branch_tracking_status_combined():
    runner = MockRunner(
        {
            _FOR_EACH_REF_FMT: (
                True,
                "main\torigin/main\t[behind 2]\nfeature\torigin/feature\t[gone]\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    tracking, behind, indeterminate = git.list_branch_tracking_status()
    assert tracking == {"main": "origin/main", "feature": "origin/feature"}
    assert behind == {"main": 2}
    assert indeterminate == {"feature"}


def test_check_remote_freshness_matches():
    runner = MockRunner(
        {
            ("git", "ls-remote", "origin", "refs/heads/main"): (True, "sha1\trefs/heads/main\n", ""),
            ("git", "rev-parse", "origin/main"): (True, "sha1", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    all_match, check_ok = git.check_remote_freshness({"main": "origin/main"})
    assert all_match is True
    assert check_ok is True


def test_check_remote_freshness_batches_rev_parse():
    runner = MockRunner(
        {
            ("git", "ls-remote", "origin", "refs/heads/main", "refs/heads/develop"): (
                True,
                "sha1\trefs/heads/main\nsha2\trefs/heads/develop\n",
                "",
            ),
            ("git", "rev-parse", "origin/main", "origin/develop"): (True, "sha1\nsha2", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    all_match, check_ok = git.check_remote_freshness(
        {"main": "origin/main", "develop": "origin/develop"}
    )
    assert all_match is True
    assert check_ok is True
    rev_parse_calls = [c for c in runner.calls if c[:2] == ["git", "rev-parse"]]
    assert len(rev_parse_calls) == 1
    assert rev_parse_calls[0] == ["git", "rev-parse", "origin/main", "origin/develop"]


def test_check_remote_freshness_differs():
    runner = MockRunner(
        {
            ("git", "ls-remote", "origin", "refs/heads/main"): (True, "sha2\trefs/heads/main\n", ""),
            ("git", "rev-parse", "origin/main"): (True, "sha1", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    all_match, check_ok = git.check_remote_freshness({"main": "origin/main"})
    assert all_match is False
    assert check_ok is True


def test_check_remote_freshness_ls_remote_fails():
    runner = MockRunner(
        {
            ("git", "ls-remote", "origin", "refs/heads/main"): (False, "", "network error"),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    all_match, check_ok = git.check_remote_freshness({"main": "origin/main"})
    assert all_match is False
    assert check_ok is False


def test_fast_forward_branch_dry_run_no_mutating_commands():
    runner = MockRunner(
        {
            ("git", "rev-parse", "develop", "origin/develop"): (True, "aaa\nbbb", ""),
            ("git", "merge-base", "--is-ancestor", "develop", "origin/develop"): (True, "", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner, options=PullOptions(dry_run=True))
    ok, msg, old_sha, new_sha = git.fast_forward_branch("develop", "origin/develop")
    assert ok is True
    assert msg == "would fast-forward"
    assert old_sha == "aaa"
    assert new_sha == "bbb"
    assert not any(call[1] == "update-ref" for call in runner.calls)


def test_pull_current_branch_ff_only():
    runner = MockRunner({("git", "merge", "--ff-only", "@{u}"): (True, "Already up to date.", "")})
    git = GitRepo("/tmp/repo", runner=runner, options=PullOptions(ff_only=True))
    success, stdout, stderr = git.pull_current_branch()
    assert success is True
    assert any(tuple(call) == ("git", "merge", "--ff-only", "@{u}") for call in runner.calls)


def test_pull_current_branch_uses_upstream_ref():
    runner = MockRunner({("git", "merge", "origin/main"): (True, "Updating aaa..bbb", "")})
    git = GitRepo("/tmp/repo", runner=runner)
    success, _, _ = git.pull_current_branch("origin/main")
    assert success is True
    assert runner.calls == [["git", "merge", "origin/main"]]


def test_pull_current_branch_rebase():
    runner = MockRunner({("git", "rebase", "origin/main"): (True, "", "")})
    git = GitRepo("/tmp/repo", runner=runner, options=PullOptions(rebase=True))
    success, _, _ = git.pull_current_branch("origin/main")
    assert success is True
    assert runner.calls == [["git", "rebase", "origin/main"]]


def test_has_uncommitted_changes_single_status():
    runner = MockRunner(
        {
            ("git", "status", "--porcelain=v1", "-uno"): (True, " M file.py\n", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.has_uncommitted_changes() is True
    assert len(runner.calls) == 1


def test_has_uncommitted_changes_clean():
    runner = MockRunner(
        {
            ("git", "status", "--porcelain=v1", "-uno"): (True, "", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.has_uncommitted_changes() is False


def test_stash_changes_reuses_known_dirty():
    runner = MockRunner(
        {
            ("git", "stash", "push", "-m", "Auto-stash by git_pull_all (repo)"): (True, "", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    success, stashed = git.stash_changes("repo", has_tracked=True)
    assert success is True
    assert stashed is True
    assert not any(call[1] == "status" for call in runner.calls)


def test_untracked_would_block_checkout():
    runner = MockRunner(
        {
            ("git", "ls-files", "--others", "--exclude-standard"): (True, "blocked.txt\nnew.txt", ""),
            ("git", "ls-tree", "-r", "--name-only", "main"): (True, "blocked.txt\nother.txt", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    blockers = git.untracked_would_block_checkout("main")
    assert blockers == {"blocked.txt"}


def test_untracked_would_block_checkout_skips_ls_tree_when_clean():
    runner = MockRunner(
        {
            ("git", "ls-files", "--others", "--exclude-standard"): (True, "", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    assert git.untracked_would_block_checkout("main") == set()
    assert not any(call[1] == "ls-tree" for call in runner.calls)


def test_candidate_tracking_branches_includes_current_and_trunks():
    names = candidate_tracking_branches("feature-x", frozenset({"main", "develop"}))
    assert names == ["develop", "feature-x", "main"]


def test_list_branch_tracking_status_scoped_to_candidates():
    runner = MockRunner(
        {
            (
                "git",
                "for-each-ref",
                "--format=%(refname:short)\t%(upstream:short)\t%(upstream:track)",
                "refs/heads/main",
            ): (
                True,
                "main\torigin/main\t[behind 1]\n",
                "",
            ),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    tracking, behind, indeterminate = git.list_branch_tracking_status({"main": "origin/main"})
    assert tracking == {"main": "origin/main"}
    assert behind == {"main": 1}
    assert indeterminate == set()
    assert runner.calls[0][-1] == "refs/heads/main"


def test_get_changed_paths_name_only():
    runner = MockRunner(
        {
            ("git", "diff", "--name-only", "aaa..bbb"): (True, "z.py\na.py\n", ""),
        }
    )
    git = GitRepo("/tmp/repo", runner=runner)
    changes = git.get_changed_paths("aaa", "bbb")
    assert changes == [
        FileChange(path="a.py", insertions=0, deletions=0),
        FileChange(path="z.py", insertions=0, deletions=0),
    ]


def test_fetch_cache_fresh(tmp_path):
    fetch_head = tmp_path / "FETCH_HEAD"
    fetch_head.write_text("deadbeef\t\tbranch 'main' of origin\n", encoding="utf-8")
    runner = MockRunner(
        {
            ("git", "rev-parse", "--git-path", "FETCH_HEAD"): (True, str(fetch_head), ""),
        }
    )
    git = GitRepo(str(tmp_path), runner=runner)
    now = fetch_head.stat().st_mtime + 10
    assert git.fetch_cache_fresh(30, now=now) is True
    assert git.fetch_cache_fresh(5, now=now) is False


def test_targeted_fetch_with_retry_succeeds_after_failure():
    calls = {"n": 0}

    def flaky(command, cwd):
        calls["n"] += 1
        if command[:3] == ["git", "fetch", "--prune"] and calls["n"] == 1:
            return False, "", "network blip"
        if command[:3] == ["git", "fetch", "--prune"]:
            return True, "", ""
        return False, "", f"unexpected: {command}"

    sleeps: list[float] = []
    git = GitRepo("/tmp/repo", runner=flaky)
    ok, err = git.targeted_fetch_with_retry(
        {"main": "origin/main"}, retries=2, sleep_fn=sleeps.append
    )
    assert ok is True
    assert err == ""
    assert sleeps == [1.0]
    assert calls["n"] == 2


def test_targeted_fetch_with_retry_exhausts():
    def always_fail(command, cwd):
        return False, "", "down"

    sleeps: list[float] = []
    git = GitRepo("/tmp/repo", runner=always_fail)
    ok, err = git.targeted_fetch_with_retry(
        {"main": "origin/main"}, retries=2, sleep_fn=sleeps.append
    )
    assert ok is False
    assert err == "down"
    assert sleeps == [1.0, 2.0]


def test_normalize_config_validates():
    raw = {
        "parallel": 8,
        "max_age": 900,
        "retry": 2,
        "trunk": ["main", "develop"],
        "ff_only": True,
        "directory": "~/code",
        "bogus": "x",
        "parallel_bad": "nope",
    }
    normalized = normalize_config(raw)
    assert normalized["parallel"] == 8
    assert normalized["max_age"] == 900
    assert normalized["retry"] == 2
    assert normalized["trunk"] == ["main", "develop"]
    assert normalized["ff_only"] is True
    assert "bogus" not in normalized
    assert normalized["directory"].endswith("/code") or "/code" in normalized["directory"]


def test_normalize_config_rejects_negative():
    assert "max_age" not in normalize_config({"max_age": -1})
    assert "retry" not in normalize_config({"retry": -2})
    assert "parallel" not in normalize_config({"parallel": 0})


def test_peek_directory_arg():
    assert peek_directory_arg(["-d", "~/code"]) is not None
    assert peek_directory_arg(["--directory=/tmp/x"]) == "/tmp/x"
    assert peek_directory_arg(["--verbose"]) is None


def test_results_to_summary_dict():
    results = [
        RepoResult(
            repo_path="/code/foo",
            updated=["main"],
            changes_by_branch={"main": [FileChange("a.py", 1, 0)]},
            duration_seconds=1.25,
        )
    ]
    errors = [GitError("/code/bar", "develop", "Fetch", "failed")]
    summary = results_to_summary_dict(errors, results, dry_run=False)
    assert summary["totals"]["repos"] == 1
    assert summary["totals"]["branches_updated"] == 1
    assert summary["repos"][0]["name"] == "foo"
    assert summary["repos"][0]["duration_seconds"] == 1.25
    assert summary["errors"][0]["type"] == "Fetch"
    assert "url" not in summary["repos"][0]

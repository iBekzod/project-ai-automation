"""Git operations against the target repo. Honours DRY_RUN for remote-side actions."""
from __future__ import annotations

import logging
import subprocess
import config

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


def git(*args: str, check: bool = True) -> tuple[int, str, str]:
    """Run a git command inside REPO_PATH. Skips remote-side commands under DRY_RUN."""
    if config.DRY_RUN and args and args[0] in ("push",):
        log.info("[DRY_RUN] skip: git %s", " ".join(args))
        return 0, "", ""
    log.info("git %s", " ".join(args))
    r = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(config.REPO_PATH),
        encoding="utf-8",
        errors="replace",
    )
    stdout = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()
    if r.returncode != 0:
        log.error("git %s failed: %s", args[0], stderr or stdout)
        if check:
            raise GitError(f"git {args[0]} failed: {stderr or stdout}")
    return r.returncode, stdout, stderr


def current_branch() -> str:
    _, out, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    return out


def ensure_clean_worktree():
    """Refuse to proceed if the worktree has uncommitted changes."""
    _, out, _ = git("status", "--porcelain")
    if out:
        raise GitError(
            "Target repo has uncommitted changes; aborting to avoid clobbering work.\n"
            f"{out}"
        )


def ensure_stage_up_to_date():
    """Checkout stage and pull latest."""
    ensure_clean_worktree()
    git("fetch", "origin")
    git("checkout", config.STAGE_BRANCH)
    # ff-only to catch divergence instead of creating a merge commit silently.
    git("pull", "--ff-only", "origin", config.STAGE_BRANCH)


def apply_fix(issue_id: str, files: dict[str, str], summary: str) -> str:
    """Create a branch from stage, write files, commit, push. Returns branch name."""
    if not files:
        raise GitError("apply_fix called with empty files map")

    branch = f"fix/bot-{issue_id}"
    ensure_stage_up_to_date()
    git("checkout", "-b", branch)

    repo_root = config.REPO_PATH.resolve()
    for rel_path, content in files.items():
        rel = rel_path.replace("\\", "/").lstrip("/")
        target = config.REPO_PATH / rel
        # Guard against path traversal outside the repo
        try:
            target.resolve().relative_to(repo_root)
        except ValueError as e:
            raise GitError(f"file path escapes repo: {rel_path}") from e
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        log.info("wrote %s (%d bytes)", target, len(content))

    git("add", "-A")
    commit_msg = f"fix: bot issue {issue_id}\n\n{summary}".strip()
    git("commit", "-m", commit_msg)
    git("push", "-u", "origin", branch)
    return branch


def merge_to_prod() -> bool:
    """Fast-forward (or merge) stage into the prod branch and push."""
    ensure_clean_worktree()
    git("fetch", "origin")
    git("checkout", config.PROD_BRANCH)
    git("pull", "--ff-only", "origin", config.PROD_BRANCH)
    rc, _, err = git(
        "merge", "--no-ff", config.STAGE_BRANCH,
        "-m", f"merge: {config.STAGE_BRANCH} -> {config.PROD_BRANCH} via bot",
        check=False,
    )
    if rc != 0:
        log.error("merge failed: %s", err)
        return False
    git("push", "origin", config.PROD_BRANCH)
    return True


def push_to_branch(branch: str, source_branch: str | None = None) -> tuple[bool, str]:
    """Push the latest bot work into the named branch (prod/dev/stage/custom).

    Flow: ensure clean, fetch, checkout `branch`, fast-forward pull, merge
    `source_branch` (defaults to STAGE_BRANCH) with --no-ff, push. DRY_RUN
    skips only the final `git push` — local merge still happens so the dev
    can inspect with `git log`.

    Returns (ok, human_message).
    """
    source = source_branch or config.STAGE_BRANCH
    try:
        ensure_clean_worktree()
    except GitError as exc:
        return False, str(exc)
    try:
        git("fetch", "origin")
        git("checkout", branch)
        # Pull latest on the target so we merge into the current tip.
        git("pull", "--ff-only", "origin", branch)
    except GitError as exc:
        return False, f"checkout/pull failed on {branch}: {exc}"

    rc, _, err = git(
        "merge", "--no-ff", source,
        "-m", f"merge: {source} -> {branch} via bot /push",
        check=False,
    )
    if rc != 0:
        return False, f"merge {source} -> {branch} failed: {err}"
    try:
        git("push", "origin", branch)
    except GitError as exc:
        return False, f"push failed: {exc}"
    mode = "(DRY_RUN — push skipped)" if config.DRY_RUN else "(pushed to origin)"
    return True, f"{source} -> {branch} {mode}"


def rollback_fix(issue_id: str, branches: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Create a revert commit for the bot-authored commit matching issue_id.

    Searches each candidate branch (default: stage, prod_branch) for a commit
    whose message contains `bot issue <issue_id>`. Reverts the first match
    with a new non-destructive revert commit, then pushes. If the commit
    appears on multiple branches, it's reverted on each that matches.

    Returns (ok, human_message). `ok=True` means at least one branch was
    successfully reverted.
    """
    try:
        ensure_clean_worktree()
    except GitError as exc:
        return False, str(exc)
    target_branches = branches or (config.STAGE_BRANCH, config.PROD_BRANCH)
    git("fetch", "origin")

    reverted_on: list[str] = []
    errors: list[str] = []
    needle = f"bot issue {issue_id}"

    for br in target_branches:
        rc, _, err = git("checkout", br, check=False)
        if rc != 0:
            errors.append(f"{br}: checkout failed: {err}")
            continue
        rc, _, err = git("pull", "--ff-only", "origin", br, check=False)
        if rc != 0:
            errors.append(f"{br}: pull failed: {err}")
            continue
        rc, out, err = git(
            "log", "--format=%H", "--grep", needle, "-n", "1",
            check=False,
        )
        sha = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
        if not sha:
            continue  # this branch doesn't have the commit; try next
        rc, _, err = git("revert", "--no-edit", sha, check=False)
        if rc != 0:
            errors.append(f"{br}: revert {sha[:8]} failed: {err}")
            # Leave the worktree in a half-reverted state? Abort it.
            git("revert", "--abort", check=False)
            continue
        try:
            git("push", "origin", br)
            reverted_on.append(f"{br}@{sha[:8]}")
        except GitError as exc:
            errors.append(f"{br}: push after revert failed: {exc}")

    if reverted_on:
        msg = "Reverted on: " + ", ".join(reverted_on)
        if errors:
            msg += " | errors: " + "; ".join(errors)
        return True, msg
    if errors:
        return False, "Rollback failed: " + "; ".join(errors)
    return False, f"No commit matching 'bot issue {issue_id}' found on {', '.join(target_branches)}"

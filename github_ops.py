"""Minimal GitHub PR helpers. Obeys DRY_RUN."""
from __future__ import annotations

import logging

from github import Github

import config

log = logging.getLogger(__name__)

_gh: Github | None = None
_repo = None
_cached_token: str | None = None
_cached_repo_name: str | None = None


def _get_repo():
    """Return a PyGithub Repository, re-creating the client if token/repo changed.

    Reads config.* on every call so Settings → Save reflects without a restart.
    """
    global _gh, _repo, _cached_token, _cached_repo_name
    token = config.GITHUB_TOKEN
    repo_name = config.GITHUB_REPO
    if not token or not repo_name:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO must be set")
    if _repo is None or _cached_token != token or _cached_repo_name != repo_name:
        _gh = Github(token)
        _repo = _gh.get_repo(repo_name)
        _cached_token = token
        _cached_repo_name = repo_name
    return _repo


def create_and_merge_pr(branch: str, title: str, body: str) -> tuple[str, bool]:
    """Open a PR from `branch` into STAGE_BRANCH and attempt to merge it.

    Returns (url, merged). Under DRY_RUN no PR is created and merged=True is
    returned so the caller still advances to the 'ready for publish' stage.
    """
    stage = config.STAGE_BRANCH
    if config.DRY_RUN:
        log.info("[DRY_RUN] would open PR %s -> %s: %s", branch, stage, title)
        return f"(DRY_RUN) would open PR {branch} -> {stage}", True

    repo = _get_repo()
    pr = repo.create_pull(
        title=title,
        body=body or "(no description)",
        head=branch,
        base=stage,
    )
    log.info("PR opened: %s", pr.html_url)
    try:
        pr.merge(merge_method="squash")
        log.info("PR merged: %s", pr.html_url)
        return pr.html_url, True
    except Exception as exc:  # noqa: BLE001 - surface any merge failure
        log.error("PR merge failed for %s: %s", pr.html_url, exc)
        return pr.html_url, False

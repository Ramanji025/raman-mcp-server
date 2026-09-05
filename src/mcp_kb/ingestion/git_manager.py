"""Git operations: clone, pull and per-file change detection (goal #8)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import yaml
from git import GitCommandError, Repo

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class RepoChange:
    """Files added/modified/deleted between two commits."""

    repo: str
    old_commit: str | None
    new_commit: str
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def is_full(self) -> bool:
        """True if this change represents a first-time full clone (no prior commit)."""
        return self.old_commit is None

    @property
    def touched(self) -> list[str]:
        """Every added or modified file path (excludes deletions)."""
        return [*self.added, *self.modified]


class GitManager:
    """Clones and refreshes microservice repositories under ``repos_root``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.root = Path(settings.repos_root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- authentication ---------------------------------------------------- #
    def _authenticated_url(self, url: str) -> str:
        """Inject the configured username + PAT into an HTTPS remote URL.

        Leaves SSH/git URLs and URLs that already carry credentials untouched.
        Also skipped entirely when ``GIT_USE_SYSTEM_CREDENTIALS=true`` (e.g. a
        cloud VM relying on its own git credential helper / SSH agent).
        """
        if self._settings.git_use_system_credentials:
            return url
        token = self._settings.git_token
        if not token or not url.lower().startswith(("http://", "https://")):
            return url
        parsed = urlparse(url)
        if "@" in parsed.netloc:  # credentials already present in the URL
            return url
        user = self._settings.git_username or "oauth2"
        creds = f"{quote(user, safe='')}:{quote(token, safe='')}"
        netloc = f"{creds}@{parsed.netloc}"
        return urlunparse(parsed._replace(netloc=netloc))

    @staticmethod
    def _redact(url: str) -> str:
        """Hide any embedded credentials before logging a URL."""
        return re.sub(r"//[^/@]+@", "//***@", url)

    # ---- manifest ---------------------------------------------------------- #
    def load_manifest(self) -> list[dict[str, str]]:
        """Read ``config/repos.yaml`` and resolve ``${...}`` tokens.

        Supported tokens: ``${remote_prefix}`` (from the manifest) and
        ``${GIT_USERNAME}`` / ``${GIT_TOKEN}`` (from the environment/settings).
        """
        path = self._settings.repos_manifest
        if not path.exists():
            log.warning("manifest_missing", path=str(path))
            return []
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defaults = raw.get("defaults", {})
        prefix = defaults.get("remote_prefix", "")
        branch = defaults.get("branch") or self._settings.git_default_branch
        subs = {
            "remote_prefix": prefix,
            "GIT_USERNAME": self._settings.git_username or "",
            "GIT_TOKEN": self._settings.git_token or "",
        }
        repos = []
        for entry in raw.get("repositories", []):
            url = entry.get("url") or ""
            for key, value in subs.items():
                url = url.replace(f"${{{key}}}", value)
            repos.append(
                {"name": entry["name"], "url": url, "branch": entry.get("branch", branch)}
            )
        return repos

    # ---- clone / pull ------------------------------------------------------ #
    def clone_or_pull(self, name: str, url: str, branch: str = "main") -> RepoChange:
        """Ensure ``name`` is present and up to date; return the diff."""
        dest = self.root / name
        if dest.exists() and (dest / ".git").exists():
            return self.pull(name, branch)
        auth_url = self._authenticated_url(url)
        log.info("cloning", repo=name, url=self._redact(auth_url), branch=branch)
        Repo.clone_from(auth_url, dest, branch=branch, depth=None)
        repo = Repo(dest)
        return RepoChange(repo=name, old_commit=None, new_commit=repo.head.commit.hexsha)

    def pull(self, name: str, branch: str = "main") -> RepoChange:
        """Fast-forward ``name`` and compute changed files (incremental)."""
        dest = self.root / name
        repo = Repo(dest)
        old = repo.head.commit.hexsha
        try:
            # Refresh the origin URL so a rotated PAT is picked up on every pull.
            origin = repo.remotes.origin
            origin.set_url(self._authenticated_url(next(origin.urls)))
            origin.fetch()
            repo.git.checkout(branch)
            origin.pull(branch)
        except GitCommandError as exc:  # offline / detached -> index what we have
            log.warning("git_pull_failed", repo=name, error=str(exc))
            return RepoChange(repo=name, old_commit=None, new_commit=old)
        new = repo.head.commit.hexsha
        if old == new:
            log.info("repo_up_to_date", repo=name, commit=new[:10])
            return RepoChange(repo=name, old_commit=new, new_commit=new)
        return self._diff(repo, name, old, new)

    def diff_against(self, name: str, old_commit: str | None) -> RepoChange:
        """Diff current HEAD against a previously recorded commit."""
        repo = Repo(self.root / name)
        new = repo.head.commit.hexsha
        if old_commit is None or old_commit == new:
            if old_commit is None:
                return RepoChange(repo=name, old_commit=None, new_commit=new)
            return RepoChange(repo=name, old_commit=new, new_commit=new)
        return self._diff(repo, name, old_commit, new)

    @staticmethod
    def _diff(repo: Repo, name: str, old: str, new: str) -> RepoChange:
        change = RepoChange(repo=name, old_commit=old, new_commit=new)
        for d in repo.commit(old).diff(new):
            if d.new_file:
                change.added.append(d.b_path)
            elif d.deleted_file:
                change.deleted.append(d.a_path)
            elif d.renamed_file:
                change.deleted.append(d.a_path)
                change.added.append(d.b_path)
            else:
                change.modified.append(d.b_path)
        log.info(
            "repo_diff", repo=name, added=len(change.added),
            modified=len(change.modified), deleted=len(change.deleted),
        )
        return change

    def current_commit(self, name: str) -> str | None:
        """Return the current HEAD commit hash for `name`, or None if not cloned locally."""
        dest = self.root / name
        if not (dest / ".git").exists():
            return None
        return Repo(dest).head.commit.hexsha

    def discover_local_repos(self) -> list[str]:
        """Any directory under ``repos_root`` that is a git working tree."""
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and (p / ".git").exists()
        )

    # ---- version-aware indexing (goal: "show release 2.7", "compare 2.7 vs 2.8") ---- #
    def list_tags(self, name: str) -> list[str]:
        """All git tags (release tags) available for ``name``, newest first."""
        dest = self.root / name
        if not (dest / ".git").exists():
            return []
        repo = Repo(dest)
        tags = sorted(repo.tags, key=lambda t: t.commit.committed_datetime, reverse=True)
        return [t.name for t in tags]

    def resolve_ref(self, name: str, ref: str) -> dict[str, str | None]:
        """Resolve a branch/tag/commit-ish to its commit SHA + metadata.

        Accepts anything GitPython's ``Repo.commit()`` accepts: a branch name,
        a release tag, a short/long SHA, or ``HEAD~N``. This is what makes
        ``compare_versions()`` version-aware without needing a separate
        "release tag" concept baked into git itself.
        """
        dest = self.root / name
        repo = Repo(dest)
        commit = repo.commit(ref)
        tag_names = [t.name for t in repo.tags if t.commit.hexsha == commit.hexsha]
        return {
            "ref": ref, "commit": commit.hexsha,
            "committed_at": commit.committed_datetime.isoformat(),
            "matching_tags": ", ".join(tag_names) or None,
        }

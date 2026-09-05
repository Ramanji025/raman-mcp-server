"""Unit tests for git PAT authentication and manifest interpolation."""
from __future__ import annotations

from mcp_kb.ingestion.git_manager import GitManager


def test_https_url_gets_username_and_token(settings):
    settings.git_username = "alice"
    settings.git_token = "ghp_secret123"
    gm = GitManager(settings)
    out = gm._authenticated_url("https://github.com/rx/rx-order-service.git")
    assert out == "https://alice:ghp_secret123@github.com/rx/rx-order-service.git"


def test_token_only_falls_back_to_oauth2_user(settings):
    settings.git_username = None
    settings.git_token = "ghp_secret123"
    gm = GitManager(settings)
    out = gm._authenticated_url("https://github.com/rx/x.git")
    assert out == "https://oauth2:ghp_secret123@github.com/rx/x.git"


def test_special_characters_in_token_are_encoded(settings):
    settings.git_username = "user@corp"
    settings.git_token = "p@ss/word:with#chars"
    gm = GitManager(settings)
    out = gm._authenticated_url("https://git.internal/rx/x.git")
    # '@', '/', ':' and '#' must be percent-encoded so the URL stays valid.
    assert "user%40corp:p%40ss%2Fword%3Awith%23chars@git.internal" in out


def test_ssh_and_prewired_urls_are_untouched(settings):
    settings.git_token = "ghp_secret123"
    gm = GitManager(settings)
    ssh = "git@github.com:rx/x.git"
    assert gm._authenticated_url(ssh) == ssh
    prewired = "https://bob:tok@github.com/rx/x.git"
    assert gm._authenticated_url(prewired) == prewired


def test_no_token_leaves_url_unchanged(settings):
    settings.git_token = None
    gm = GitManager(settings)
    url = "https://github.com/rx/x.git"
    assert gm._authenticated_url(url) == url


def test_redaction_hides_credentials(settings):
    gm = GitManager(settings)
    redacted = gm._redact("https://alice:ghp_secret@github.com/rx/x.git")
    assert "ghp_secret" not in redacted
    assert redacted == "https://***@github.com/rx/x.git"


def test_manifest_interpolates_token_and_branch(settings, tmp_path):
    settings.git_username = "alice"
    settings.git_token = "tok123"
    settings.git_default_branch = "develop"
    manifest = tmp_path / "repos.yaml"
    manifest.write_text(
        "defaults:\n"
        "  remote_prefix: https://git.internal/rx\n"
        "repositories:\n"
        "  - name: rx-order-service\n"
        "    url: \"https://${GIT_USERNAME}:${GIT_TOKEN}@git.internal/rx/order.git\"\n"
        "  - name: rx-auth-service\n"
        "    url: \"${remote_prefix}/auth.git\"\n"
        "    branch: release\n",
        encoding="utf-8",
    )
    settings.repos_manifest = manifest
    gm = GitManager(settings)
    repos = {r["name"]: r for r in gm.load_manifest()}
    assert repos["rx-order-service"]["url"] == \
        "https://alice:tok123@git.internal/rx/order.git"
    assert repos["rx-order-service"]["branch"] == "develop"   # default applied
    assert repos["rx-auth-service"]["url"] == "https://git.internal/rx/auth.git"
    assert repos["rx-auth-service"]["branch"] == "release"     # per-repo override

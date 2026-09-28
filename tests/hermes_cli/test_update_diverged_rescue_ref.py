"""`hermes update` must not drop local commits.

When the checkout sits on the update's target branch and its history has
diverged, upstream resets hard to ``origin/<branch>`` behind a rescue ref. Our
fork's install carries its own commits atop upstream on that branch, so every
update diverges and the reset rolled the running agent back to plain upstream
(AIA-45, AIA-47). The update now refuses instead: no reset, HEAD and the working
tree as they were, exit 1. The rescue ref and reset remain only for a checkout
with no local-only commits.
The installer update paths are covered in
``tests/scripts/install/test_install_diverged_rescue_ref.py``.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd


GIT = ["git"]


def _git(repo, *args, check=True):
    return subprocess.run(
        GIT + list(args), cwd=repo, capture_output=True, text=True, check=check)


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def behind_checkout(tmp_path):
    """A checkout on ``main`` one commit behind ``origin/main``, with nothing of its own."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "shared.txt", "shared\n")
    _commit(upstream, "upstream-only.txt", "upstream\n")

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _git(checkout, "config", "user.name", "t")
    _git(checkout, "config", "user.email", "t@example.invalid")
    _git(checkout, "reset", "-q", "--hard", "HEAD~1")          # back to the shared commit
    return checkout


@pytest.fixture()
def diverged_checkout(behind_checkout):
    """A checkout on ``main`` carrying a local commit its ``origin/main`` does not have."""
    local_sha = _commit(behind_checkout, "local-fix.txt", "local\n")  # diverges from origin/main
    return behind_checkout, local_sha


def _rescue_refs(checkout):
    out = _git(checkout, "for-each-ref", "--format=%(refname) %(objectname)",
               "refs/hermes-update-backups/").stdout
    return dict(line.split() for line in out.splitlines() if line.strip())


def test_hermes_update_refuses_when_branch_carries_local_commits(
        diverged_checkout, monkeypatch, capsys):
    """The real apply path: ff-only fails, the reconcile refuses, HEAD stays on the local commit."""
    checkout, local_sha = diverged_checkout
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)

    with pytest.raises(SystemExit) as error:
        update_cmd._pull_updates(
            GIT, "main", None, prompt_for_restore=False, gw_input_fn=None,
            discard_local_changes=False, keep_stash=False)

    assert error.value.code == 1
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == local_sha
    assert _rescue_refs(checkout) == {}, "a refusal moves nothing, so it parks nothing"
    out = capsys.readouterr().out
    assert "Update refused" in out
    assert "1 commit(s) that are not on origin/main" in out
    assert "PR to fork/main" in out, "the refusal must name the upgrade route"
    assert "reset --hard" not in out, "never hand the operator the command that erases them"


def test_refusal_puts_the_autostash_back(diverged_checkout, monkeypatch, capsys):
    """Uncommitted edits are stashed before the pull; a refusal must return them to the tree."""
    checkout, local_sha = diverged_checkout
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    (checkout / "local-fix.txt").write_text("uncommitted edit\n", encoding="utf-8")
    stash_ref = update_cmd._m()._stash_local_changes_if_needed(GIT, checkout)
    assert stash_ref is not None

    with pytest.raises(SystemExit):
        update_cmd._pull_updates(
            GIT, "main", stash_ref, prompt_for_restore=False, gw_input_fn=None,
            discard_local_changes=False, keep_stash=False)

    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == local_sha
    assert (checkout / "local-fix.txt").read_text(encoding="utf-8") == "uncommitted edit\n"
    assert _git(checkout, "stash", "list").stdout == ""
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("✗ Update refused: the code was not updated"), \
        "the restore helper's 'updated codebase' line must not be the last word"


def test_clean_install_still_updates(behind_checkout, monkeypatch):
    """No local commits: the fast-forward runs as upstream intends."""
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", behind_checkout)

    update_cmd._pull_updates(
        GIT, "main", None, prompt_for_restore=False, gw_input_fn=None,
        discard_local_changes=False, keep_stash=False)

    head = _git(behind_checkout, "rev-parse", "HEAD").stdout.strip()
    assert head == _git(behind_checkout, "rev-parse", "origin/main").stdout.strip()


def test_reconcile_still_resets_when_nothing_local_would_be_lost(behind_checkout, monkeypatch):
    """The reset survives for a checkout with no local-only commits (ff failed for another reason)."""
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", behind_checkout)
    pre = _git(behind_checkout, "rev-parse", "HEAD").stdout.strip()

    assert update_cmd._reconcile_diverged_checkout(GIT, "main", pre) is True

    head = _git(behind_checkout, "rev-parse", "HEAD").stdout.strip()
    assert head == _git(behind_checkout, "rev-parse", "origin/main").stdout.strip()

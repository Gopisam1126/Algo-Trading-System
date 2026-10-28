#!/usr/bin/env python
"""Promote a branch, carrying only what the target branch needs.

    python scripts/promote.py --from DEV --to QA
    python scripts/promote.py --from DEV --to QA --dry-run

**Why a script rather than a remembered sequence of git commands.** Promotion
has three easy ways to go wrong quietly: overwriting work that only exists on
the target, promoting a commit CI never saw, and — the reason this file exists
— forgetting to strip the paths the target has no use for. A control that must
be remembered every time is a control that eventually is not.

**What QA does not need.** `Documents/` is design and planning material: specs,
the backlog, the tracker workbook, architecture diagrams. QA exists to verify
the deployable system, and 2.5 MB of Excel and PNGs contribute nothing to that
while making every clone and checkout heavier. They stay on DEV, which is where
they are written and read.

Note this does NOT replace the image-content assertion in `ci.yml`, which
checks that tests, documents and migrations never reach the runtime image.
That check operates on the artifact and stays the authoritative one — an image
is what actually runs. This is the same principle applied one step earlier, to
the branch.

**Safety.** Never force-pushes. Refuses to run if the target has commits the
source does not, because that would silently discard them.

**It promotes DEV -> QA and nothing else** (28 Oct 2026). It used to accept any
``--from``/``--to``, so ``--from DEV --to PROD`` would have pushed DEV's whole
tree - Documents, the lifecycle driver and all - straight to PROD, skipping QA,
while the /sdlc skill's first rule ("never push to PROD") cited promote.yml's
``exit 1`` as the gate: a workflow this script never goes through. The rule was
held up by an instruction alone. PROD is the owner's act, through promote.yml,
which stays blocked on the pre-live checklist.

**And only a commit CI has passed.** The paragraph above names "promoting a
commit CI never saw" as one of three ways promotion goes wrong, and nothing here
checked it. The source commit's CI run is now asked for, and anything but a
completed, successful run - including the API being unreachable - refuses.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

#: Resolved once, absolutely. Same reasoning as `common/secrets.py`: letting
#: subprocess search PATH at exec time means whatever `git` happens to resolve
#: to gets to push to a remote. Resolving explicitly makes the binary being run
#: an observable fact rather than an ambient one.
GIT = shutil.which("git")
if GIT is None:  # pragma: no cover - git is a hard prerequisite
    raise SystemExit("git not found on PATH")

#: Paths stripped from the tree when promoting INTO a given branch.
#: A path listed here must not be read by any test or workflow that runs on
#: that branch — `test_promotion_hygiene.py` asserts that.
EXCLUDED: dict[str, tuple[str, ...]] = {
    # `scripts/tracker` goes with `Documents` rather than being a separate
    # judgement call: it exists only to turn the tracker workbook into the
    # live artifact, and it reads `Documents/BACKLOG_Tracker.xlsx`. Leaving it
    # on a branch that has no `Documents/` would ship a script that cannot run
    # — the exact class of latent breakage `test_promotion_hygiene.py` exists
    # to catch, one directory over.
    # `.claude` is the development process itself — the /sdlc lifecycle
    # driver. It governs how work gets BUILT, which is a question the QA
    # branch does not ask. Same reasoning as `Documents`: QA carries what
    # the system needs to run and be tested, not the material describing
    # how it came to be.
    "QA": ("Documents", "scripts/tracker", ".claude"),
}

#: The only promotions this script performs. PROD is deliberately absent: it
#: is promoted by the owner, through promote.yml, behind the pre-live checklist.
ALLOWED: frozenset[tuple[str, str]] = frozenset({("DEV", "QA")})

#: The workflow whose run must have succeeded on the source commit.
CI_WORKFLOW = "CI"

#: Attribution for the promotion commit this script writes.
ATTRIBUTION = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def refusal(source: str, target: str) -> str | None:
    """Why this promotion must not happen, or ``None`` if it may."""
    if target.upper() == "PROD":
        return (
            "REFUSING: this script never promotes to PROD. PROD means real capital; "
            "it is the owner's decision, made through promote.yml, which stays "
            "blocked until Documents/PRE_LIVE_CHECKLIST.md is signed off."
        )
    if (source, target) not in ALLOWED:
        allowed = ", ".join(f"{s}->{t}" for s, t in sorted(ALLOWED))
        return f"REFUSING: {source}->{target} is not a promotion this script performs ({allowed})."
    return None


def ci_verdict(runs: list[dict[str, object]], sha: str) -> str | None:
    """``None`` if the CI workflow's latest run on ``sha`` succeeded, else why not.

    Takes the decoded ``workflow_runs`` list so it is testable without a network.
    The latest run wins: a re-run that passed after a failure is a pass.
    """
    mine = [
        r for r in runs if r.get("head_sha") == sha and r.get("name") == CI_WORKFLOW
    ]
    if not mine:
        return (
            f"no {CI_WORKFLOW} run found for {sha[:12]} - CI has not seen this commit"
        )
    latest = max(mine, key=lambda r: str(r.get("created_at") or ""))
    if latest.get("status") != "completed":
        return f"{CI_WORKFLOW} on {sha[:12]} is still {latest.get('status')}"
    if latest.get("conclusion") != "success":
        return f"{CI_WORKFLOW} on {sha[:12]} concluded {latest.get('conclusion')!r}"
    return None


def fetch_runs(remote: str, sha: str) -> list[dict[str, object]]:
    """The Actions runs for ``sha``, from GitHub's public API.

    Raises on any failure: an unanswered question is not a green build.
    """
    import json
    import re
    import urllib.request

    url = git("remote", "get-url", remote)
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    if match is None:
        raise SystemExit(
            f"REFUSING: cannot read a GitHub owner/repo from remote {url!r}"
        )
    owner, repo = match.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=20"
    request = urllib.request.Request(
        api, headers={"Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return list(payload.get("workflow_runs", []))


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        [GIT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def filtered_tree(source_ref: str, excluded: tuple[str, ...]) -> str:
    """Build a tree object identical to ``source_ref`` minus ``excluded``.

    Uses a scratch index so the working tree and the real index are untouched —
    running this must never disturb whatever the developer has in progress.
    """
    import os
    import tempfile

    if not excluded:
        return git("rev-parse", f"{source_ref}^{{tree}}")

    with tempfile.TemporaryDirectory() as tmp:
        index = os.path.join(tmp, "promote.index")
        env = dict(os.environ, GIT_INDEX_FILE=index)

        def git_env(*args: str) -> str:
            result = subprocess.run(
                [GIT, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
            )
            if result.returncode != 0:
                raise SystemExit(
                    f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
                )
            return result.stdout.strip()

        git_env("read-tree", source_ref)
        for path in excluded:
            # -r for directories, --ignore-unmatch so a path already absent is
            # not an error — the second promotion after a removal is normal.
            git_env("rm", "-r", "--cached", "--ignore-unmatch", "-q", path)
        return git_env("write-tree")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--to", dest="target", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--message", default=None)
    args = parser.parse_args(argv)

    remote, source, target = args.remote, args.source, args.target
    # Before ANY git command: a refused promotion touches nothing.
    refused = refusal(source, target)
    if refused:
        print(refused)
        return 1
    excluded = EXCLUDED.get(target, ())

    print(f"Promoting {source} -> {target}")
    git("fetch", "-q", remote)

    source_ref = f"{remote}/{source}"
    target_ref = f"{remote}/{target}"
    source_sha = git("rev-parse", source_ref)
    target_sha = git("rev-parse", target_ref)

    # --- CI must have passed this exact commit ------------------------------
    try:
        why_not = ci_verdict(fetch_runs(remote, source_sha), source_sha)
    except SystemExit:
        raise
    # Deliberately total: a network error, a rate limit and a malformed reply all
    # mean "we do not know whether CI passed", and that must refuse, not pass.
    except Exception as exc:  # noqa: BLE001
        why_not = (
            f"could not ask GitHub whether CI passed ({type(exc).__name__}: {exc})"
        )
    if why_not:
        print(f"\nREFUSING: {why_not}. Promote only a commit CI has passed.")
        return 1
    print(f"  {CI_WORKFLOW} passed on {source_sha[:12]}")

    # --- divergence -------------------------------------------------------
    # Only non-merge commits count: every promotion is itself a merge commit on
    # the target, so `git log target..source` is never empty in a healthy repo.
    diverged = git("log", "--no-merges", "--oneline", f"{source_ref}..{target_ref}")
    if diverged:
        print("\nREFUSING: the target has commits the source does not:\n")
        print(diverged)
        print(f"\nPromoting would discard them. Merge them back into {source} first.")
        return 1
    print("  no divergence")

    ahead = git("rev-list", "--count", f"{target_ref}..{source_ref}")
    tree = filtered_tree(source_ref, excluded)
    current_tree = git("rev-parse", f"{target_ref}^{{tree}}")

    # The question is whether the TARGET'S TREE is already what it should be —
    # not whether the source is ahead. A target level with the source can still
    # be carrying paths that need stripping, which is the exact state this
    # script was written to correct.
    if tree == current_tree:
        print(f"  {target} already carries exactly the right tree — nothing to do")
        return 0
    if ahead == "0":
        print(f"  {target} is level with {source} but its tree differs — re-promoting")
    else:
        print(f"  {source} is {ahead} commit(s) ahead")

    if excluded:
        print(f"  stripped from the {target} tree: {', '.join(excluded)}")

    if args.dry_run:
        print(f"\nDRY RUN — would create a commit on {target} with tree {tree[:12]}")
        for path in excluded:
            present = git("ls-tree", "--name-only", tree, path, check=False)
            print(f"    {path}: {'STILL PRESENT' if present else 'absent'}")
        return 0

    message = args.message or (
        f"Promote {source} to {target}\n\n"
        + (
            f"Tree carries {source}'s contents minus "
            f"{', '.join(excluded)}, which {target} has no use for.\n"
            if excluded
            else ""
        )
        + "\n"
        + ATTRIBUTION
    )
    commit = git("commit-tree", tree, "-p", target_sha, "-p", source_sha, "-m", message)
    git("push", "-q", remote, f"{commit}:refs/heads/{target}")
    git("fetch", "-q", remote)

    # --- verify -----------------------------------------------------------
    new_target = git("rev-parse", target_ref)
    if new_target != commit:
        print(
            f"\nPUSH DID NOT LAND: {target} is {new_target[:12]}, expected {commit[:12]}"
        )
        return 1

    expected = filtered_tree(source_ref, excluded)
    actual = git("rev-parse", f"{target_ref}^{{tree}}")
    if expected != actual:
        print(f"\nTREE MISMATCH: {target} is {actual[:12]}, expected {expected[:12]}")
        return 1

    print(f"\n  {source} {source_sha[:8]} -> {target} {new_target[:8]}")
    print("  tree verified against the filtered source")
    for path in excluded:
        still = git("ls-tree", "--name-only", f"{target_ref}", path, check=False)
        status = "STILL PRESENT" if still else "absent"
        print(f"    {path}: {status}")
        if still:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

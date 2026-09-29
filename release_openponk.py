#!/usr/bin/env python3
"""
OpenPonk Multi-Repository Release Automation Tool.

Coordinates Git branching, baseline dependency rewrites, and tagging
across configured OpenPonk ecosystem repositories using native Git CLI.
Operates completely external to the Pharo runtime image.

WORKFLOW AND USAGE (PIPELINE STAGES):
------------------------------------
1. Level 0: Remote Synchronization:
   Pulls latest upstream commits on development branches (master/main):
     python release_openponk.py --pull

2. Level 1: Pre-flight Audit (Read-Only):
   Verifies working tree cleanliness and displays planned baseline rewrites:
     python release_openponk.py --check
     python release_openponk.py --tag v5.1.0 --check

3. Level 2: Local Release Execution:
   Checks out release_base, merges development branch (-X theirs),
   rewrites baseline URLs in Tonel files, commits, and creates local tags:
     python release_openponk.py --local
     python release_openponk.py --tag v5.1.0 --local
   (Default mode when no stage flag is specified is --local)

4. Level 3: Remote Publication:
   Pushes release_base branch and generated tags to origin across all repos:
     python release_openponk.py --push
     python release_openponk.py --tag v5.1.0 --push

Rollback / Reset:
   Aborts active merges, checks out dev branch, and removes local tags:
     python release_openponk.py --reset
     python release_openponk.py --tag v5.1.0 --reset
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# =============================================================================
# RELEASE CONFIGURATION AND REPOSITORIES
# =============================================================================

DEFAULT_RELEASE_TAG: str = "v5.1.0"

ORDERED_REPOSITORIES: List[str] = [
    "synchronized-links",
    "uml-metamodel",
    "xml-dom-visitor",
    "pharo-changes-builder",
    "openponk",
    "openponk-model",
    "openponk-git",
    "xmi",
    "uml-xmi",
    "uml-bootstrap-generator",
    "uml-profiles",
    "ontouml-profile",
    "ontouml-modelquery",
    "ontouml-verifications",
    "openponk-ontouml-to-uml-transformation",
    "ontouml-patterns",
    "class-editor",
    "simulation",
    "borm-model",
    "borm-editor",
    "markov-chains",
    "petrinets",
    "BPMN",
    "fsm-editor",
    "ERD",
    "DEMO",
    "plugins",
]

CANONICAL_REPOSITORIES: Dict[str, str] = {
    repository_name.lower(): repository_name
    for repository_name in ORDERED_REPOSITORIES
}

KNOWN_EXTERNAL_MAPPINGS: Dict[str, str] = {
    "pharo-contributions/xml-xmlparser": "github://pharo-contributions/XML-XMLParser:v3.6.x/src",
    "pharo-contributions/xml-xpath": "github://pharo-contributions/XML-XPath:v2.3.0",
    "pharo-contributions/xml-xmlwriter": "github://pharo-contributions/XML-XMLWriter:v3.1.x/src",
    "moosetechnology/petitparser": "github://moosetechnology/PetitParser:v3.0.0",
    "janbliznicenko/magritte": "github://JanBliznicenko/magritte:dcb9e61",
}


# =============================================================================
# GIT CLI ENGINE
# =============================================================================

def execute_git_command(
    command_arguments: List[str],
    working_directory: Path,
) -> Tuple[int, str, str]:
    """
    Executes a Git CLI command in the specified working directory.
    """
    completed_process = subprocess.run(
        ["git"] + command_arguments,
        cwd=working_directory,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (
        completed_process.returncode,
        completed_process.stdout.strip(),
        completed_process.stderr.strip(),
    )


# =============================================================================
# TONEL BASELINE REWRITING
# =============================================================================

def rewrite_dependency_url(
    raw_url_string: str,
    target_version_tag: str,
) -> str:
    trimmed_url = raw_url_string.strip()
    lowercase_url = trimmed_url.lower()

    openponk_scheme_prefix = "github://openponk/"
    if lowercase_url.startswith(openponk_scheme_prefix):
        remaining_path = trimmed_url[len(openponk_scheme_prefix):]
        if "/" in remaining_path:
            raw_repository_name, subpath = remaining_path.split("/", 1)
            formatted_subpath = "/" + subpath
        else:
            raw_repository_name = remaining_path
            formatted_subpath = ""

        clean_repository_name = raw_repository_name.split(":")[0]
        canonical_name = CANONICAL_REPOSITORIES.get(
            clean_repository_name.lower(),
            clean_repository_name,
        )
        return (
            f"github://OpenPonk/{canonical_name}:"
            f"{target_version_tag}{formatted_subpath}"
        )

    generic_scheme_prefix = "github://"
    if lowercase_url.startswith(generic_scheme_prefix):
        path_after_scheme = trimmed_url[len(generic_scheme_prefix):]
        if "/" in path_after_scheme:
            repository_owner, repository_rest = path_after_scheme.split("/", 1)
            repository_with_version = repository_rest.split("/")[0]
            repository_name_only = repository_with_version.split(":")[0]
            lookup_key = f"{repository_owner}/{repository_name_only}".lower()
            if lookup_key in KNOWN_EXTERNAL_MAPPINGS:
                return KNOWN_EXTERNAL_MAPPINGS[lookup_key]

    return trimmed_url


def rewrite_tonel_file_content(
    source_content: str,
    target_version_tag: str,
) -> Tuple[str, bool, List[str]]:
    """
    Scans Tonel file contents for URL string literals and rewrites them.
    Returns the rewritten content, a modification boolean, and a change list.
    """
    recorded_changes: List[str] = []

    def string_literal_replacement(match_object: re.Match) -> str:
        original_url = match_object.group(1)
        rewritten_url = rewrite_dependency_url(original_url, target_version_tag)
        if original_url != rewritten_url:
            recorded_changes.append(f"{original_url} -> {rewritten_url}")
            return f"'{rewritten_url}'"
        return match_object.group(0)

    url_literal_pattern = re.compile(r"'((?:github)://[^']+)'", re.IGNORECASE)
    transformed_content = url_literal_pattern.sub(
        string_literal_replacement,
        source_content,
    )
    is_modified = len(recorded_changes) > 0
    return transformed_content, is_modified, recorded_changes


def locate_repository_directory(
    root_directory: Path,
    repository_name: str,
) -> Optional[Path]:
    """
    Resolves repository directory by exact or case-insensitive matching.
    """
    exact_path = root_directory / repository_name
    if exact_path.is_dir() and (exact_path / ".git").is_dir():
        return exact_path

    for child_path in root_directory.iterdir():
        if (
            child_path.is_dir()
            and child_path.name.lower() == repository_name.lower()
            and (child_path / ".git").is_dir()
        ):
            return child_path
    return None


def derive_major_version_tag(version_tag: str) -> str:
    """
    Derives floating major version tag (e.g. v5.x) from semantic version tag.
    """
    if "." in version_tag:
        return version_tag.split(".")[0] + ".x"
    return version_tag + ".x"


# =============================================================================
# PIPELINE STAGES
# =============================================================================

def pull_single_repository(
    repository_name: str,
    repository_path: Path,
) -> bool:
    """
    Level 0: Synchronizes development branch with remote origin.
    """
    print(f"\n--- [PULL: {repository_name}] ---")

    exit_code, standard_output, standard_error = execute_git_command(
        ["status", "--porcelain"],
        repository_path,
    )
    if exit_code != 0:
        print(f"  ERROR: git status failed: {standard_error}")
        return False
    if standard_output:
        print(f"  ERROR: Working copy has uncommitted changes:\n{standard_output}")
        return False

    _, main_branch_match, _ = execute_git_command(
        ["branch", "--list", "main"],
        repository_path,
    )
    development_branch = "main" if main_branch_match else "master"

    exit_code, _, standard_error = execute_git_command(
        ["checkout", development_branch],
        repository_path,
    )
    if exit_code != 0:
        print(f"  ERROR: Failed to switch to {development_branch}: {standard_error}")
        return False

    print(f"  Pulling origin/{development_branch}...")
    exit_code, pull_stdout, pull_stderr = execute_git_command(
        ["pull", "--ff-only", "origin", development_branch],
        repository_path,
    )
    if exit_code != 0:
        print(f"  ERROR: Fast-forward pull failed:\n{pull_stdout}\n{pull_stderr}")
        return False

    print(f"  [OK] {pull_stdout or 'Already up to date.'}")
    return True


def check_single_repository(
    repository_name: str,
    repository_path: Path,
    target_version_tag: str,
) -> bool:
    """
    Level 1: Read-only inspection of repository state and baselines.
    """
    print(f"\n--- [CHECK: {repository_name}] ---")

    exit_code, standard_output, standard_error = execute_git_command(
        ["status", "--porcelain"],
        repository_path,
    )
    if exit_code != 0:
        print(f"  ERROR: git status failed: {standard_error}")
        return False
    if standard_output:
        print(f"  WARNING: Working copy is dirty:\n{standard_output}")
        return False

    _, current_branch_name, _ = execute_git_command(
        ["branch", "--show-current"],
        repository_path,
    )
    _, main_branch_match, _ = execute_git_command(
        ["branch", "--list", "main"],
        repository_path,
    )
    development_branch = "main" if main_branch_match else "master"
    print(f"  Branch: {development_branch} (currently on: {current_branch_name or 'DETACHED'})")

    baseline_files = [
        candidate_path
        for candidate_path in repository_path.rglob("BaselineOf*.class.st")
        if ".git" not in candidate_path.parts
    ]
    for baseline_file_path in baseline_files:
        try:
            original_content = baseline_file_path.read_text(encoding="utf-8", newline="")
        except Exception as read_exception:
            print(f"  ERROR reading {baseline_file_path}: {read_exception}")
            return False

        _, has_changes, changes = rewrite_tonel_file_content(original_content, target_version_tag)
        if has_changes:
            print(f"  Baseline rewrite in {baseline_file_path.name}:")
            for change in changes:
                print(f"    * {change}")

    print("  [OK] Clean and valid.")
    return True


def release_single_repository(
    repository_name: str,
    repository_path: Path,
    target_version_tag: str,
    major_version_tag: str,
) -> bool:
    """
    Level 2: Idempotent local release execution.
    """
    print(f"\n--- [{repository_name}] ({repository_path}) ---")

    # Step 1: Pre-flight cleanliness verification
    exit_code, standard_output, standard_error = execute_git_command(
        ["status", "--porcelain"],
        repository_path,
    )
    if exit_code != 0:
        print(f"  ERROR: git status failed: {standard_error}")
        return False
    if standard_output:
        print(f"  ERROR: Working copy has uncommitted changes:\n{standard_output}")
        return False

    # Step 2: Determine development branch
    _, current_branch_name, _ = execute_git_command(
        ["branch", "--show-current"],
        repository_path,
    )
    _, main_branch_match, _ = execute_git_command(
        ["branch", "--list", "main"],
        repository_path,
    )
    development_branch = "main" if main_branch_match else "master"

    if current_branch_name != development_branch:
        exit_code, _, standard_error = execute_git_command(
            ["checkout", development_branch],
            repository_path,
        )
        if exit_code != 0:
            print(f"  ERROR: Failed to switch to {development_branch}: {standard_error}")
            return False

    # Step 3: Ensure release_base exists locally
    _, local_release_match, _ = execute_git_command(
        ["branch", "--list", "release_base"],
        repository_path,
    )
    has_local_release = bool(local_release_match)

    _, remote_release_match, _ = execute_git_command(
        ["branch", "-r", "--list", "origin/release_base"],
        repository_path,
    )
    has_remote_release = bool(remote_release_match)

    if not has_local_release:
        if has_remote_release:
            print("  Creating local release_base tracking origin/release_base...")
            exit_code, _, standard_error = execute_git_command(
                ["checkout", "-b", "release_base", "origin/release_base"],
                repository_path,
            )
            if exit_code != 0:
                print(f"  ERROR: Failed to create tracking branch: {standard_error}")
                return False
        else:
            print(f"  Creating new release_base branch from {development_branch}...")
            exit_code, _, standard_error = execute_git_command(
                ["checkout", "-b", "release_base"],
                repository_path,
            )
            if exit_code != 0:
                print(f"  ERROR: Failed to create release_base branch: {standard_error}")
                return False
    else:
        print("  Checking out existing release_base branch...")
        exit_code, _, standard_error = execute_git_command(
            ["checkout", "release_base"],
            repository_path,
        )
        if exit_code != 0:
            print(f"  ERROR: Failed to switch to release_base: {standard_error}")
            return False

    # Step 4: Merge development branch using -X theirs to prioritize incoming features
    print(f"  Merging {development_branch} into release_base (strategy: -X theirs)...")
    exit_code, standard_output, standard_error = execute_git_command(
        [
            "merge",
            development_branch,
            "-X",
            "theirs",
            "-m",
            f"Merge branch '{development_branch}' into release_base",
        ],
        repository_path,
    )
    if exit_code != 0:
        error_details = standard_output if standard_output else standard_error
        print(f"  ERROR: Merge failed:\n{error_details}")
        execute_git_command(["merge", "--abort"], repository_path)
        execute_git_command(["checkout", development_branch], repository_path)
        return False

    # Step 5: Rewrite Tonel baseline files
    baseline_files = [
        candidate_path
        for candidate_path in repository_path.rglob("BaselineOf*.class.st")
        if ".git" not in candidate_path.parts
    ]
    modified_baseline_files: List[Path] = []

    for baseline_file_path in baseline_files:
        try:
            original_file_content = baseline_file_path.read_text(
                encoding="utf-8",
                newline="",
            )
        except Exception as read_exception:
            print(f"  WARNING: Unable to read {baseline_file_path}: {read_exception}")
            continue

        (
            transformed_file_content,
            content_changed,
            applied_rewrites,
        ) = rewrite_tonel_file_content(
            original_file_content,
            target_version_tag,
        )

        if content_changed:
            modified_baseline_files.append(baseline_file_path)
            print(f"  Rewriting baseline in {baseline_file_path.name}:")
            for rewrite_description in applied_rewrites:
                print(f"    * {rewrite_description}")

            baseline_file_path.write_text(
                transformed_file_content,
                encoding="utf-8",
                newline="",
            )

    # Step 6: Commit baseline modifications if any occurred
    if modified_baseline_files:
        print(f"  Committing baseline updates ({target_version_tag})...")
        relative_file_paths = [
            str(file_path.relative_to(repository_path))
            for file_path in modified_baseline_files
        ]
        execute_git_command(["add"] + relative_file_paths, repository_path)
        exit_code, _, standard_error = execute_git_command(
            ["commit", "-m", f"Release {target_version_tag}"],
            repository_path,
        )
        if exit_code != 0:
            print(f"  ERROR: Commit failed: {standard_error}")
            execute_git_command(["checkout", development_branch], repository_path)
            return False

    # Step 7: Create semantic and floating major tags (idempotent / overwrite if matching)
    print(f"  Tagging: {target_version_tag} and {major_version_tag}...")
    execute_git_command(["tag", "-f", target_version_tag], repository_path)
    execute_git_command(["tag", "-f", major_version_tag], repository_path)

    # Step 8: Return working copy to development branch
    print(f"  Returning to {development_branch}...")
    execute_git_command(["checkout", development_branch], repository_path)

    print(f"  [SUCCESS] Repository {repository_name} released cleanly.")
    return True


def reset_single_repository(
    repository_name: str,
    repository_path: Path,
    target_version_tag: str,
    major_version_tag: str,
) -> None:
    """
    Rolls back local tags and aborts any active merge.
    """
    _, main_match, _ = execute_git_command(["branch", "--list", "main"], repository_path)
    dev_branch = "main" if main_match else "master"

    execute_git_command(["merge", "--abort"], repository_path)
    execute_git_command(["checkout", dev_branch], repository_path)
    execute_git_command(["tag", "-d", target_version_tag], repository_path)
    execute_git_command(["tag", "-d", major_version_tag], repository_path)
    print(f"  [RESET] {repository_name} reset to {dev_branch}, local tags removed.")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main() -> None:
    default_root_path = Path(__file__).resolve().parent.parent

    argument_parser = argparse.ArgumentParser(
        description="Automated OpenPonk Release Pipeline (Multi-Stage Architecture)"
    )
    argument_parser.add_argument(
        "--root",
        type=Path,
        default=default_root_path,
        help=f"Root directory containing repository directories (default: {default_root_path})",
    )
    argument_parser.add_argument(
        "--tag",
        default=DEFAULT_RELEASE_TAG,
        help=f"Target semantic release tag (default: {DEFAULT_RELEASE_TAG})",
    )
    argument_parser.add_argument(
        "--pull",
        action="store_true",
        help="Level 0: Pull latest changes from remote origin on development branch",
    )
    argument_parser.add_argument(
        "--check",
        action="store_true",
        help="Level 1: Read-only pre-flight inspection across all repositories",
    )
    argument_parser.add_argument(
        "--local",
        action="store_true",
        help="Level 2: Execute local release (branch, merge -X theirs, baselines, tags)",
    )
    argument_parser.add_argument(
        "--push",
        action="store_true",
        help="Level 3: Push release_base and tags to origin across all repositories",
    )
    argument_parser.add_argument(
        "--reset",
        action="store_true",
        help="Rollback: Abort unmerged states, return to dev branch, delete local tags",
    )

    parsed_arguments = argument_parser.parse_args()
    root_directory: Path = parsed_arguments.root
    target_version_tag: str = parsed_arguments.tag
    major_version_tag: str = derive_major_version_tag(target_version_tag)

    mode_pull = parsed_arguments.pull
    mode_check = parsed_arguments.check
    mode_push = parsed_arguments.push
    mode_reset = parsed_arguments.reset
    mode_local = parsed_arguments.local or (
        not mode_pull and not mode_check and not mode_push and not mode_reset
    )

    print("=================================================================")
    print(f"OpenPonk Release Pipeline: {target_version_tag} ({major_version_tag})")
    print(f"Root path: {root_directory}")
    if mode_reset:
        print("Mode: ROLLBACK / RESET")
    elif mode_pull:
        print("Mode: LEVEL 0 - PULL REPOSITORIES")
    elif mode_check:
        print("Mode: LEVEL 1 - READ-ONLY CHECK")
    elif mode_push:
        print("Mode: LEVEL 3 - REMOTE PUSH")
    else:
        print("Mode: LEVEL 2 - LOCAL RELEASE")
    print("=================================================================")

    if not root_directory.is_dir():
        print(f"FATAL: Root directory does not exist: {root_directory}")
        sys.exit(1)

    resolved_repositories: List[Tuple[str, Path]] = []
    for repository_name in ORDERED_REPOSITORIES:
        matched_path = locate_repository_directory(root_directory, repository_name)
        if matched_path:
            resolved_repositories.append((repository_name, matched_path))
        else:
            print(f"FATAL: Missing repository directory: {repository_name}")
            sys.exit(1)

    if mode_reset:
        for name, path in resolved_repositories:
            reset_single_repository(name, path, target_version_tag, major_version_tag)
        print("\nAll repositories reset successfully.")
        return

    if mode_pull:
        all_succeeded = True
        for name, path in resolved_repositories:
            if not pull_single_repository(name, path):
                all_succeeded = False
        print("\n=================================================================")
        print(f"Pull summary: {'ALL REPOSITORIES UP TO DATE' if all_succeeded else 'PULL ERRORS DETECTED'}")
        if not all_succeeded:
            sys.exit(1)
        return

    if mode_check:
        all_passed = True
        for name, path in resolved_repositories:
            if not check_single_repository(name, path, target_version_tag):
                all_passed = False
        print("\n=================================================================")
        print(f"Check summary: {'ALL CHECKS PASSED' if all_passed else 'ERRORS DETECTED'}")
        if not all_passed:
            sys.exit(1)
        return

    if mode_local:
        successful_count = 0
        for name, path in resolved_repositories:
            is_ok = release_single_repository(
                name,
                path,
                target_version_tag,
                major_version_tag,
            )
            if is_ok:
                successful_count += 1
            else:
                print(f"\nABORTING: Failed at repository {name}!")
                sys.exit(1)

        print("\n=================================================================")
        print(f"Local Release Summary: {successful_count} / {len(resolved_repositories)} repositories released locally.")
        print("\nReady for Level 3: Run with --push to publish to remote origins.")

    if mode_push:
        import time

        print("\nExecuting Level 3: Remote Push to origin...")
        refspecs = [
            "release_base",
            f"refs/tags/{target_version_tag}",
            f"+refs/tags/{major_version_tag}",
        ]

        for name, path in resolved_repositories:
            print(f"  Pushing {name}...")
            exit_code, stdout, stderr = execute_git_command(
                ["push", "origin"] + refspecs,
                path,
            )
            if exit_code != 0:
                print(f"  Warning: Push encountered error, retrying in 3 seconds ({name})...")
                time.sleep(3)
                exit_code, stdout, stderr = execute_git_command(
                    ["push", "origin"] + refspecs,
                    path,
                )

            if exit_code != 0:
                print(f"  ERROR: Push failed for {name}:\n{stderr or stdout}")
                sys.exit(1)

        print(f"\nAll {len(resolved_repositories)} repositories pushed successfully to GitHub.")


if __name__ == "__main__":
    main()
"""Guards for the dependency supply-chain configuration.

These tests protect the install-time security posture from silently
regressing: every locked dependency must be hash-pinned, the resolver
cooldown must stay configured, and the install path must keep using
`--require-hashes`, `--only-binary=:all:` and `--uploaded-prior-to`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-build.txt",
)

# `name==version[ ; marker]` opening a pinned requirement block.
PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s;]+(\s*;.*)?$")


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirement_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Return `(requirement_line, hash_lines)` pairs found in an export."""
    blocks: list[tuple[str, list[str]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().removesuffix("\\").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if blocks:
                blocks[-1][1].append(line)
            continue
        if line.startswith("-"):
            continue
        blocks.append((line, []))
    return blocks


@pytest.mark.parametrize("filename", REQUIREMENTS_FILES)
def test_requirements_export_exists(filename: str) -> None:
    assert (REPO_ROOT / filename).is_file(), f"{filename} is missing; run scripts/lock_deps.sh"


@pytest.mark.parametrize("filename", REQUIREMENTS_FILES)
def test_every_requirement_is_version_pinned_and_hashed(filename: str) -> None:
    text = (REPO_ROOT / filename).read_text(encoding="utf-8")
    blocks = _requirement_blocks(text)

    assert blocks, f"{filename} contains no requirements"

    for requirement, hashes in blocks:
        assert PINNED_REQUIREMENT.match(requirement), (
            f"{filename}: {requirement!r} is not pinned with '=='; "
            "an unpinned requirement defeats --require-hashes"
        )
        assert hashes, f"{filename}: {requirement!r} has no --hash lines"
        assert all(h.startswith("--hash=sha256:") for h in hashes), (
            f"{filename}: {requirement!r} uses a non-sha256 hash"
        )


def test_uv_cooldown_is_configured() -> None:
    uv_config = _pyproject()["tool"]["uv"]

    assert "exclude-newer" in uv_config, (
        "pyproject.toml [tool.uv] must set exclude-newer so freshly published "
        "(and potentially compromised) releases are not resolved"
    )
    assert uv_config["exclude-newer"], "exclude-newer must not be empty or disabled"


def test_uv_cooldown_has_no_unexpected_per_package_exemptions() -> None:
    """Per-package overrides bypass the cooldown, so they must stay deliberate."""
    exemptions = _pyproject()["tool"]["uv"].get("exclude-newer-package", {})

    assert exemptions == {}, (
        "unexpected exclude-newer-package exemptions bypass the cooldown guard: "
        f"{sorted(exemptions)}"
    )


def test_build_group_mirrors_build_system_requires() -> None:
    """`--no-build-isolation` only works if build deps are in the lock."""
    pyproject = _pyproject()
    build_group = pyproject["dependency-groups"]["build"]
    build_requires = pyproject["build-system"]["requires"]

    def names(specs: list[str]) -> set[str]:
        return {re.split(r"[<>=!~\[ ]", spec, maxsplit=1)[0].lower() for spec in specs}

    missing = names(build_requires) - names(build_group)
    assert not missing, (
        f"[build-system].requires entries missing from the 'build' dependency group: {missing}"
    )


def test_install_script_applies_all_guards() -> None:
    script = (REPO_ROOT / "scripts" / "install_deps.sh").read_text(encoding="utf-8")

    for flag in ("--require-hashes", "--only-binary=:all:", "--uploaded-prior-to=P7D", "--no-deps"):
        assert flag in script, f"scripts/install_deps.sh no longer passes {flag}"

    assert "--no-build-isolation" in script, (
        "the editable install must skip build isolation, otherwise pip fetches "
        "an unverified build backend from the index"
    )


def test_ci_workflow_uses_hardened_install_and_sha_pinned_actions() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file(), "CI workflow is missing"

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "scripts/install_deps.sh --dev" in workflow, (
        "CI must install through the hardened script, not a bare pip install"
    )

    uses_lines = [
        line.split("uses:", 1)[1].split("#", 1)[0].strip()
        for line in workflow.splitlines()
        if line.strip().startswith("- uses:") or line.strip().startswith("uses:")
    ]
    assert uses_lines, "CI workflow references no actions"

    for action in uses_lines:
        _, _, ref = action.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"action {action!r} must be pinned to a full 40-character commit SHA, "
            "because a tag can be repointed at attacker-controlled code"
        )

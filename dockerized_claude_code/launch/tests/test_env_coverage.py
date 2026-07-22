"""Verify the build-arg / env-var wiring between the launcher's Python
taxonomy, each layer's `tag.docker`, and the Dockerfiles.

Catches orphan references that would otherwise silently degrade at build
time (an unforwarded ARG keeps its Dockerfile default; a typo'd forward
name never matches a staged value). Four checks:

1. `${VAR}` references in every Dockerfile — must be a ContainerEnvKey, an
   INSTALL_<TOOL> from toolkit_install_flags (profile-driven toolchains) or
   install_creds_flags (creds-driven CLIs), a token env-var from
   OPTIONAL_CREDS_TOKEN_ENV_VARS, PARENT_IMAGE (threaded by ensure_image),
   or in the allowlist of build-time-default ARGs / RUN-local shell vars.
2. `ARG VAR` declarations — same allowed set.
3. Each layer's `[build] arg_forward` names must resolve: a plain name is a
   declared ARG in that layer's Dockerfile AND a launcher-staged var; a glob
   must match at least one staged var.
4. Reverse direction: every launcher-staged ARG a Dockerfile declares must
   be forwarded by its layer's tag.docker — otherwise the staged value
   silently drops at build time.
"""

import fnmatch
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.container_env import ContainerEnvKey, install_creds_flags, toolkit_install_flags
from launch.tags import scan_all

REGISTRY = scan_all(paths.AGENTS_DIR)

# Build layers: (tag name, dockerfile path, contribution) — professions from
# their own dirs, dood from its claimed `_dood` layer. The base Dockerfile is
# checked too but has no tag.docker (its one build-arg is threaded directly
# by docker_config.ensure_image).
_dood_layer = REGISTRY.specialties["dood"].layer
assert _dood_layer is not None   # the shipped tree claims profession/code/_dood
BUILD_LAYERS = [
    ("code", REGISTRY.professions["code"].path / "Dockerfile", REGISTRY.professions["code"].docker),
    ("webdev", REGISTRY.professions["webdev"].path / "Dockerfile", REGISTRY.professions["webdev"].docker),
    ("dood", _dood_layer.path / "Dockerfile", _dood_layer.docker),
]


# Allowlist for vars referenced in Dockerfiles but NOT staged by the
# launcher's Python code:
#   HOST_UID     — base Dockerfile build-time ARG with a default of 1000.
#                  Currently not parameterized from the launcher; the default
#                  applies.
#   PARENT_IMAGE — threaded explicitly per step by docker_config.ensure_image
#                  (not staged in the container-env accumulator).
#   VERSION /
#   ARCH_SUFFIX  — shell-local variables set inside the [code] Dockerfile's
#                  jira-cli install RUN block (version comes from the
#                  GitHub API; arch from `dpkg --print-architecture`).
#                  Not Docker ARGs — they live entirely within one RUN.
#   GO_VER /
#   KOTLIN_VER   — same pattern for the go / kotlin install RUN blocks.
_ALLOWLIST = {"HOST_UID", "PARENT_IMAGE", "VERSION", "ARCH_SUFFIX", "GO_VER", "KOTLIN_VER"}


def _staged_env_vars():
    """Union of every env-var name the launcher actively stages: the static
    ContainerEnvKey set, the profile-driven toolchain INSTALL_<TOOL> flags
    (one per template.form entry — value-independent, since
    toolkit_install_flags emits one key per manifest entry regardless; the
    profile path is patched to a throwaway location purely for hermeticity,
    so this never touches the real ~/.claude-agents/code_profile.toml), the
    creds-driven CLI INSTALL_<TOOL> flags, and the optional-cred token vars."""
    configurable = [p for p in REGISTRY.professions.values() if p.toolkit_path]
    with patch("launch.container_env.toolkit_profile_path", lambda name: Path("/nonexistent")):
        toolkit_flags = toolkit_install_flags(configurable)
    return (
        {m.value for m in ContainerEnvKey}
        | set(toolkit_flags)
        | set(install_creds_flags(set()))
        | set(paths.OPTIONAL_CREDS_TOKEN_ENV_VARS.values())
    )


def _allowed_vars():
    return _staged_env_vars() | _ALLOWLIST


# Matches ${VAR} and ${VAR:-default} — variable name in group 1.
_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}")
# Matches `ARG VARNAME` in Dockerfile.
_ARG_RE = re.compile(r"^ARG\s+([A-Z_][A-Z0-9_]*)\b", re.MULTILINE)


def _dockerfiles():
    """The base Dockerfile + every build layer's Dockerfile."""
    return [paths.BASE_DOCKERFILE] + [dockerfile for _, dockerfile, _ in BUILD_LAYERS]


def _env_refs_in(path):
    """Set of `${VAR}`-style references in `path`'s text."""
    return {m.group(1) for m in _ENV_REF_RE.finditer(path.read_text())}


def _arg_decls_in(path):
    """Set of `ARG VAR` declarations in a Dockerfile."""
    return {m.group(1) for m in _ARG_RE.finditer(path.read_text())}


def _expand_forward(forward, staged):
    """A layer's arg_forward names expanded against the staged-var set —
    the same glob semantics docker_config.build_arg_flags applies."""
    out = set()
    for pattern in forward:
        if any(ch in pattern for ch in "*?["):
            out |= set(fnmatch.filter(staged, pattern))
        else:
            out.add(pattern)
    return out


# ============================================================
# Dockerfile references
# ============================================================


class TestDockerfileEnvRefs(unittest.TestCase):
    def test_every_dollar_ref_is_known(self):
        # ${VAR} usages inside Dockerfile bodies (e.g. RUN useradd … -u
        # ${HOST_UID}). Each should be a known staged var or allowlisted.
        allowed = _allowed_vars()
        for path in _dockerfiles():
            with self.subTest(file=str(path.relative_to(paths.DOCKERIZED_CLAUDE_ROOT))):
                unknown = _env_refs_in(path) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} references env vars not in our taxonomy: {sorted(unknown)}",
                )

    def test_every_arg_is_known(self):
        # Every `ARG VAR` declaration must be a known staged var, PARENT_IMAGE,
        # or in the allowlist (build-time defaults the launcher doesn't override).
        allowed = _allowed_vars()
        for path in _dockerfiles():
            with self.subTest(file=str(path.relative_to(paths.DOCKERIZED_CLAUDE_ROOT))):
                unknown = _arg_decls_in(path) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} declares ARGs not in our taxonomy: {sorted(unknown)}",
                )


# ============================================================
# tag.docker ⇄ Dockerfile consistency
# ============================================================


class TestArgForwardsMatchDockerfiles(unittest.TestCase):
    """A layer's `[build] arg_forward` and its Dockerfile's ARG declarations
    must agree in both directions — a forward without an ARG silently
    no-ops (docker warns, value unused); a launcher-staged ARG without a
    forward silently keeps its Dockerfile default."""

    def test_every_forward_resolves_to_a_dockerfile_arg(self):
        staged = _staged_env_vars()
        for name, dockerfile, contribution in BUILD_LAYERS:
            forward = contribution.build_arg_forward if contribution else ()
            declared = _arg_decls_in(dockerfile)
            with self.subTest(layer=name):
                expanded = _expand_forward(forward, staged)
                self.assertTrue(expanded <= declared,
                                f"{name}'s tag.docker forwards {sorted(expanded - declared)} "
                                f"but its Dockerfile declares no matching ARG")

    def test_every_forward_name_is_staged_or_glob(self):
        staged = _staged_env_vars()
        for name, _, contribution in BUILD_LAYERS:
            forward = contribution.build_arg_forward if contribution else ()
            for pattern in forward:
                with self.subTest(layer=name, pattern=pattern):
                    if any(ch in pattern for ch in "*?["):
                        self.assertTrue(fnmatch.filter(staged, pattern),
                                        f"glob {pattern!r} matches no staged var")
                    else:
                        self.assertIn(pattern, staged,
                                      f"{pattern!r} is never staged by the launcher")

    def test_every_staged_dockerfile_arg_is_forwarded(self):
        # Reverse direction: an ARG the launcher actively stages MUST be in
        # the layer's arg_forward, otherwise the staged value silently drops
        # at build time. Allowlisted ARGs (HOST_UID, PARENT_IMAGE) don't
        # apply — those aren't staged (PARENT_IMAGE threads explicitly).
        staged = _staged_env_vars()
        for name, dockerfile, contribution in BUILD_LAYERS:
            forward = contribution.build_arg_forward if contribution else ()
            with self.subTest(layer=name):
                staged_args = _arg_decls_in(dockerfile) & staged
                missing = staged_args - _expand_forward(forward, staged)
                self.assertFalse(
                    missing,
                    f"{dockerfile.name} declares ARGs {sorted(missing)} that the "
                    f"launcher stages, but {name}'s tag.docker doesn't forward "
                    f"them — the staged value would silently drop. Add them to "
                    f"[build] arg_forward.",
                )

    def test_base_dockerfile_consumes_the_refresh_arg(self):
        # The base build's one launcher-staged arg is threaded directly by
        # ensure_image (no tag.docker for base) — guard the pairing.
        self.assertIn("SOFTWARE_STACK_REFRESH", _arg_decls_in(paths.BASE_DOCKERFILE))


# ============================================================
# tag.docker env forwards
# ============================================================


class TestEnvForwardsAreStaged(unittest.TestCase):
    def test_every_env_forward_name_is_a_known_key(self):
        # A run-side env_forward name must be something the launcher can
        # stage — a ContainerEnvKey (or dynamic token var). A typo here
        # would gate on a value that never arrives.
        known = _staged_env_vars()
        for kind in (REGISTRY.professions, REGISTRY.specialties, REGISTRY.policies):
            for tag in kind.values():
                layer = getattr(tag, "layer", None)
                contributions = [c for c in (tag.docker, layer.docker if layer else None) if c]
                for contribution in contributions:
                    for env_name in contribution.env_forward:
                        with self.subTest(tag=tag.name, env=env_name):
                            self.assertIn(env_name, known)


if __name__ == "__main__":
    unittest.main()

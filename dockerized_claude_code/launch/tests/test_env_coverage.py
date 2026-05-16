"""Verify every env var referenced in compose .yml / Dockerfile files is
defined in our Python-side env-var taxonomy.

Catches orphan references that would otherwise silently substitute to the
compose default (e.g. typo in `${TARGT_IMAGE}` becomes empty string at run
time without an error). Three checks:

1. `${VAR}` substitution in compose .yml — must be a ComposeEnvKey, an
   INSTALL_<TOOL> from install_creds_flags, a token env-var from
   OPTIONAL_CREDS_TOKEN_ENV_VARS, or in the allowlist (TERM, HOST_UID,
   etc.) of build-time-default ARGs / shell-inherited passthroughs.
2. `environment: [- VAR]` passthroughs in compose .yml — same allowed set.
3. `ARG VAR` in Dockerfile files — each ARG that's passed from a compose
   `args:` block (i.e. is in the allowed set) is recognised; the rest fall
   under the build-time-default allowlist."""

import re
import unittest

from launch import paths
from launch.compose_env import ComposeEnvKey
from launch.structs import InstanceModifiers


# Allowlist for vars referenced in compose/Dockerfile files but NOT staged
# by the launcher's Python code:
#   TERM         — shell-inherited from the launcher's tty, declared as a
#                  passthrough in compose.yml's `environment:` list.
#   HOST_UID     — base Dockerfile build-time ARG with a default of 1000.
#                  Currently not parameterized from the launcher; the default
#                  applies.
#   VERSION /
#   ARCH_SUFFIX  — shell-local variables set inside Dockerfile.prog's
#                  jira-cli install RUN block (version comes from the
#                  GitHub API; arch from `dpkg --print-architecture`).
#                  Not Docker ARGs — they live entirely within one RUN.
_ALLOWLIST = {"TERM", "HOST_UID", "VERSION", "ARCH_SUFFIX"}


def _defined_env_vars():
    """Union of every env-var name the launcher actively stages: the static
    ComposeEnvKey set, the per-service INSTALL_<TOOL> build flags, and the
    optional-cred token vars."""
    return (
        {m.value for m in ComposeEnvKey}
        | {f"INSTALL_{svc.upper()}" for svc in paths.OPTIONAL_CREDS_MOUNTS}
        | set(paths.OPTIONAL_CREDS_TOKEN_ENV_VARS.values())
    )


def _allowed_vars():
    return _defined_env_vars() | _ALLOWLIST


# Matches ${VAR} and ${VAR:-default} — variable name in group 1.
_ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}")
# Matches `ARG VARNAME` in Dockerfile.
_ARG_RE = re.compile(r"^ARG\s+([A-Z_][A-Z0-9_]*)\b", re.MULTILINE)


def _compose_files():
    """All compose .yml files the launcher uses: the base + every modifier
    layer (non-BASE)."""
    return [paths.COMPOSE_FILE_PATH] + [
        paths.compose_layer_path(m.slug)
        for m in InstanceModifiers
        if m is not InstanceModifiers.BASE
    ]


def _dockerfiles():
    """The base Dockerfile + every modifier's Dockerfile."""
    return [paths.DOCKER_DIR / "Dockerfile"] + [
        paths.DOCKER_DIR / f"Dockerfile.{m.slug}"
        for m in InstanceModifiers
        if m is not InstanceModifiers.BASE
    ]


def _env_refs_in(path):
    """Set of `${VAR}`-style references in `path`'s text."""
    return {m.group(1) for m in _ENV_REF_RE.finditer(path.read_text())}


def _arg_decls_in(path):
    """Set of `ARG VAR` declarations in a Dockerfile."""
    return {m.group(1) for m in _ARG_RE.finditer(path.read_text())}


def _env_passthroughs_in(text):
    """Find `- VAR` items under `environment:` blocks in a compose .yml.
    State machine because regex alone can't distinguish env passthroughs
    from cap_add items (both render as `- WORD` lines)."""
    refs = set()
    in_env = False
    env_indent = -1
    for line in text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            continue
        # Section opener
        if stripped.rstrip().endswith("environment:"):
            in_env = True
            env_indent = indent
            continue
        if not in_env:
            continue
        # List item under environment:
        if stripped.startswith("-") and indent > env_indent:
            name = stripped[1:].lstrip().split("=")[0].rstrip()
            if re.match(r"^[A-Z_][A-Z0-9_]*$", name):
                refs.add(name)
            continue
        # Anything else with indent ≤ env_indent terminates the block
        if indent <= env_indent:
            in_env = False
    return refs


# ============================================================
# compose .yml
# ============================================================


class TestComposeFileEnvRefs(unittest.TestCase):
    def test_every_ref_is_known(self):
        allowed = _allowed_vars()
        for path in _compose_files():
            with self.subTest(file=path.name):
                unknown = _env_refs_in(path) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} references env vars not in our Python taxonomy: {sorted(unknown)}",
                )

    def test_every_passthrough_is_known(self):
        allowed = _allowed_vars()
        for path in _compose_files():
            with self.subTest(file=path.name):
                unknown = _env_passthroughs_in(path.read_text()) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} declares passthrough env vars not in our taxonomy: {sorted(unknown)}",
                )


# ============================================================
# Dockerfile.*
# ============================================================


class TestDockerfileEnvRefs(unittest.TestCase):
    def test_every_dollar_ref_is_known(self):
        # ${VAR} usages inside Dockerfile bodies (e.g. RUN useradd … -u
        # ${HOST_UID}). Each should be a known ARG or compose env var.
        allowed = _allowed_vars()
        for path in _dockerfiles():
            with self.subTest(file=path.name):
                unknown = _env_refs_in(path) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} references env vars not in our taxonomy: {sorted(unknown)}",
                )

    def test_every_arg_is_known(self):
        # Every `ARG VAR` declaration must be a known env var (Python-staged)
        # or in the allowlist (build-time defaults the launcher doesn't override).
        allowed = _allowed_vars()
        for path in _dockerfiles():
            with self.subTest(file=path.name):
                unknown = _arg_decls_in(path) - allowed
                self.assertFalse(
                    unknown,
                    f"{path.name} declares ARGs not in our taxonomy: {sorted(unknown)}",
                )


# ============================================================
# Consistency between compose args block and Dockerfile ARGs
# ============================================================


class TestComposeArgsMatchDockerfile(unittest.TestCase):
    """When a compose layer declares `args: { FOO: ${FOO:-bar} }`, the matching
    Dockerfile.<step> must declare `ARG FOO` — otherwise the value silently
    drops on the floor at build time. This is a different invariant from the
    two above; here we check pairings layer-by-layer."""

    def _compose_args_keys(self, compose_text):
        """Parse `args:` block keys via line scanner. Each key is `NAME: ${...}`
        style under a `args:` heading."""
        keys = set()
        in_args = False
        args_indent = -1
        for line in compose_text.splitlines():
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.rstrip().endswith("args:"):
                in_args = True
                args_indent = indent
                continue
            if not in_args:
                continue
            if indent <= args_indent:
                in_args = False
                continue
            # KEY: VALUE form
            m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*:", stripped)
            if m:
                keys.add(m.group(1))
        return keys

    def test_compose_args_keys_have_matching_dockerfile_arg(self):
        for m in InstanceModifiers:
            if m is InstanceModifiers.BASE:
                continue
            with self.subTest(modifier=m.value):
                compose_path = paths.compose_layer_path(m.slug)
                dockerfile_path = paths.DOCKER_DIR / f"Dockerfile.{m.slug}"
                compose_args = self._compose_args_keys(compose_path.read_text())
                dockerfile_args = _arg_decls_in(dockerfile_path)
                missing = compose_args - dockerfile_args
                self.assertFalse(
                    missing,
                    f"{compose_path.name} passes args {sorted(missing)} but "
                    f"{dockerfile_path.name} doesn't declare them as ARG",
                )


if __name__ == "__main__":
    unittest.main()

"""Tag system — base record + shared parsing, for the tags rewrite.

A **tag** is one member of one of four *kinds* (engine / profession /
specialty / policy). The kinds are a closed set — one subclass of `Tag`
each, in the sibling modules — while the *members* of each kind are an OPEN
set discovered from the `agents/` file tree at startup (see `registry.py`).
Dropping a valid tag folder into the tree adds a member with no code change.

This module owns:
  - `Tag`            — the frozen per-member record (name, description, the
                       relations derived at scan time, an optional docker
                       contribution). Kind subclasses add kind-specific fields.
  - `DockerContribution` — the parsed `tag.docker` (the "how it touches the
                       container" half of the what/how split; `tag.info` is
                       the "what").
  - `TagError`       — the single fail-loud exception every scanner / validator
                       raises, always naming the offending path.
  - shared parsers   — `read_info` (tag.info → dict), `parse_docker`
                       (tag.docker → DockerContribution), `parse_wants`.

Design notes:
  - Frozen + all-hashable fields (tuples, not dicts/lists) so a Tag can go in
    a set if a consumer ever wants it; `wants` is stored as a tuple of pairs
    and re-exposed as a dict via `wants_map`.
  - `description` is a plain TOML field (queryable directly — its first line
    is the member's nutshell, the full text feeds the form's body panel), NOT
    a comment block. `tag.info` is therefore pure TOML.
  - Requirements are NEVER authored — they are derived from tree position by
    the per-kind scanners. There is no `requires` key in any manifest.

Leaf module: imports stdlib + `tomllib` only. The kind modules import from
here; `registry.py` imports the kinds.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

INFO_FILE = "tag.info"       # per-tag manifest (TOML) — presence marks a dir as an offered tag
DOCKER_FILE = "tag.docker"   # per-tag container contribution (TOML) — optional
HIDDEN_PREFIX = "_"          # a `_`-prefixed dir is a hidden asset dir, not an offered tag


class TagError(Exception):
    """Raised on any tag-tree defect — malformed manifest, name collision,
    dangling reference, orphan asset dir, unparseable fragment. Always
    constructed with a message that names the offending path or tag so a
    launch aborts with a fixable one-liner rather than a traceback."""


# ============================================================
# Docker contribution — the parsed tag.docker ("how")
# ============================================================

@dataclass(frozen=True)
class DockerContribution:
    """A tag's static, declarative container contribution, parsed from
    `tag.docker`. Dynamic values (resolved firewall addresses, detected
    DOCKER_GID, per-instance workspace mounts) are NOT here — those stay in
    the launch-time handlers, which forward the names listed in
    `build_arg_forward` / `env_forward` after staging the values.

    Fields:
      build_arg_forward — build-arg names this layer's Dockerfile consumes
                          (`docker build --build-arg NAME=<staged>`).
      cap_add           — Linux capabilities (`docker run --cap-add`).
      entrypoint        — container entrypoint; a bare name resolves to an
                          absolute in-container path the launcher knows, a
                          path is used as-is. Stored verbatim here.
      mounts            — (source, target) pairs; source already resolved to
                          an absolute host Path (relative sources joined to
                          the tag dir at parse time), target kept as the raw
                          "path[:ro]" string.
      env_forward       — env-var names forwarded into the container
                          (`docker run -e NAME=<staged>`)."""
    build_arg_forward: tuple[str, ...] = ()
    cap_add: tuple[str, ...] = ()
    entrypoint: str | None = None
    mounts: tuple[tuple[Path, str], ...] = ()
    env_forward: tuple[str, ...] = ()


# ============================================================
# Tag — the per-member record
# ============================================================

@dataclass(frozen=True)
class Tag:
    """One discovered tag. Class-level attrs (set on each kind subclass)
    describe the KIND; instance attrs describe the MEMBER.

    Kind-level (ClassVar, overridden per subclass):
      parentheses — (opener, closer) wrapping the shortname in the label
                    (`[code]`, `{auto}`, `<+query>`, `(researcher)`).
      root        — subtree under agents/ this kind is discovered from.
      nutshell    — one-line gloss of the KIND (form section header, legend).

    Member-level:
      name        — folder name; the canonical string stored in instances.toml,
                    used as an image-tag component and a form key.
      shortname   — what shows inside the parentheses (defaults to name).
      description — full prose; first line is the member nutshell.
      requires    — names of tags that must also be active — DERIVED from tree
                    position by the scanner, never authored.
      wants       — 1-directional (name, message) pairs: this tag proclaims an
                    almost-dependency, and the message surfaces in the form's
                    warning zone while the wanted tag is unchecked.
      docker      — parsed tag.docker, or None when the tag makes no container
                    contribution."""

    parentheses: ClassVar[tuple[str, str]] = ("", "")
    root: ClassVar[str] = ""
    nutshell: ClassVar[str] = ""

    name: str
    path: Path
    shortname: str = ""
    description: str = ""
    requires: frozenset[str] = frozenset()
    wants: tuple[tuple[str, str], ...] = ()
    docker: DockerContribution | None = None

    @property
    def label(self) -> str:
        """Display label — shortname wrapped in the kind's parentheses.
        `Profession(name='code')` → `[code]`; a policy with shortname
        `+query` → `<+query>`."""
        opener, closer = self.parentheses
        return f"{opener}{self.shortname or self.name}{closer}"

    @property
    def wants_map(self) -> dict[str, str]:
        """`wants` as a `{wanted-tag: message}` dict (the tuple-of-pairs
        storage keeps the frozen record hashable)."""
        return dict(self.wants)


# ============================================================
# Shared parsers
# ============================================================

def read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file, re-raising any parse error as a `TagError` that names
    the file (tomllib's own message doesn't include the path)."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise TagError(f"{path}: cannot read TOML ({e})") from e


def read_info(tag_dir: Path) -> dict[str, Any]:
    """Read a tag dir's `tag.info` into a dict. Raises `TagError` if absent —
    presence of `tag.info` is what makes a dir an offered tag, so a caller
    reaching here without one is a scanner bug, surfaced loudly."""
    info_path = tag_dir / INFO_FILE
    if not info_path.is_file():
        raise TagError(f"{tag_dir}: missing {INFO_FILE}")
    return read_toml(info_path)


def parse_wants(info: dict[str, Any], tag_dir: Path) -> tuple[tuple[str, str], ...]:
    """Extract the optional `[wants]` table ({wanted-tag: message}) as a sorted
    tuple of pairs. Each value must be a string message; a non-string (or a
    non-table `wants`) is a `TagError`. Reference validity (does the wanted
    tag exist?) is checked later, cross-kind, in the registry validator."""
    raw = info.get("wants", {})
    if not isinstance(raw, dict):
        raise TagError(f"{tag_dir}/{INFO_FILE}: [wants] must be a table, got {type(raw).__name__}")
    pairs = []
    for wanted, message in raw.items():
        if not isinstance(message, str):
            raise TagError(f"{tag_dir}/{INFO_FILE}: wants.{wanted} must be a string message")
        pairs.append((wanted, message))
    return tuple(sorted(pairs))


def parse_docker(tag_dir: Path) -> DockerContribution | None:
    """Parse a tag dir's optional `tag.docker` into a `DockerContribution`, or
    None when the file is absent (most tags make no container contribution).

    `[build].arg_forward` → build_arg_forward. `[run]` supplies cap_add,
    entrypoint, mounts, env_forward. Mount entries are `"source -> target"`;
    a relative source resolves against `tag_dir`, an absolute source (a host
    path like /var/run/docker.sock) is kept as-is. A referenced mount source
    that doesn't exist on disk is a `TagError` (fail loud at scan, not at
    `docker run`)."""
    docker_path = tag_dir / DOCKER_FILE
    if not docker_path.is_file():
        return None
    data = read_toml(docker_path)
    build = data.get("build", {})
    run = data.get("run", {})

    mounts = []
    for entry in run.get("mounts", []):
        if "->" not in entry:
            raise TagError(f"{docker_path}: mount '{entry}' is not 'source -> target'")
        raw_src, target = (part.strip() for part in entry.split("->", 1))
        src = Path(raw_src) if Path(raw_src).is_absolute() else (tag_dir / raw_src)
        # Only relative (tag-owned) sources are validated for existence; an
        # absolute host path (docker.sock) may legitimately not exist at scan time.
        if not Path(raw_src).is_absolute() and not src.exists():
            raise TagError(f"{docker_path}: mount source '{raw_src}' not found in {tag_dir}")
        mounts.append((src, target))

    entrypoint = run.get("entrypoint")
    if entrypoint is not None and not (tag_dir / entrypoint).exists() and "/" not in entrypoint:
        # A bare entrypoint name must name a script shipped in the tag dir.
        raise TagError(f"{docker_path}: entrypoint '{entrypoint}' not found in {tag_dir}")

    return DockerContribution(
        build_arg_forward=tuple(build.get("arg_forward", [])),
        cap_add=tuple(run.get("cap_add", [])),
        entrypoint=entrypoint,
        mounts=tuple(mounts),
        env_forward=tuple(run.get("env_forward", [])),
    )


def is_hidden_asset_dir(child: Path) -> bool:
    """Classify a subdirectory of a kind subtree under the STRICT tree rule:
      - name starts with `_`  → hidden asset dir → True  (skip, or claim as a layer)
      - contains `tag.info`   → offered tag       → False
      - neither               → structural error  → raises TagError

    i.e. every non-`_` directory MUST carry a `tag.info`. A bare dir is almost
    always a forgotten or misnamed manifest — treating it permissively (as a
    silent grouping shelf) would drop the tag AND sever any requirement edge
    routed through it (a nested `[web]` losing its `requires: code`), with no
    error. `_`-dir meaning varies by location: in the profession tree a `_dir`
    is a specialty-claimed image layer (see `profession.discover_layers`);
    anywhere else it's simply an ignored asset dir."""
    if child.name.startswith(HIDDEN_PREFIX):
        return True
    if (child / INFO_FILE).is_file():
        return False
    raise TagError(
        f"{child}: a tag directory needs {INFO_FILE}; "
        f"prefix the name with '{HIDDEN_PREFIX}' if it's a hidden asset dir, not a tag"
    )


def walk_tag_tree(root: Path) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """Yield `(tag_dir, ancestor_names)` for every tag dir under `root`,
    depth-first with parents before children and siblings in name order.
    `ancestor_names` is the tuple of enclosing tag-dir names from the top down
    (excluding `tag_dir`) — professions read it as `requires`, engines as the
    conf-inheritance chain.

    Strict (see `is_hidden_asset_dir`): `_`-dirs are skipped, tag dirs are
    yielded + descended, and a bare non-`_` dir without `tag.info` raises.
    Missing `root` yields nothing (a kind with no members is valid)."""
    if not root.is_dir():
        return

    def rec(d: Path, ancestors: tuple[str, ...]) -> Iterator[tuple[Path, tuple[str, ...]]]:
        for child in sorted(d.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or is_hidden_asset_dir(child):
                continue
            yield child, ancestors
            yield from rec(child, ancestors + (child.name,))

    yield from rec(root, ())


def common_fields(tag_dir: Path) -> dict[str, Any]:
    """The `tag.info`/`tag.docker`-derived fields every kind shares, as a
    kwargs dict the per-kind scanners splat into their constructor alongside
    the kind-specific fields (and the tree-derived `requires`). Keeps the four
    scanners from each re-reading the manifest."""
    info = read_info(tag_dir)
    description = str(info.get("description", "")).strip()
    return {
        "name": tag_dir.name,
        "path": tag_dir,
        "shortname": str(info.get("shortname", "") or tag_dir.name),
        "description": description,
        "wants": parse_wants(info, tag_dir),
        "docker": parse_docker(tag_dir),
        "_info": info,   # handed back so the kind can read its own keys without a re-read
    }

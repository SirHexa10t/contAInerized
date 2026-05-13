"""Agent composition layer: computes the docker build chain (base → modifiers in
InstanceModifiers declaration order) and runs each active modifier's handler
contributions (volume mounts + compose env staging) in a single pass via
compose_chain. Also owns the mode-conditional memory-template list
(mode_memory_templates) that agents_crud.sync_memory_templates consumes.

The modifier taxonomy itself (InstanceModifiers + tags() / modes() views +
descriptions) lives in structs.py — both this module and agents_crud consume it
from there. Sort keys for the picker (agent/mode/tag sort) live in agents_crud —
they're picker-side concerns and don't belong in the composition layer.

Imports path constants from paths, low-level filesystem + firewall-whitelist
helpers from file_access (used by prune_caches / resolved_whitelist_domains),
env-/mount-staging helpers from docker_config, and the InstanceModifiers
taxonomy from structs; agents_crud, menu_picker, and run.py import from here.
"""

import subprocess
import time

from .file_access import delete_file, ensure_dir, user_firewall_whitelist_lines
from .docker_config import DOCKER_GID, WHITELIST_DOMAINS, add_docker_mount, stage_compose_env
from .paths import ADDENDUM_SUFFIX, CACHE_MOUNTS, CACHE_ROOT
from .structs import InstanceModifiers

# === Priority orderings ===
# The InstanceModifiers enum (in structs.py) is the canonical ordered taxonomy
# — declaration order encodes chain composition order, and tags() / modes()
# provide the two subset views. Adding a new tag/mode means one line in that
# enum AND wiring its handler in *_HANDLERS below.

# === [prog]-tag cache pruning thresholds — applied to CACHE_MOUNTS by prune_caches below ===
CACHE_PRUNE_THRESHOLD_GB = 5   # per-cache size at which prune kicks in
CACHE_PRUNE_MIN_AGE_DAYS = 7   # files younger than this are kept even when over threshold

# === Always-allowed domains in {auto} mode (used by resolved_whitelist_domains) ===
# The list lives here (not in init-firewall.sh) so a single Python step owns the
# full resolved domain set — built-ins + user whitelist entries, deduped. Every
# form you want allowed must be listed explicitly (e.g. both `foo.com` and
# `www.foo.com` if both are needed). The one convenience: a `www.X` entry also
# implicitly allows `X`, since the user typing the `www.` form clearly meant
# the bare apex too. Bash inside the container just iterates WHITELIST_DOMAINS.
BUILTIN_FIREWALL_DOMAINS = [
    # === Core launcher dependencies ===
    # Anthropic
    "api.anthropic.com",
    "console.anthropic.com",
    "www.claude.ai",
    # GitHub (git, releases, raw, codeload, container registry)
    "www.github.com",
    "api.github.com",
    "www.raw.githubusercontent.com",
    "www.objects.githubusercontent.com",
    "codeload.github.com",
    "www.ghcr.io",
    # npm
    "registry.npmjs.org",
    # PyPI
    "www.pypi.org",
    "files.pythonhosted.org",
    # crates.io (Rust)
    "www.crates.io",
    "static.crates.io",
    "index.crates.io",

    # === Developer documentation & references ===
    # Q&A and community
    "www.stackoverflow.com",
    "www.stackexchange.com",     # covers DBA / Security / Code Review etc.; Server Fault and Super User live at their own apexes
    "www.gitlab.com",
    # Language docs — Python (PyPI registry above)
    "docs.python.org",
    "peps.python.org",
    # Language docs — Rust (crates.io registry above)
    "doc.rust-lang.org",
    "www.rust-lang.org",
    "www.docs.rs",
    # Language docs — Node.js / JavaScript (npm registry above)
    "www.nodejs.org",
    "developer.mozilla.org",  # MDN — also covers HTML / CSS / Web APIs
    "www.npmjs.com",
    "tc39.es",     # ECMAScript spec
    # Language docs — TypeScript
    "www.typescriptlang.org",
    # Language docs — Go
    "go.dev",
    "pkg.go.dev",
    # Language docs — Java
    "docs.oracle.com",
    "openjdk.org",
    "www.mvnrepository.com",
    "search.maven.org",
    # Language docs — C# / .NET (also covers Azure, VS Code, TypeScript, etc.)
    "www.learn.microsoft.com",
    # Language docs — C / C++
    "www.en.cppreference.com",
    "www.isocpp.org",
    # Language docs — Ruby
    "www.ruby-lang.org",
    "www.ruby-doc.org",
    "www.rubygems.org",
    # Language docs — PHP
    "www.php.net",
    "www.packagist.org",
    # Language docs — Swift / Apple
    "www.swift.org",
    "www.developer.apple.com",
    # Language docs — Kotlin
    "www.kotlinlang.org",
    # Language docs — Other
    "www.haskell.org",
    "www.dart.dev",
    "www.elixir-lang.org",
    "www.hexdocs.pm",
    "www.scala-lang.org",
    "www.clojure.org",
    "www.julialang.org",
    "www.ocaml.org",
    "www.erlang.org",
    "www.r-project.org",
    "www.cran.r-project.org",
    "www.perl.org",
    "www.perldoc.perl.org",
    "www.lua.org",
    # Cloud / infra — AWS
    "docs.aws.amazon.com",
    "www.aws.amazon.com",
    "www.repost.aws",            # AWS re:Post Q&A
    # Cloud / infra — GCP
    "www.cloud.google.com",
    "firebase.google.com",
    # Cloud / infra — Azure (learn.microsoft.com above)
    "www.azure.microsoft.com",
    # Cloud / infra — Docker / Kubernetes / Helm
    "docs.docker.com",
    "www.kubernetes.io",
    "www.helm.sh",
    # Cloud / infra — HashiCorp (Terraform, Vault, Consul, Nomad)
    "developer.hashicorp.com",
    # Web standards
    "www.whatwg.org",            # HTML / DOM / Fetch specs
    "www.w3.org",                # W3C specs
    "www.caniuse.com",           # browser compat tables
    "www.web.dev",               # Google web best-practices
    # Frontend frameworks
    "www.react.dev",
    "www.vuejs.org",
    "www.angular.dev",
    "www.svelte.dev",
    "www.nextjs.org",
    "www.nuxt.com",
    "www.remix.run",
    "www.astro.build",
    # Backend frameworks — Python
    "docs.djangoproject.com",
    "flask.palletsprojects.com",
    "fastapi.tiangolo.com",
    # Backend frameworks — Node
    "www.expressjs.com",
    "www.nestjs.com",
    # Backend frameworks — Java
    "www.spring.io",
    "docs.spring.io",
    # Backend frameworks — Ruby
    "www.rubyonrails.org",
    "guides.rubyonrails.org",
    # Backend frameworks — PHP
    "www.laravel.com",
    "www.symfony.com",
    # ML / data
    "www.pytorch.org",
    "www.tensorflow.org",
    "www.scikit-learn.org",
    "www.numpy.org",
    "pandas.pydata.org",
    "www.jupyter.org",
    "www.huggingface.co",
    "www.arxiv.org",
    "paperswithcode.com",
    # AI / LLM APIs (Anthropic API endpoints above)
    "docs.anthropic.com",
    "platform.openai.com",
    # Databases
    "www.postgresql.org",
    "dev.mysql.com",
    "www.mariadb.com",
    "www.sqlite.org",
    "www.redis.io",
    "www.mongodb.com",
    "www.elastic.co",
    # Linux / systems
    "www.man7.org",              # Linux man pages
    "www.kernel.org",
    "wiki.archlinux.org",    # general Linux setup info, even off-Arch
    "access.redhat.com",
    "www.lwn.net",               # kernel and systems-internals reporting
    # Standards / RFCs
    "datatracker.ietf.org",
    "www.rfc-editor.org",
    "semver.org",
    "www.json.org",
    # Build & tooling
    "www.webpack.js.org",
    "www.vite.dev",
    "www.rollupjs.org",
    "www.esbuild.github.io",
    "cmake.org",
    "www.ninja-build.org",
    "www.git-scm.com",
    # Reliable tutorial / reference sites
    "www.realpython.com",        # Python
    "www.baeldung.com",          # Java / Spring
    "www.digitalocean.com",      # community tutorials
    "www.css-tricks.com",        # web / CSS
    "www.smashingmagazine.com",  # web / CSS
    "www.learnxinyminutes.com",  # quick-reference cheat sheets per language
    "www.martinfowler.com",      # architecture and refactoring
    "www.fly.io",                # systems / networking writing on fly.io/blog
]


# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        ensure_dir(host)


def prune_caches():
    """For caches above CACHE_PRUNE_THRESHOLD_GB, remove files older than CACHE_PRUNE_MIN_AGE_DAYS.
    Skipped when any agent container is running (to avoid yanking caches mid-build)."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=claude-code_", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or result.stdout.strip():
        return
    time_cutoff = time.time() - CACHE_PRUNE_MIN_AGE_DAYS * 86400  # days → seconds (match epoch-second time.time())
    size_cutoff = CACHE_PRUNE_THRESHOLD_GB * 1024**3              # GB   → bytes   (match st_size units)
    for host in CACHE_MOUNTS:
        if not host.exists():
            continue
        files = [(f, f.stat()) for f in host.rglob("*") if f.is_file()]
        total = sum(s.st_size for _, s in files)
        if total <= size_cutoff:
            continue
        freed = 0
        for f, s in files:
            if s.st_mtime < time_cutoff:
                delete_file(f)
                freed += s.st_size
        if freed:
            print(f"  Pruned {host.relative_to(CACHE_ROOT)}: freed {freed / 1024**3:.1f} GB (was {total / 1024**3:.1f} GB)")


def _apply_prog():
    """[prog] tag handler. Three side effects, no return value:
      • prepare_caches() mkdirs the host cache dirs (so the bind-mount targets
        exist before the container starts; otherwise Docker creates them as
        root and we can't clean them up later).
      • prune_caches() opportunistically trims oversized caches.
      • add_docker_mount stages each cache as a bind-mount for the upcoming
        `docker compose run` (read-write — toolchains write into them).

    The compose/Dockerfile pair (compose.prog.yml + docker/Dockerfile.prog) is
    NOT selected here — chain order in compose_chain handles that."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


# === Mode dispatch — like tags, but per-instance (set at create/modify time, stored in agent_modes_map.json) ===

def _detect_docker_gid():
    """Return the host's docker group GID as a string, or None if no docker group exists.
    Used as the DOCKER_GID build-arg for Dockerfile.dood (so claude can read/write the
    bind-mounted /var/run/docker.sock)."""
    try:
        result = subprocess.run(
            ["getent", "group", "docker"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None  # no getent (e.g., not Linux)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split(":")[2]
    return None


def _apply_dood():
    """{DooD} mode: bind-mount the host's /var/run/docker.sock so the agent can drive
    the host's Docker daemon (run sub-containers, build images, etc.). Detects the
    host's docker-group GID via getent and stages it as the DOCKER_GID compose var
    so the compose layer picks it up as a build-arg. Without a docker group on
    the host, the agent couldn't access the bind-mounted socket — so we fail loudly
    here rather than build an image that won't work."""
    gid = _detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            f"{InstanceModifiers.MODE_DOOD.value} mode requires a `docker` group on the host. On Linux: "
            f"`sudo usermod -aG docker $USER` (then log out + back in). "
            f"If you don't actually need {InstanceModifiers.MODE_DOOD.value}, modify the instance and decline the prompt."
        )
    stage_compose_env(DOCKER_GID, gid)


# === Firewall whitelist (used by {auto} mode) ===
# resolved_whitelist_domains below merges BUILTIN_FIREWALL_DOMAINS with the
# user's whitelist file, treating both alike: every entry is taken at face
# value, no apex→www speculation. The only convenience is that a `www.X`
# entry also implicitly allows `X` (the user typed `www.` explicitly, so we
# trust both forms matter). A leading `*.` is silently stripped — lenient
# rather than crashing on accidental wildcards.

def resolved_whitelist_domains():
    """Full domain list for the {auto} firewall: BUILTIN_FIREWALL_DOMAINS ∪ user
    whitelist, deduped and sorted. Every form needs to be listed explicitly
    (no apex→www speculation); a single convenience adds the non-www counterpart
    for any `www.X` entry. A leading `*.` on entries is silently stripped."""
    expanded = set()
    for d in set(BUILTIN_FIREWALL_DOMAINS) | set(user_firewall_whitelist_lines()):
        d = d.removeprefix("*.")   # tolerate accidental wildcards: `*.foo.com` → `foo.com`
        expanded.add(d)
        if d.startswith("www."):
            expanded.add(d.removeprefix("www."))
    return sorted(expanded)


def _apply_auto():
    """{auto} mode: lets the agent run unattended (--dangerously-skip-permissions
    is added in compose.auto.yml's entrypoint) behind an iptables outbound whitelist.
    The Dockerfile.auto image carries iptables + sudo + a tightly-scoped sudoers
    entry; the firewall script + entrypoint wrapper get bind-mounted via compose.auto.yml.
    Side effect: stages the resolved domain list (built-ins + user entries +
    www↔apex counterparts where applicable) as the WHITELIST_DOMAINS compose var
    so init-firewall.sh inside the container can read it — no whitelist file
    mount needed."""
    stage_compose_env(WHITELIST_DOMAINS, " ".join(resolved_whitelist_domains()))


# === Mode-conditional memory addendums ===
# Each mode optionally has a `memory/<mode_lower>-addendum.md` template that
# agents_crud.sync_memory_templates splices into per-instance MEMORY.md when
# the mode is active (and removes when it's not). Missing template files are
# no-ops in sync_memory_templates, so a mode without an addendum costs nothing.

def mode_memory_templates(modes):
    """Build `(filename, active)` pairs for each `<mode>-addendum.md` template
    in MEMORY_DIR, one entry per InstanceModifiers.modes() member — `active` is
    True iff that mode's `.value` is in `modes` (which holds canonical strings
    as stored in agent_modes_map.json). agents_crud.sync_memory_templates
    processes the returned list after the always-active seek_summary entry."""
    return [(f"{m.filename_form}{ADDENDUM_SUFFIX}", m.value in modes)
            for m in InstanceModifiers.modes()]


# === Chain composition: the build/run image is layered base → modifiers in InstanceModifiers declaration order. ===

def compose_chain(tags, modes):
    """Compose the docker build chain and run each active modifier's handler in
    a single pass. Always starts with 'base'; appends each active modifier's
    canonical string in InstanceModifiers declaration order and invokes its
    handler inline (which stages compose env vars / bind-mounts via
    stage_compose_env / add_docker_mount).

    Returns the chain list of strings — drives image naming
    (claude-agents:<chain[1:] joined by dot>, or claude-agents:base for the
    base case) and the compose -f stack via chain_image_tag /
    chain_compose_files in docker_config.

    Unknown tags/modes raise ValueError so a typo surfaces loudly. `tags` /
    `modes` accept any iterable of canonical-string forms; coerced to sets
    internally for O(1) membership checks and natural deduplication.

    Adding a new modifier means: a new entry in InstanceModifiers (in the
    desired chain position), the matching `_apply_*` function above, and one
    new conditional block here. No dispatch table to keep in sync."""
    tags, modes = set(tags), set(modes)
    if unknown := tags - set(InstanceModifiers.tag_values()):
        raise ValueError(f"Unknown tag(s): {sorted(unknown)}. Known tags: {list(InstanceModifiers.tag_values())}")
    if unknown := modes - set(InstanceModifiers.mode_values()):
        raise ValueError(f"Unknown mode(s): {sorted(unknown)}. Known modes: {list(InstanceModifiers.mode_values())}")
    chain = ["base"]
    if InstanceModifiers.TAG_PROG.value in tags:
        _apply_prog()
        chain.append(InstanceModifiers.TAG_PROG.value)
    if InstanceModifiers.MODE_AUTO.value in modes:
        _apply_auto()
        chain.append(InstanceModifiers.MODE_AUTO.value)
    if InstanceModifiers.MODE_DOOD.value in modes:
        _apply_dood()
        chain.append(InstanceModifiers.MODE_DOOD.value)
    return chain

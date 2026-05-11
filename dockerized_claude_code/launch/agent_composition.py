"""Agent composition layer: parses the agent filename grammar
(`<name>([tag]|(parent))*`), locates the matching `.md`, picks the right `.conf`,
sorts agents by model family/version, and computes the docker build chain
(base → tags → modes) plus the per-handler runtime contributions (volume mounts +
side effects) via apply_composition.

Imports path constants from paths, generic helpers from utils, env-staging
helpers from docker_config, and user-side data from user_additions;
agents_crud, menu_picker, and run.py import from here.
"""

import re
import subprocess
import time

from dotenv import dotenv_values  # pip install python-dotenv

from .docker_config import register_docker_gid, register_whitelist_domains
from .paths import (
    AGENTS_DIR, CACHE_MOUNTS, CACHE_ROOT, DEFAULT_CONF, FIREWALL_WHITELIST_FILE, MEMORY_DIR,
)
from .utils import parse_lines

MD_EXT = ".md"
CONF_EXT = ".conf"

# === Priority orderings — earlier entries sort/place ahead of later ones. ===
# ORDERED_TAGS / ORDERED_MODES drive the chain composition (each walrus also
# publishes the matching TAG_*/MODE_* module-level constant; adding a new
# tag/mode means appending one line here AND wiring its handler in *_HANDLERS
# below). ORDERED_MODEL_FAMILIES only feeds the picker's sort priority.

ORDERED_MODEL_FAMILIES = ["opus", "sonnet", "haiku"]

ORDERED_TAGS = [
    TAG_PROG  := "prog",
]

ORDERED_MODES = [
    MODE_AUTO := "auto",
    MODE_DOOD := "DooD",
]

# Short, one-sentence explanations of each tag/mode — surfaced by the picker's
# F8 composition legend so users can recall what a given `[tag]` / `{mode}`
# implies. Keys must match the names in ORDERED_TAGS / ORDERED_MODES.
TAG_DESCRIPTIONS = {
    TAG_PROG: "programming-oriented; built with various programs and toolchains (Rust, Node, build-essential, uv)",
}

MODE_DESCRIPTIONS = {
    MODE_AUTO: "autonomous; Doesn't need permission to perform actions. Built with a firewall slightly increased security. Danger: hard to control!",
    MODE_DOOD: "Docker outside-of Docker; Can run Docker. Danger: authority to do anything (effectively host-root)!",
}

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


def parse_stem(stem):
    """Parse a filename stem into (name, tags, parent).

    Grammar: <name>(<bracketed-tag>|<parenthesized-parent>)*
      - `[tag]` accumulates into tags (list, in the order they appear).
      - `(parent)` is single-valued; if repeated, last wins.
      - Order between brackets and parens is free: 'name[prog](thinker)' and
        'name(thinker)[prog]' both parse the same way.

    Examples:
        'name'                → ('name', [], None)
        'name(thinker)'       → ('name', [], 'thinker')
        'name[prog]'          → ('name', ['prog'], None)
        'name[prog](thinker)' → ('name', ['prog'], 'thinker')
        'name[a][b]'          → ('name', ['a', 'b'], None)
    """
    m = re.match(r"^([^()\[\]]+)", stem)
    if not m:
        return (stem, [], None)
    name = m.group(1)
    tags = []
    parent = None
    for paren, bracket in re.findall(r"\(([^()]+)\)|\[([^\[\]]+)\]", stem[len(name):]):
        if paren:
            parent = paren
        else:
            tags.append(bracket)
    return (name, tags, parent)


def find_md_for_agent(agent_name):
    """Locate an agent's .md by its clean name; handles any [tag]/(parent) suffix combination
    in the filename. Glob can't express the new grammar (`[` is a glob metacharacter), so we
    enumerate `.md` files and match on the parsed stem — cheap with the project's agent count."""
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        if parse_stem(path.stem)[0] == agent_name:
            return path
    return None


def load_conf(md_path):
    """Locate and load an agent's .conf. Returns (path_or_None, values_dict).
    A '(parent)' suffix in the filename aliases to '<parent>.conf'; otherwise '<name>.conf';
    falls back to DEFAULT_CONF. Tags are ignored here — handled by apply_composition()."""
    name, _, parent = parse_stem(md_path.stem)
    specific = AGENTS_DIR / f"{(parent or name)}{CONF_EXT}"
    conf_path = specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)
    return conf_path, (dotenv_values(conf_path) if conf_path else {})


def parse_model_id(model):
    """Extract (family, major, minor) from a model ID like 'claude-opus-4-7'.
    Returns None when no recognized family is present."""
    m = re.search(r"(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def agent_sort_key(item):
    """Sort by family (ORDERED_MODEL_FAMILIES order — opus first, haiku last),
    then version desc, then name asc. Agents whose .conf has no recognisable
    model sink past all known families via the sentinel index."""
    name, path = item
    _, conf = load_conf(path)
    family, major, minor = parse_model_id(conf.get("ANTHROPIC_MODEL", "")) or (None, 0, 0)
    return (_ordering_index_or_end(family, ORDERED_MODEL_FAMILIES), (-major, -minor), name)


def _ordering_index_or_end(value, ordering):
    """Position of `value` in `ordering`, or `len(ordering)` if absent — pushes
    unknowns past the end when used as a sort-key element."""
    return ordering.index(value) if value in ordering else len(ordering)


def tag_sort_key(tags):
    """Sort key for agents grouped by tag set, following ORDERED_TAGS order.
    Untagged ([]) → empty tuple, which sorts before any non-empty key. Unknown
    tags sink past the end via a sentinel index so typo'd tags don't mix into
    the untagged group."""
    return tuple(sorted(_ordering_index_or_end(t, ORDERED_TAGS) for t in tags))


def mode_sort_key(modes):
    """Sort key for instances grouped by mode set, following ORDERED_MODES order.
    Mode-less ([]) → empty tuple, which sorts before any non-empty key. Unknown
    modes sink past the end via a sentinel index."""
    return tuple(sorted(_ordering_index_or_end(m, ORDERED_MODES) for m in modes))


# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        host.mkdir(parents=True, exist_ok=True)


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
                f.unlink()
                freed += s.st_size
        if freed:
            print(f"  Pruned {host.relative_to(CACHE_ROOT)}: freed {freed / 1024**3:.1f} GB (was {total / 1024**3:.1f} GB)")


def _apply_prog():
    """[prog] tag handler. Two responsibilities — the side effects run *first*,
    then the dict of runtime extras is returned:

      • SIDE EFFECTS: prepare_caches() mkdirs the host cache dirs (so the
        bind-mount targets exist before the container starts; otherwise Docker
        creates them as root and we can't clean them up later); prune_caches()
        opportunistically trims oversized caches.
      • RETURNS: volume_args list of -v flags mounting those caches into the
        container at the matching paths.

    The compose/Dockerfile pair (compose.prog.yml + docker/Dockerfile.prog) is
    NOT selected here — chain order in compute_chain handles that."""
    prepare_caches()
    prune_caches()
    return {
        "volume_args": [arg for h, c in CACHE_MOUNTS.items() for arg in ("-v", f"{h}:{c}")],
    }


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
    host's docker-group GID via getent and hands it to docker_config.register_docker_gid
    so the compose layer can pick it up as a build-arg. Without a docker group on
    the host, the agent couldn't access the bind-mounted socket — so we fail loudly
    here rather than build an image that won't work."""
    gid = _detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            "DooD mode requires a `docker` group on the host. On Linux: "
            "`sudo usermod -aG docker $USER` (then log out + back in). "
            "If you don't actually need DooD, modify the instance and decline the prompt."
        )
    register_docker_gid(gid)
    return {}


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
    for d in set(BUILTIN_FIREWALL_DOMAINS) | set(parse_lines(FIREWALL_WHITELIST_FILE)):
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
    Side effect: hands the resolved domain list (built-ins + user entries +
    www↔apex counterparts where applicable) to docker_config.register_whitelist_domains
    so the compose layer can pass it through to init-firewall.sh — no whitelist
    file mount needed inside the container."""
    register_whitelist_domains(resolved_whitelist_domains())
    return {}


# Handler dispatch tables. Tag/mode ordering lives in ORDERED_TAGS / ORDERED_MODES
# (defined up top with the other constants); these dicts are pure name → handler
# lookups. TAG_HANDLERS sits next to MODE_HANDLERS so the dispatch surface for the
# whole composition layer is in one place.
TAG_HANDLERS = {TAG_PROG: _apply_prog}
MODE_HANDLERS = {
    MODE_AUTO: _apply_auto,
    MODE_DOOD: _apply_dood,
}


# === Chain composition: the build/run image is layered base → tags (in ORDERED_TAGS order) → modes (in ORDERED_MODES order). ===

def compute_chain(tags, modes):
    """Return the build chain for the given tags + modes. Always starts with 'base';
    appends tags in ORDERED_TAGS order, then modes in ORDERED_MODES order. Result
    drives both image naming (claude-agents:<chain[1:] joined by dot>, or
    claude-agents:base for chain == ['base']) and the compose -f stack. Unknown
    tags/modes raise ValueError so a typo surfaces loudly.

    `tags` / `modes` accept any iterable; coerced to sets internally for O(1)
    membership checks and natural deduplication."""
    tags, modes = set(tags), set(modes)
    if unknown := tags - set(ORDERED_TAGS):
        raise ValueError(f"Unknown tag(s): {sorted(unknown)}. Known tags: {ORDERED_TAGS}")
    if unknown := modes - set(ORDERED_MODES):
        raise ValueError(f"Unknown mode(s): {sorted(unknown)}. Known modes: {ORDERED_MODES}")
    chain = ["base"]
    for tag in ORDERED_TAGS:
        if tag in tags:
            chain.append(tag)
    for mode in ORDERED_MODES:
        if mode in modes:
            chain.append(mode)
    return chain


def apply_composition(chain):
    """Run handlers for each non-base step in the chain and aggregate their contributions.
    Returns {volume_args: [str,...]}. Tag steps look up TAG_HANDLERS, mode steps MODE_HANDLERS;
    'base' has no handler and is skipped. Unknown steps raise ValueError — chains coming from
    compute_chain are pre-validated, so this only fires if the caller built a chain by hand.

    NOTE: handlers may have side effects (host-side mkdirs, env-var exports for
    build-args, etc.) — see each `_apply_*` docstring. Calling apply_composition
    therefore both *gathers* runtime extras AND *triggers* any pre-build/pre-run
    setup the chain implies."""
    aggregated = {"volume_args": []}
    for step in chain[1:]:
        handler = TAG_HANDLERS.get(step) or MODE_HANDLERS.get(step)
        if handler is None:
            raise ValueError(f"Unknown chain step: {step}. Known: {list(TAG_HANDLERS) + list(MODE_HANDLERS)}")
        contribution = handler()
        for key in aggregated:
            aggregated[key].extend(contribution.get(key, []))
    return aggregated

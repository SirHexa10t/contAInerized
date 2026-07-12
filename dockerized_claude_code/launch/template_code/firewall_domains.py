"""Firewall data for the {auto} mode — pure data, no logic (the
template_code/ convention). Two tables:

  BUILTIN_FIREWALL_DOMAINS — the curated always-allowed domain list. The
      user's firewall_whitelist.txt is unioned in at resolution time
      (network.start_whitelist_resolution). Every form you want allowed must
      be listed explicitly (e.g. both `foo.com` and `www.foo.com` if both are
      needed); the one convenience: a `www.X` entry also implicitly allows
      `X`, since typing the `www.` form clearly means the bare apex too.

  CDN_IPV4_RANGES — published IPv4 blocks of the major CDN providers, used
      by network.py's CDN widening: when a whitelisted host resolves into one
      of these blocks, the whole containing block is whitelisted instead of
      pinning the momentary IPs, so CDN POP rotation can't strand the host
      behind a stale pinned IP.

      ⚠ Security tradeoff (deliberate, user-requested): a CDN block is shared
      by every customer of that CDN — allowing the block makes OTHER sites
      served from those same addresses reachable too (HTTPS routing is
      SNI-based, one IP serves many customers). The widening only triggers
      when a *whitelisted* host is detected on that CDN, and only for
      default-port entries — but the effective grant is
      "this CDN's edge, on these blocks", not "this one site".

      Provenance (long-stable published lists — refresh occasionally):
        cloudflare  — https://www.cloudflare.com/ips-v4
        fastly      — https://api.fastly.com/public-ip-list
        github      — https://api.github.com/meta (web/api/pages edges)
        cloudfront  — AWS ip-ranges.json, service=CLOUDFRONT (deliberately a
                      subset: the large long-stable blocks; POPs outside them
                      simply fall back to pinned-IP behavior)

Consumed only by launch/network.py."""

BUILTIN_FIREWALL_DOMAINS = [
    # === Core launcher dependencies ===
    # Anthropic
    "api.anthropic.com",
    "console.anthropic.com",
    "www.claude.ai",
    # GitHub (git, releases, raw, codeload, container registry)
    "www.github.com",
    "api.github.com",
    "ssh.github.com",
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
    # Atlassian (Jira / Confluence / Bitbucket) marketing + docs; per-tenant subdomains
    # (e.g. <org>.atlassian.net) need their own entry in the user whitelist since
    # CloudFront sharding can put them on a different POP than the apex.
    "www.atlassian.net",
    "www.atlassian.com",
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
    # Browser automation ({web} mode — browser-binary CDN, bare apex only)
    "cdn.playwright.dev",
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
    "www.paperswithcode.com",
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
    "www.semver.org",
    "www.json.org",
    # Build & tooling
    "www.webpack.js.org",
    "www.vite.dev",
    "www.rollupjs.org",
    "www.esbuild.github.io",
    "www.cmake.org",
    "www.ninja-build.org",
    "www.git-scm.com",
    # Reliable tutorial / reference sites
    "www.realpython.com",        # Python
    "www.baeldung.com",          # Java / Spring
    "www.digitalocean.com",      # community tutorials
    "www.css-tricks.com",        # web / CSS
    "www.smashingmagazine.com",  # web / CSS
    "www.learnxinyminutes.com",  # quick-reference cheat sheets per language
    "cheatsheetseries.owasp.org",  # web / app security cheat sheets
    "www.martinfowler.com",      # architecture and refactoring
    "www.fly.io",                # systems / networking writing on fly.io/blog
]


# {provider: (cidr, ...)} — every block must be valid IPv4 CIDR
# (test_network validates each entry parses). Adding a provider or block here
# is the whole change — network.py's detection iterates this table.
CDN_IPV4_RANGES: dict[str, tuple[str, ...]] = {
    "cloudflare": (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
        "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
        "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
        "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
        "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    ),
    "fastly": (
        "23.235.32.0/20", "43.249.72.0/22", "103.244.50.0/24",
        "103.245.222.0/23", "103.245.224.0/24", "104.156.80.0/20",
        "140.248.64.0/18", "140.248.128.0/17", "146.75.0.0/17",
        "151.101.0.0/16", "157.52.64.0/18", "167.82.0.0/17",
        "167.82.128.0/20", "167.82.160.0/20", "167.82.224.0/20",
        "172.111.64.0/18", "185.31.16.0/22", "199.27.72.0/21",
        "199.232.0.0/16",
    ),
    "github": (
        "140.82.112.0/20", "143.55.64.0/20", "185.199.108.0/22",
        "192.30.252.0/22",
    ),
    "cloudfront": (
        "13.32.0.0/15", "13.224.0.0/14", "18.64.0.0/14", "52.84.0.0/15",
        "54.230.0.0/16", "54.239.128.0/18", "99.84.0.0/16",
        "108.156.0.0/14", "143.204.0.0/16", "205.251.192.0/19",
    ),
}

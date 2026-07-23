"""Firewall data for the {firewall} specialty — pure data, no logic (the
template_code/ convention). One table:

  BUILTIN_FIREWALL_DOMAINS — the curated always-allowed domain list. The
      user's firewall_whitelist.txt is unioned in at resolution time
      (network.start_whitelist_resolution). Every form you want allowed must
      be listed explicitly (e.g. both `foo.com` and `www.foo.com` if both are
      needed); the one convenience: a `www.X` entry also implicitly allows
      `X`, since typing the `www.` form clearly means the bare apex too.

No IP address ranges live here (or anywhere in the source): the CDN provider
blocks that drive the firewall resolver's widening are fetched from each provider's own
published list at launch and cached on disk — see firewall.resolver._RANGE_FETCHERS.

Consumed only by launch/firewall/resolver.py."""

BUILTIN_FIREWALL_DOMAINS = [
    # === Core launcher dependencies ===
    # Anthropic
    "api.anthropic.com",
    "console.anthropic.com",
    "www.claude.ai",
    # GitHub (git, releases, raw, codeload, container registry). The
    # githubusercontent hosts are the real asset CDNs: release-download URLs
    # 302 to objects./release-assets. — a reachable github.com is useless for
    # fetching a release if those aren't open too.
    "*.github.com",
    "*.api.github.com",
    "*.ssh.github.com",
    "*.raw.githubusercontent.com",
    "*.gist.githubusercontent.com",
    "*.objects.githubusercontent.com",
    "*.release-assets.githubusercontent.com",
    "www.github.com",
    "codeload.github.com",
    "www.ghcr.io",
    # npm
    "*.registry.npmjs.org",
    # PyPI
    "*.www.pypi.org",
    "*.files.pythonhosted.org",
    # crates.io (Rust) + rustup dist host (toolchains & components — rustup
    # inside a [code]{auto} container can't add clippy/rustfmt without it)
    "*.www.crates.io",
    "*.static.crates.io",
    "*.index.crates.io",
    "*.static.rust-lang.org",

    # === Developer documentation & references ===
    # Q&A and community
    "www.stackoverflow.com",
    "www.stackexchange.com",     # covers DBA / Security / Code Review etc.; Server Fault and Super User live at their own apexes
    "*.gitlab.com",
    "www.gitlab.com",
    # Atlassian (Jira / Confluence / Bitbucket) marketing + docs; per-tenant subdomains
    # (e.g. <org>.atlassian.net) need their own entry in the user whitelist since
    # CloudFront sharding can put them on a different POP than the apex.
    "*.atlassian.net",
    "www.atlassian.net",
    "*.atlassian.com",
    "www.atlassian.com",
    # Language docs — Python (PyPI registry above)
    "*.docs.python.org",
    "*.peps.python.org",
    # Language docs — Rust (crates.io registry above)
    "*.doc.rust-lang.org",
    "www.rust-lang.org",
    "*.docs.rs",
    "www.docs.rs",
    # Language docs — Node.js / JavaScript (npm registry above)
    "*.nodejs.org",
    "www.nodejs.org",
    "*.developer.mozilla.org",  # MDN — also covers HTML / CSS / Web APIs
    "*.npmjs.com",
    "www.npmjs.com",
    "*.tc39.es",     # ECMAScript spec
    # Language docs — TypeScript
    "www.typescriptlang.org",
    # Language docs — Go
    "*.go.dev",
    "pkg.go.dev",
    # Language docs — Java
    "docs.oracle.com",
    "openjdk.org",
    "*.mvnrepository.com",
    "www.mvnrepository.com",
    "*.search.maven.org",
    # Language docs — C# / .NET (also covers Azure, VS Code, TypeScript, etc.)
    "www.learn.microsoft.com",
    # Language docs — C / C++
    "*.en.cppreference.com",
    "www.en.cppreference.com",
    "*.isocpp.org",
    "www.isocpp.org",
    # Language docs — Ruby
    "*.ruby-lang.org",
    "www.ruby-lang.org",
    "www.ruby-doc.org",
    "*.rubygems.org",
    "www.rubygems.org",
    # Language docs — PHP
    "www.php.net",
    "www.packagist.org",
    # Language docs — Swift / Apple
    "www.swift.org",
    "www.developer.apple.com",
    # Language docs — Kotlin
    "*.kotlinlang.org",
    "www.kotlinlang.org",
    # Language docs — Other
    "www.haskell.org",
    "www.dart.dev",
    "*.elixir-lang.org",
    "www.elixir-lang.org",
    "*.hexdocs.pm",
    "www.hexdocs.pm",
    "*.scala-lang.org",
    "www.scala-lang.org",
    "*.clojure.org",
    "www.clojure.org",
    "*.julialang.org",
    "www.julialang.org",
    "www.ocaml.org",
    "www.erlang.org",
    "www.r-project.org",
    "www.cran.r-project.org",
    "*.perl.org",
    "www.perl.org",
    "*.perldoc.perl.org",
    "www.perldoc.perl.org",
    "www.lua.org",
    # Cloud / infra — AWS
    "*.docs.aws.amazon.com",
    "*.aws.amazon.com",
    "www.aws.amazon.com",
    "*.repost.aws",            # AWS re:Post Q&A
    "www.repost.aws",            # AWS re:Post Q&A
    # Cloud / infra — GCP
    "*.cloud.google.com",
    "www.cloud.google.com",
    "*.firebase.google.com",
    # Cloud / infra — Azure (learn.microsoft.com above)
    "www.azure.microsoft.com",
    # Cloud / infra — Docker / Kubernetes / Helm
    "*.docs.docker.com",
    "www.kubernetes.io",
    "www.helm.sh",
    # Cloud / infra — HashiCorp (Terraform, Vault, Consul, Nomad)
    "developer.hashicorp.com",
    # Web standards
    "www.whatwg.org",            # HTML / DOM / Fetch specs
    "*.w3.org",                # W3C specs
    "www.w3.org",                # W3C specs
    "www.caniuse.com",           # browser compat tables
    "*.web.dev",               # Google web best-practices
    "www.web.dev",               # Google web best-practices
    # Frontend frameworks
    "www.react.dev",
    "www.vuejs.org",
    "www.angular.dev",
    "www.svelte.dev",
    "www.nextjs.org",
    "www.nuxt.com",
    "*.remix.run",
    "www.remix.run",
    "www.astro.build",
    # Browser automation ({web} mode — browser-binary CDN, bare apex only)
    "*.playwright.dev",
    "cdn.playwright.dev",
    # Backend frameworks — Python
    "*.docs.djangoproject.com",
    "*.flask.palletsprojects.com",
    "*.fastapi.tiangolo.com",
    # Backend frameworks — Node
    "*.expressjs.com",
    "www.expressjs.com",
    "www.nestjs.com",
    # Backend frameworks — Java
    "*.spring.io",
    "www.spring.io",
    "*.docs.spring.io",
    # Backend frameworks — Ruby
    "*.rubyonrails.org",
    "www.rubyonrails.org",
    "guides.rubyonrails.org",
    # Backend frameworks — PHP
    "*.laravel.com",
    "www.laravel.com",
    "*.symfony.com",
    "www.symfony.com",
    # ML / data
    "www.pytorch.org",
    "*.tensorflow.org",
    "www.tensorflow.org",
    "*.scikit-learn.org",
    "www.scikit-learn.org",
    "*.numpy.org",
    "www.numpy.org",
    "*.pandas.pydata.org",
    "*.jupyter.org",
    "www.jupyter.org",
    "*.huggingface.co",
    "www.huggingface.co",
    "*.arxiv.org",
    "www.arxiv.org",
    "*.paperswithcode.com",
    "www.paperswithcode.com",
    # AI / LLM APIs (Anthropic API endpoints above)
    "docs.anthropic.com",
    "*.platform.openai.com",
    # Databases
    "www.postgresql.org",
    "dev.mysql.com",
    "*.mariadb.com",
    "www.mariadb.com",
    "www.sqlite.org",
    "*.redis.io",
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
    "*.datatracker.ietf.org",
    "*.rfc-editor.org",
    "www.rfc-editor.org",
    "*.semver.org",
    "www.semver.org",
    "www.json.org",
    # Build & tooling
    "*.webpack.js.org",
    "www.webpack.js.org",
    "www.vite.dev",
    "www.rollupjs.org",
    "*.esbuild.github.io",
    "www.esbuild.github.io",
    "www.cmake.org",
    "*.ninja-build.org",
    "www.ninja-build.org",
    "*.git-scm.com",
    "www.git-scm.com",
    # Reliable tutorial / reference sites
    "*.realpython.com",        # Python
    "www.realpython.com",        # Python
    "*.baeldung.com",          # Java / Spring
    "www.baeldung.com",          # Java / Spring
    "*.digitalocean.com",      # community tutorials
    "www.digitalocean.com",      # community tutorials
    "*.css-tricks.com",        # web / CSS
    "www.css-tricks.com",        # web / CSS
    "www.smashingmagazine.com",  # web / CSS
    "*.learnxinyminutes.com",  # quick-reference cheat sheets per language
    "www.learnxinyminutes.com",  # quick-reference cheat sheets per language
    "cheatsheetseries.owasp.org",  # web / app security cheat sheets
    "www.martinfowler.com",      # architecture and refactoring
    "www.fly.io",                # systems / networking writing on fly.io/blog
]

"""Structural integrity: assert the files the launcher relies on actually
exist at the paths it computes. Catches "renamed/moved a file but missed a
reference" regressions immediately.

For each chain tag with launch-side machinery, this also verifies the
required `_apply_<name>` handler is in place — a missing artifact fails
here at test time rather than at runtime.
The tag tree itself (Dockerfiles, tag.info shapes, `.lego` references) is
validated by TestTagTreeDiscovery / TestAgentLegoFiles below via scan_all."""

import unittest

from launch import paths, tag_handlers
from launch.tags import load_lego, scan_all

# Chain tags with host-side launch behavior must have an _apply_ handler;
# the rest are data-only ({auto} = claude_args + wants; [web]'s playwright
# cache rides [code]'s ~/.cache mount).
HANDLER_TAGS = ["code", "dood", "firewall"]
DATA_ONLY_TAGS = ["web", "auto"]


class TestRepoLayout(unittest.TestCase):
    def test_dockerized_root_exists(self):
        self.assertTrue(paths.DOCKERIZED_CLAUDE_ROOT.is_dir())

    def test_agents_dir_exists(self):
        self.assertTrue(paths.AGENTS_DIR.is_dir())

    def test_settings_dir_exists(self):
        self.assertTrue(paths.SETTINGS_DIR.is_dir())

    def test_template_files_dir_exists(self):
        self.assertTrue(paths.TEMPLATE_FILES_DIR.is_dir())


class TestBaseDockerArtifacts(unittest.TestCase):
    def test_base_dockerfile_exists(self):
        self.assertTrue(paths.BASE_DOCKERFILE.is_file())

    def test_base_dockerfile_carries_firewall_prerequisites(self):
        # {firewall} has no image layer — iptables + the scoped sudoers entry
        # ride the base image (inert without the specialty's mounted script).
        text = paths.BASE_DOCKERFILE.read_text()
        self.assertIn("iptables", text)
        self.assertIn("init-firewall.sh", text)   # the sudoers NOPASSWD line


class TestTagHandlerArtifacts(unittest.TestCase):
    """Tags with host-side launch behavior must keep their `_apply_<name>`
    handler; data-only tags must NOT grow one silently (their behavior is
    declared in tag.info / tag.docker, and an unexpected handler would mean
    logic crept out of the data). Dockerfile/tag.docker presence is checked
    against the discovered tree in TestTagTreeDiscovery."""

    def test_handler_tags_have_apply_handler(self):
        for tag in HANDLER_TAGS:
            with self.subTest(tag=tag):
                handler = getattr(tag_handlers, f"_apply_{tag}", None)
                self.assertTrue(callable(handler),
                                f"tag_handlers is missing _apply_{tag}()")

    def test_data_only_tags_have_no_handler(self):
        for tag in DATA_ONLY_TAGS:
            with self.subTest(tag=tag):
                self.assertFalse(hasattr(tag_handlers, f"_apply_{tag}"),
                                 f"_apply_{tag} exists — {tag} is meant to be data-only")

    def test_root_has_no_layer_dockerfiles(self):
        # Layer Dockerfiles live in the agents/ tree; a stray root
        # Dockerfile.<x> would be dead weight (or worse, shadow one).
        strays = list(paths.DOCKERIZED_CLAUDE_ROOT.glob("Dockerfile.*"))
        self.assertEqual(strays, [])

    def test_base_has_no_apply_handler(self):
        # base has no side effects beyond being the starting image — no handler.
        self.assertFalse(hasattr(tag_handlers, "_apply_base"))


class TestUserExtrasTemplates(unittest.TestCase):
    """Template files that the launcher plants into ~/.claude-agents/user_extras/
    on first launch."""

    def test_firewall_whitelist_template_exists(self):
        self.assertTrue(paths.FIREWALL_WHITELIST_TEMPLATE.is_file())

    def test_optional_creds_readme_template_exists(self):
        self.assertTrue(paths.OPTIONAL_CREDS_README_TEMPLATE.is_file())


class TestDefaultAgentConf(unittest.TestCase):
    def test_default_conf_exists(self):
        self.assertTrue(paths.DEFAULT_CONF.is_file())


class TestTagTreeDiscovery(unittest.TestCase):
    """Integration checkpoint (tags rewrite): the real agents/ tree is
    discovered + validated by the tags package. Guards every migration step —
    a mis-shaped tag dir (missing tag.info, stray dir, orphan _layer, dangling
    reference) fails scan_all here rather than at launch. Kinds whose subtrees
    aren't migrated yet are simply absent (empty), which scan_all allows."""

    def setUp(self):
        self.reg = scan_all(paths.AGENTS_DIR)

    def test_engines_discovered(self):
        self.assertLessEqual(
            {"default", "golem", "poet", "thinker", "researcher", "breakthrough"},
            set(self.reg.engines),
        )

    def test_engine_conf_resolves_through_new_tree(self):
        self.assertIn("haiku", self.reg.engines["golem"].conf_map.get("ANTHROPIC_MODEL", ""))
        self.assertTrue(self.reg.engines["breakthrough"].conf_map.get("ANTHROPIC_MODEL"))

    def test_professions_discovered_with_nesting_requires(self):
        self.assertLessEqual({"code", "web"}, set(self.reg.professions))
        self.assertEqual(self.reg.professions["web"].requires, frozenset({"code"}))

    def test_specialties_discovered_with_layer_requires(self):
        self.assertLessEqual({"auto", "dood"}, set(self.reg.specialties))
        self.assertEqual(self.reg.specialties["dood"].requires, frozenset({"code"}))  # via _dood layer
        self.assertTrue(self.reg.specialties["auto"].warn)

    def test_professions_have_dockerfile(self):
        for name in ("code", "web"):
            self.assertTrue((self.reg.professions[name].path / "Dockerfile").is_file())

    def test_policies_discovered(self):
        self.assertLessEqual({"web-research", "no-sudo"}, set(self.reg.policies))
        self.assertEqual(self.reg.policies["web-research"].label, "<+query>")


class TestAgentLegoFiles(unittest.TestCase):
    """Every agent has a `.lego`, and each references only real tags on the
    right axes. Validated against the discovered registry — a typo'd
    engine/profession name, or a tag listed on the wrong axis, fails here
    rather than at launch."""

    def setUp(self):
        self.reg = scan_all(paths.AGENTS_DIR)

    def test_every_agent_has_a_lego(self):
        for md in paths.AGENTS_DIR.glob("*.md"):
            name = md.stem
            with self.subTest(agent=name):
                self.assertTrue((paths.AGENTS_DIR / f"{name}.lego").is_file(),
                                f"agent {name!r} ({md.name}) has no {name}.lego")

    def test_every_lego_validates_against_registry(self):
        legos = sorted(paths.AGENTS_DIR.glob("*.lego"))
        self.assertTrue(legos, "no .lego files found")
        for lego in legos:
            with self.subTest(lego=lego.name):
                self.reg.validate_build(load_lego(lego), lego)   # raises on any bad reference


class TestFirewallSpecialtyArtifacts(unittest.TestCase):
    """{firewall} bind-mounts init-firewall.sh + firewall-entrypoint.sh into
    the container (declared in its tag.docker; sources existence-checked at
    scan time, but guard the shipped tree explicitly too)."""

    def setUp(self):
        self.fw = scan_all(paths.AGENTS_DIR).specialties["firewall"]

    def test_mount_sources_resolve_to_real_files(self):
        self.assertTrue(self.fw.docker.mounts)
        for source, _target in self.fw.docker.mounts:
            with self.subTest(path=str(source)):
                self.assertTrue(source.is_file(), f"mount source missing: {source}")

    def test_entrypoint_and_cap_declared(self):
        self.assertEqual(self.fw.docker.entrypoint, "firewall-entrypoint.sh")
        self.assertIn("NET_ADMIN", self.fw.docker.cap_add)

    def test_no_image_layer(self):
        # The firewall is run-time-only — iptables lives in the base image.
        self.assertIsNone(self.fw.layer)

    def test_auto_wants_firewall(self):
        auto = scan_all(paths.AGENTS_DIR).specialties["auto"]
        self.assertIn("firewall", dict(auto.wants))
        self.assertIn("--dangerously-skip-permissions", auto.claude_args)


if __name__ == "__main__":
    unittest.main()

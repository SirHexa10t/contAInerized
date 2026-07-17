"""Tests for the launch.tags package (P0 of the tags rewrite).

Everything here runs against **fixture trees** built in temp dirs — the
scanners take an `agents_dir` argument, so no test touches the real repo
`agents/` (which still holds the pre-rewrite layout until P1). Coverage:
per-kind discovery, tree-derived requirements, hidden-layer claiming, engine
conf inheritance, policy-fragment merge rules, `.lego` parsing/validation,
manifest parsing edge cases, and the registry's cross-cutting validation.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from launch import tags
from launch.tags import (
    AgentBuild, Engine, Instance, Policy, Profession, Registry, Specialty,
    TagError, addendums, image_chain, load_lego, merge_fragments, migrations,
    resolve_build, scan_all, store,
)


class TagTreeTestCase(unittest.TestCase):
    """Base: `tree({relpath: contents})` writes a fixture `agents/` dir under
    a per-test temp dir and returns its path. `dedent` is applied so tests can
    indent heredocs naturally."""

    def tree(self, spec: dict[str, str]) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "agents"
        for rel, contents in spec.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(contents))
        return root

    # A small, valid, full-coverage tree reused by several tests.
    def full_tree(self) -> Path:
        return self.tree({
            "engine/default/tag.info": 'description = "baseline"\n',
            "engine/default/engine.conf": 'CLAUDE_CODE_EFFORT_LEVEL=high\n',
            "profession/code/tag.info": 'description = "coding toolchains"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/web/tag.info": 'description = "browser"\n',
            "profession/code/web/Dockerfile": "FROM code\n",
            "profession/code/_dood/Dockerfile": "FROM code\n",
            "specialty/auto/tag.info": (
                'description = "work nonstop"\nwarn = true\n'
                'claude_args = ["--dangerously-skip-permissions"]\n'
                '[wants]\nfirewall = "open network!"\n'
            ),
            "specialty/dood/tag.info": 'description = "host docker"\nwarn = true\n',
            "specialty/firewall/tag.info": 'description = "whitelist"\n',
            "specialty/combos.info": '[warnings]\n"dood + auto" = "both = danger"\n',
            "policy/no-sudo/tag.info": 'description = "no sudo"\nshortname = "-su"\n',
            "policy/no-sudo/policy.json": '{"permissions": {"deny": ["Bash(sudo *)"]}}',
        })


# ============================================================
# Engine — discovery + conf inheritance
# ============================================================

class TestEngine(TagTreeTestCase):
    def test_flat_engine_conf(self):
        root = self.tree({
            "engine/golem/tag.info": 'description = "cheap"\n',
            "engine/golem/engine.conf": 'ANTHROPIC_MODEL="claude-haiku-4-5"\nCLAUDE_CODE_EFFORT_LEVEL=low\n',
        })
        (golem,) = Engine.scan(root)
        self.assertEqual(golem.name, "golem")
        self.assertEqual(golem.label, "(golem)")
        self.assertEqual(golem.conf_map,
                         {"ANTHROPIC_MODEL": "claude-haiku-4-5", "CLAUDE_CODE_EFFORT_LEVEL": "low"})

    def test_nested_engine_inherits_and_overrides(self):
        root = self.tree({
            "engine/thinker/tag.info": 'description = "t"\n',
            "engine/thinker/engine.conf": 'ANTHROPIC_MODEL="claude-opus-4-8"\nCLAUDE_CODE_EFFORT_LEVEL=high\n',
            "engine/thinker/breakthrough/tag.info": 'description = "b"\n',
            "engine/thinker/breakthrough/engine.conf": 'CLAUDE_CODE_EFFORT_LEVEL=max\n',
        })
        by_name = {e.name: e for e in Engine.scan(root)}
        # child inherits parent's model, overrides effort
        self.assertEqual(by_name["breakthrough"].conf_map,
                         {"ANTHROPIC_MODEL": "claude-opus-4-8", "CLAUDE_CODE_EFFORT_LEVEL": "max"})
        # parent untouched
        self.assertEqual(by_name["thinker"].conf_map["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_engine_without_conf_is_empty(self):
        root = self.tree({"engine/bare/tag.info": 'description = "no conf"\n'})
        (bare,) = Engine.scan(root)
        self.assertEqual(bare.conf_map, {})

    def test_valueless_conf_key_dropped(self):
        root = self.tree({
            "engine/x/tag.info": 'description = "x"\n',
            "engine/x/engine.conf": 'BARE_KEY\nREAL=1\n',
        })
        (x,) = Engine.scan(root)
        self.assertEqual(x.conf_map, {"REAL": "1"})

    def test_missing_engine_root_yields_nothing(self):
        self.assertEqual(Engine.scan(self.tree({"profession/code/tag.info": 'description="c"\n'})), [])


# ============================================================
# Profession — discovery + requires-from-nesting + hidden layers
# ============================================================

class TestProfession(TagTreeTestCase):
    def test_nesting_becomes_requires(self):
        root = self.full_tree()
        by_name = {p.name: p for p in Profession.scan(root)}
        self.assertEqual(by_name["code"].requires, frozenset())
        self.assertEqual(by_name["web"].requires, frozenset({"code"}))
        self.assertEqual(by_name["web"].label, "[web]")

    def test_underscore_dir_not_offered_as_profession(self):
        names = {p.name for p in Profession.scan(self.full_tree())}
        self.assertEqual(names, {"code", "web"})   # _dood excluded

    def test_shortname_defaults_to_name(self):
        (code,) = [p for p in Profession.scan(self.full_tree()) if p.name == "code"]
        self.assertEqual(code.shortname, "code")

    def test_stray_dir_without_taginfo_raises(self):
        # STRICT: a non-tag, non-underscore dir is a structural error — a
        # forgotten/misnamed tag.info would otherwise silently drop the tag
        # and sever any requirement edge routed through it.
        root = self.tree({
            "profession/code/tag.info": 'description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/stray/notes.txt": "not a tag\n",
        })
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Profession.scan(root)

    def test_stray_intermediate_breaks_nesting(self):
        # The inverse of the old "grouping" allowance: an intermediate dir
        # meant to hold [web] must itself be a tag (carry tag.info), else error.
        root = self.tree({
            "profession/grp/web/tag.info": 'description = "w"\n',
            "profession/grp/web/Dockerfile": "FROM base\n",
        })
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Profession.scan(root)

    def test_underscore_dir_elsewhere_is_skipped_not_layer(self):
        # A `_`-dir OUTSIDE the profession tree is just an ignored asset dir —
        # no layer semantics (layers are a profession-tree concept).
        root = self.tree({
            "specialty/auto/tag.info": 'description = "a"\n',
            "specialty/_shared/helper.sh": "#!/bin/sh\n",
        })
        self.assertEqual({s.name for s in Specialty.scan(root, {})}, {"auto"})

    def test_discover_layers(self):
        layers = Profession.discover_layers(self.full_tree())
        self.assertIn("dood", layers)
        self.assertEqual(layers["dood"].requires, frozenset({"code"}))

    def test_duplicate_hidden_layer_raises(self):
        root = self.tree({
            "profession/a/tag.info": 'description="a"\n', "profession/a/Dockerfile": "x\n",
            "profession/a/_dup/Dockerfile": "x\n",
            "profession/b/tag.info": 'description="b"\n', "profession/b/Dockerfile": "x\n",
            "profession/b/_dup/Dockerfile": "x\n",
        })
        with self.assertRaisesRegex(TagError, "duplicate hidden layer 'dup'"):
            Profession.discover_layers(root)

    def test_hidden_dir_with_taginfo_raises(self):
        root = self.tree({
            "profession/code/tag.info": 'description="c"\n', "profession/code/Dockerfile": "x\n",
            "profession/code/_bad/tag.info": 'description="oops"\n',
        })
        with self.assertRaisesRegex(TagError, "must not contain a tag"):
            Profession.discover_layers(root)


# ============================================================
# Specialty — discovery + layer claim + wants + combos
# ============================================================

class TestSpecialty(TagTreeTestCase):
    def _specialties(self, root):
        layers = Profession.discover_layers(root)
        return {s.name: s for s in Specialty.scan(root, layers)}

    def test_fields_and_label(self):
        s = self._specialties(self.full_tree())["auto"]
        self.assertTrue(s.warn)
        self.assertEqual(s.claude_args, ("--dangerously-skip-permissions",))
        self.assertEqual(s.label, "{auto}")
        self.assertEqual(s.wants_map, {"firewall": "open network!"})

    def test_layer_claim_supplies_requires(self):
        dood = self._specialties(self.full_tree())["dood"]
        self.assertIsNotNone(dood.layer)
        self.assertEqual(dood.requires, frozenset({"code"}))

    def test_specialty_without_layer_has_no_requires(self):
        auto = self._specialties(self.full_tree())["auto"]
        self.assertIsNone(auto.layer)
        self.assertEqual(auto.requires, frozenset())

    def test_combos_parse(self):
        (combo,) = tags.scan_combos(self.full_tree())
        self.assertEqual(combo.tags, frozenset({"dood", "auto"}))
        self.assertEqual(combo.message, "both = danger")

    def test_combo_single_name_raises(self):
        root = self.tree({"specialty/combos.info": '[warnings]\n"lonely" = "x"\n'})
        with self.assertRaisesRegex(TagError, "≥2 tag names"):
            tags.scan_combos(root)

    def test_absent_combos_file_is_empty(self):
        self.assertEqual(tags.scan_combos(self.tree({"specialty/auto/tag.info": 'description="a"\n'})), [])


# ============================================================
# Policy — discovery + fragment validation + merge
# ============================================================

class TestPolicy(TagTreeTestCase):
    def test_fields_and_fragment(self):
        (p,) = Policy.scan(self.tree({
            "policy/web-research/tag.info": 'description = "no ask"\nshortname = "+query"\nrisk_level = 2\n',
            "policy/web-research/policy.json": '{"permissions": {"allow": ["WebSearch"]}}',
        }))
        self.assertEqual(p.label, "<+query>")
        self.assertEqual(p.risk_level, 2)
        self.assertEqual(p.load_fragment(), {"permissions": {"allow": ["WebSearch"]}})

    def test_missing_json_raises(self):
        with self.assertRaisesRegex(TagError, "missing policy.json"):
            Policy.scan(self.tree({"policy/x/tag.info": 'description="x"\n'}))

    def test_non_object_fragment_raises(self):
        with self.assertRaisesRegex(TagError, "must be a JSON object"):
            Policy.scan(self.tree({
                "policy/x/tag.info": 'description="x"\n',
                "policy/x/policy.json": '["not", "an", "object"]',
            }))

    def test_bad_json_raises(self):
        with self.assertRaisesRegex(TagError, "invalid JSON"):
            Policy.scan(self.tree({
                "policy/x/tag.info": 'description="x"\n',
                "policy/x/policy.json": '{not valid',
            }))

    def test_stray_dir_under_policy_raises(self):
        # STRICT applies to every kind subtree, not just professions.
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Policy.scan(self.tree({
                "policy/real/tag.info": 'description = "r"\n',
                "policy/real/policy.json": "{}",
                "policy/junk/readme.txt": "notes\n",
            }))


class TestMergeFragments(unittest.TestCase):
    def test_disjoint_keys_union(self):
        self.assertEqual(
            merge_fragments([("a", {"x": 1}), ("b", {"y": 2})]),
            {"x": 1, "y": 2})

    def test_nested_dicts_recurse(self):
        self.assertEqual(
            merge_fragments([("a", {"p": {"allow": ["X"]}}), ("b", {"p": {"deny": ["Y"]}})]),
            {"p": {"allow": ["X"], "deny": ["Y"]}})

    def test_lists_concat_and_dedupe(self):
        self.assertEqual(
            merge_fragments([("a", {"allow": ["X", "Y"]}), ("b", {"allow": ["Y", "Z"]})]),
            {"allow": ["X", "Y", "Z"]})

    def test_equal_scalars_coexist(self):
        self.assertEqual(merge_fragments([("a", {"k": 1}), ("b", {"k": 1})]), {"k": 1})

    def test_scalar_conflict_names_both(self):
        with self.assertRaisesRegex(TagError, r"conflict at 'k'.*<a>.*<b>"):
            merge_fragments([("a", {"k": 1}), ("b", {"k": 2})])

    def test_shape_clash_is_conflict(self):
        with self.assertRaisesRegex(TagError, "conflict"):
            merge_fragments([("a", {"k": {"nested": 1}}), ("b", {"k": [1, 2]})])

    def test_sources_not_mutated(self):
        a = {"p": {"allow": ["X"]}}
        merge_fragments([("a", a), ("b", {"p": {"allow": ["Y"]}})])
        self.assertEqual(a, {"p": {"allow": ["X"]}})   # deepcopy protected the input


# ============================================================
# .lego — parse + reference validation
# ============================================================

class TestLego(TagTreeTestCase):
    def test_missing_file_is_empty_build(self):
        self.assertEqual(load_lego(Path("/nonexistent/x.lego")), AgentBuild())

    def test_parse(self):
        root = self.tree({"researcher.lego":
            'engine = "researcher"\nprofessions = ["code"]\nspecialties = ["auto", "firewall"]\npolicies = ["no-sudo"]\n'})
        build = load_lego(root / "researcher.lego")
        self.assertEqual(build.engine, "researcher")
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.selected(), {"researcher", "code", "auto", "firewall", "no-sudo"})

    def test_engine_must_be_string(self):
        root = self.tree({"x.lego": 'engine = ["nope"]\n'})
        with self.assertRaisesRegex(TagError, "'engine' must be a string"):
            load_lego(root / "x.lego")

    def test_axis_must_be_string_list(self):
        root = self.tree({"x.lego": 'professions = [1, 2]\n'})
        with self.assertRaisesRegex(TagError, "'professions' must be a list of strings"):
            load_lego(root / "x.lego")

    def test_validate_build_unknown_tag(self):
        reg = scan_all(self.full_tree())
        with self.assertRaisesRegex(TagError, "unknown tag 'ghost'"):
            reg.validate_build(AgentBuild(professions=("ghost",)), Path("x.lego"))

    def test_validate_build_wrong_axis(self):
        reg = scan_all(self.full_tree())
        # 'auto' is a specialty; listing it under professions is a wrong-axis error.
        with self.assertRaisesRegex(TagError, "'auto' is a specialty, not a profession"):
            reg.validate_build(AgentBuild(professions=("auto",)), Path("x.lego"))

    def test_validate_build_accepts_valid(self):
        reg = scan_all(self.full_tree())
        reg.validate_build(AgentBuild(engine="default", professions=("code", "web"),
                                      specialties=("auto", "firewall"), policies=("no-sudo",)),
                           Path("ok.lego"))   # no raise


# ============================================================
# tag.docker + tag.info parsing edge cases
# ============================================================

class TestManifestParsing(TagTreeTestCase):
    def test_docker_mount_relative_resolved_absolute_kept(self):
        root = self.tree({
            "specialty/firewall/tag.info": 'description = "fw"\n',
            "specialty/firewall/init.sh": "#!/bin/sh\n",
            "specialty/firewall/tag.docker": (
                '[run]\ncap_add = ["NET_ADMIN"]\nentrypoint = "init.sh"\n'
                'mounts = ["init.sh -> /usr/local/bin/init.sh:ro", "/var/run/docker.sock -> /var/run/docker.sock"]\n'
                'env_forward = ["WHITELIST_ADDRESSES"]\n'
            ),
        })
        (fw,) = Specialty.scan(root, {})
        d = fw.docker
        self.assertEqual(d.cap_add, ("NET_ADMIN",))
        self.assertEqual(d.env_forward, ("WHITELIST_ADDRESSES",))
        rel_src, rel_tgt = d.mounts[0]
        self.assertEqual(rel_src, root / "specialty/firewall/init.sh")   # relative → resolved to tag dir
        self.assertEqual(d.mounts[1][0], Path("/var/run/docker.sock"))   # absolute → kept
        self.assertEqual(rel_tgt, "/usr/local/bin/init.sh:ro")

    def test_docker_missing_mount_source_raises(self):
        root = self.tree({
            "specialty/x/tag.info": 'description="x"\n',
            "specialty/x/tag.docker": '[run]\nmounts = ["ghost.sh -> /bin/ghost.sh"]\n',
        })
        with self.assertRaisesRegex(TagError, "mount source 'ghost.sh' not found"):
            Specialty.scan(root, {})

    def test_docker_malformed_mount_raises(self):
        root = self.tree({
            "specialty/x/tag.info": 'description="x"\n',
            "specialty/x/tag.docker": '[run]\nmounts = ["no arrow here"]\n',
        })
        with self.assertRaisesRegex(TagError, "not 'source -> target'"):
            Specialty.scan(root, {})

    def test_docker_bare_entrypoint_must_exist(self):
        root = self.tree({
            "specialty/x/tag.info": 'description="x"\n',
            "specialty/x/tag.docker": '[run]\nentrypoint = "ghost.sh"\n',
        })
        with self.assertRaisesRegex(TagError, "entrypoint 'ghost.sh' not found"):
            Specialty.scan(root, {})

    def test_wants_non_string_message_raises(self):
        root = self.tree({"specialty/x/tag.info": 'description="x"\n[wants]\nfirewall = 5\n'})
        with self.assertRaisesRegex(TagError, "wants.firewall must be a string"):
            Specialty.scan(root, {})

    def test_malformed_toml_names_file(self):
        root = self.tree({"specialty/x/tag.info": 'description = "unterminated\n'})
        with self.assertRaisesRegex(TagError, r"tag\.info: cannot read TOML"):
            Specialty.scan(root, {})


# ============================================================
# Registry — cross-cutting validation
# ============================================================

class TestRegistryValidation(TagTreeTestCase):
    def test_full_tree_scans_clean(self):
        reg = scan_all(self.full_tree())
        self.assertIsInstance(reg, Registry)
        self.assertEqual(set(reg.engines), {"default"})
        self.assertEqual(set(reg.professions), {"code", "web"})
        self.assertEqual(set(reg.specialties), {"auto", "dood", "firewall"})
        self.assertEqual(set(reg.policies), {"no-sudo"})
        self.assertEqual(reg.kind_of("web"), "profession")
        self.assertIsNone(reg.get("nonexistent"))

    def test_cross_kind_name_collision_raises(self):
        root = self.tree({
            "profession/dup/tag.info": 'description="p"\n', "profession/dup/Dockerfile": "x\n",
            "specialty/dup/tag.info": 'description="s"\n',
        })
        with self.assertRaisesRegex(TagError, "'dup' used by both .* names must be unique"):
            scan_all(root)

    def test_orphan_hidden_layer_raises(self):
        # A `_ghost` layer with no matching specialty is an error.
        root = self.tree({
            "profession/code/tag.info": 'description="c"\n', "profession/code/Dockerfile": "x\n",
            "profession/code/_ghost/Dockerfile": "x\n",
        })
        with self.assertRaisesRegex(TagError, "hidden layer '_ghost' has no matching specialty"):
            scan_all(root)

    def test_unknown_wants_reference_raises(self):
        root = self.tree({"specialty/auto/tag.info": 'description="a"\n[wants]\nnosuchthing = "msg"\n'})
        with self.assertRaisesRegex(TagError, "wants unknown tag 'nosuchthing'"):
            scan_all(root)

    def test_unknown_combo_reference_raises(self):
        root = self.tree({
            "specialty/auto/tag.info": 'description="a"\n',
            "specialty/combos.info": '[warnings]\n"auto + ghost" = "x"\n',
        })
        with self.assertRaisesRegex(TagError, "combo references unknown tag 'ghost'"):
            scan_all(root)

    def test_within_kind_duplicate_name_raises(self):
        # Two engines resolving to the same name via nesting (parents are
        # valid tags, so strict is satisfied — the clash is the leaf name).
        root = self.tree({
            "engine/a/tag.info": 'description="a"\n',
            "engine/a/dup/tag.info": 'description="d1"\n',
            "engine/b/tag.info": 'description="b"\n',
            "engine/b/dup/tag.info": 'description="d2"\n',
        })
        with self.assertRaisesRegex(TagError, "duplicate engine 'dup'"):
            scan_all(root)

    def test_empty_tree_is_valid(self):
        reg = scan_all(self.tree({"placeholder.md": "x\n"}))
        self.assertEqual(reg.all_names(), set())


# ============================================================
# Identity — image chain + Instance + resolve_build
# ============================================================


class TestImageChain(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def test_base_only(self):
        self.assertEqual(image_chain((), ()), ["base"])

    def test_requirement_ordered_before_dependent(self):
        code, web = self.reg.professions["code"], self.reg.professions["web"]
        self.assertEqual(image_chain((web, code), ()), ["base", "code", "web"])   # code first despite input order

    def test_specialties_follow_professions(self):
        code, auto = self.reg.professions["code"], self.reg.specialties["auto"]
        self.assertEqual(image_chain((code,), (auto,)), ["base", "code", "auto"])

    def test_specialties_sorted_by_name(self):
        auto, dood = self.reg.specialties["auto"], self.reg.specialties["dood"]
        self.assertEqual(image_chain((), (dood, auto)), ["base", "auto", "dood"])


class TestInstance(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def _inst(self, **kw) -> Instance:
        base = dict(agent="researcher", md_path=Path("/x/researcher.md"),
                    session="proj", workspace="/tmp/ws", is_brand_new=True,
                    engine=self.reg.engines["default"])
        base.update(kw)
        return Instance(**base)

    def test_instance_id_and_paths(self):
        i = self._inst()
        self.assertEqual(i.instance, "researcher__proj")
        self.assertTrue(str(i.state_dir).endswith("researcher__proj"))
        self.assertEqual(i.state_md.name, "CLAUDE.md")

    def test_chain_and_conf(self):
        i = self._inst(professions=(self.reg.professions["code"],),
                       specialties=(self.reg.specialties["auto"],))
        self.assertEqual(i.chain, ["base", "code", "auto"])
        self.assertIn("CLAUDE_CODE_EFFORT_LEVEL", i.conf)

    def test_claude_args_from_specialties(self):
        i = self._inst(specialties=(self.reg.specialties["auto"],))
        self.assertIn("--dangerously-skip-permissions", i.claude_args)


class TestResolveBuild(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def test_names_resolve_to_objects(self):
        kw = resolve_build(AgentBuild(engine="default", professions=("code",),
                                      specialties=("auto",)), "x", self.reg)
        self.assertIs(kw["engine"], self.reg.engines["default"])
        self.assertEqual(kw["professions"], (self.reg.professions["code"],))
        self.assertEqual(kw["specialties"], (self.reg.specialties["auto"],))

    def test_engine_falls_back_to_default(self):
        # no engine named, agent name isn't an engine either → default
        kw = resolve_build(AgentBuild(), "poet", self.reg)
        self.assertIs(kw["engine"], self.reg.engines["default"])


# ============================================================
# Store — instances.toml load/save + legacy-map migration
# ============================================================


class TestStore(TagTreeTestCase):
    def test_load_missing_is_empty(self):
        self.assertEqual(store.load(Path("/nonexistent/instances.toml")), {})

    def test_save_load_roundtrip(self):
        p = self.tree({"placeholder": ""}) / "instances.toml"
        m = {"golem__x": {"workspace": "/w", "engine": "golem",
                          "professions": [], "specialties": [], "policies": []}}
        store.save(m, p)
        self.assertEqual(store.load(p), m)

    def test_saved_file_is_toml(self):
        p = self.tree({"placeholder": ""}) / "instances.toml"
        store.save({"golem__x": {"workspace": "/w", "professions": ["code"],
                                 "specialties": [], "policies": []}}, p)
        text = p.read_text()
        self.assertIn("[golem__x]", text)
        self.assertIn('workspace = "/w"', text)
        self.assertIn('professions = ["code"]', text)

    def test_none_values_omitted_and_read_back_absent(self):
        # TOML has no null — build_entry keeps None in the dict, dumps drops
        # it, and load simply doesn't have the key (readers .get() → None).
        p = self.tree({"placeholder": ""}) / "instances.toml"
        store.save({"golem__x": {"workspace": None, "engine": None,
                                 "professions": [], "specialties": [], "policies": []}}, p)
        entry = store.load(p)["golem__x"]
        self.assertNotIn("workspace", entry)
        self.assertNotIn("engine", entry)

    def test_non_bare_instance_id_quoted(self):
        # A future dotted agent name must not corrupt the file — the emitter
        # quotes any key that isn't a TOML bare key.
        p = self.tree({"placeholder": ""}) / "instances.toml"
        m = {"agent.v2__x": {"workspace": "/w", "professions": [],
                             "specialties": [], "policies": []}}
        store.save(m, p)
        self.assertIn('["agent.v2__x"]', p.read_text())
        self.assertEqual(store.load(p), m)

    def test_migrate_translates_modes_onto_axes(self):
        agents = self.tree({"researcher.lego": 'engine = "researcher"\nprofessions = ["code"]\n'})
        out = migrations.migrate_from_maps(
            {"researcher__proj": "/home/u/proj"},
            {"researcher__proj": ["auto", "DooD"]},
            agents,
        )
        self.assertEqual(out["researcher__proj"], {
            "workspace": "/home/u/proj", "engine": "researcher",
            # Legacy `auto` bundled the firewall → both specialties post-split.
            "professions": ["code"], "specialties": ["auto", "firewall", "dood"], "policies": [],
        })

    def test_migrate_web_mode_becomes_profession(self):
        agents = self.tree({"researcher.lego": 'engine = "researcher"\nprofessions = ["code"]\n'})
        out = migrations.migrate_from_maps({}, {"researcher__x": ["web"]}, agents)
        self.assertEqual(sorted(out["researcher__x"]["professions"]), ["code", "web"])
        self.assertEqual(out["researcher__x"]["specialties"], [])

    def test_migrate_engine_defaults_to_agent_when_lego_absent(self):
        agents = self.tree({"placeholder": ""})   # no .lego for 'poet'
        out = migrations.migrate_from_maps({"poet__d": "/w"}, {}, agents)
        self.assertEqual(out["poet__d"]["engine"], "poet")


# ============================================================
# Addendums — chain-keyed CLAUDE.md section composer
# ============================================================


class TestAddendums(unittest.TestCase):
    def test_base_carries_universal_notices(self):
        out = addendums.compose(["base"])
        self.assertIn(f"## {addendums.ADDENDUM_SECTION_TITLE}", out)
        self.assertIn(addendums.SEEK_SUMMARY.body, out)
        self.assertIn(addendums.MAINTAIN_PRIVACY.body, out)
        self.assertNotIn(addendums.FIREWALL_NOTICE.body, out)

    def test_firewall_adds_firewall_notice(self):
        self.assertIn(addendums.FIREWALL_NOTICE.body, addendums.compose(["base", "code", "firewall"]))

    def test_auto_alone_carries_no_firewall_notice(self):
        # Post-split, {auto} is pure skip-permissions — the firewall notice
        # belongs to {firewall}, which auto merely *wants*.
        self.assertNotIn(addendums.FIREWALL_NOTICE.body, addendums.compose(["base", "auto"]))

    def test_web_adds_browser_notice(self):
        self.assertIn(addendums.WEB_NOTICE.body, addendums.compose(["base", "code", "web"]))

    def test_empty_chain_renders_nothing(self):
        self.assertEqual(addendums.compose([]), "")

    def test_section_order_follows_chain(self):
        # base's summary sub-section precedes firewall's sub-section.
        out = addendums.compose(["base", "firewall"])
        self.assertLess(out.index("### Project summary"), out.index("### Firewall"))


if __name__ == "__main__":
    unittest.main()

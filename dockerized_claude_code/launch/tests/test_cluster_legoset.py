"""Tests for launch.cluster.legoset — cluster templates.

A `.legoset` is authored by hand, so every test here is about a mistake a human
would plausibly make in one: a typo'd key, two members that would collide, an
agent that does not exist. All of them must fail at PARSE time naming the file,
because the alternative is a cluster that half-launches.
"""

import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

from launch import paths
from launch.cluster.legoset import (
    assemble, auto_roles, discover_templates, instantiate, load_legoset,
    validate,
)
from launch.cluster.member import ClusterError, member_id
from launch.tags.lego import AgentBuild


class LegosetTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text: str, name: str = "team.legoset") -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class TestParsing(LegosetTmp):
    def test_table_form_with_multiplicity_and_roles(self):
        # The decided shape: two researchers, distinguished by role.
        template = load_legoset(self.write("""
members = [
  { agent = "project-starter" },
  { agent = "researcher", role = "primary" },
  { agent = "researcher", role = "adversarial" },
]
"""))
        self.assertEqual([m.id for m in template.members],
                         ["project-starter", "researcher__primary",
                          "researcher__adversarial"])

    def test_bare_string_is_sugar_for_the_one_key_table(self):
        template = load_legoset(self.write('members = ["refactorer", "golem"]'))
        self.assertEqual([m.id for m in template.members], ["refactorer", "golem"])

    def test_mixed_forms_are_allowed(self):
        template = load_legoset(self.write(
            'members = ["refactorer", { agent = "researcher", role = "primary" }]'))
        self.assertEqual([m.id for m in template.members],
                         ["refactorer", "researcher__primary"])

    def test_order_is_preserved_because_it_is_window_order(self):
        # A template author putting the lead first should get the lead first.
        template = load_legoset(self.write('members = ["c", "a", "b"]'))
        self.assertEqual([m.agent for m in template.members], ["c", "a", "b"])

    def test_name_comes_from_the_file_stem(self):
        self.assertEqual(load_legoset(
            self.write('members = ["golem"]', "devteam.legoset")).name, "devteam")

    def test_agents_view_deduplicates(self):
        template = load_legoset(self.write("""
members = [{ agent = "researcher", role = "a" }, { agent = "researcher", role = "b" }]
"""))
        self.assertEqual(template.agents, frozenset({"researcher"}))


class TestRejections(LegosetTmp):
    def test_missing_file_is_an_error_not_an_empty_default(self):
        # Unlike a .lego (where absent == all-defaults), a cluster with no
        # members is nothing at all.
        with self.assertRaises(ClusterError):
            load_legoset(self.dir / "nope.legoset")

    def test_empty_members_list_rejected(self):
        with self.assertRaises(ClusterError):
            load_legoset(self.write("members = []"))

    def test_missing_members_key_rejected(self):
        with self.assertRaises(ClusterError):
            load_legoset(self.write('name = "team"'))

    def test_duplicate_ids_rejected_with_both_positions(self):
        # Two members that would write the same directory. The message must name
        # the collision, since the fix is "give one a role".
        with self.assertRaises(ClusterError) as caught:
            load_legoset(self.write('members = ["researcher", "researcher"]'))
        self.assertIn("researcher", str(caught.exception))
        self.assertIn("role", str(caught.exception))

    def test_same_agent_twice_is_fine_when_roles_differ(self):
        # The whole point of roles — this must NOT be rejected.
        template = load_legoset(self.write("""
members = [{ agent = "researcher", role = "a" }, { agent = "researcher", role = "b" }]
"""))
        self.assertEqual(len(template.members), 2)

    def test_unknown_key_rejected_rather_than_ignored(self):
        # Silently dropping `tags = [...]` would leave an author believing tags
        # can be set here; they come from the agent's own .lego.
        with self.assertRaises(ClusterError) as caught:
            load_legoset(self.write(
                'members = [{ agent = "golem", tags = ["code"] }]'))
        self.assertIn("tags", str(caught.exception))

    def test_non_string_agent_rejected(self):
        with self.assertRaises(ClusterError):
            load_legoset(self.write("members = [{ agent = 7 }]"))

    def test_wrong_entry_type_rejected(self):
        with self.assertRaises(ClusterError):
            load_legoset(self.write("members = [7]"))

    def test_illegal_role_rejected_by_the_member_guard(self):
        with self.assertRaises(ClusterError):
            load_legoset(self.write(
                'members = [{ agent = "researcher", role = "a:b" }]'))


class TestValidateAgainstKnownAgents(LegosetTmp):
    def test_unknown_agent_named_in_the_message(self):
        template = load_legoset(self.write('members = ["nope", "golem"]'))
        with self.assertRaises(ClusterError) as caught:
            validate(template, frozenset({"golem"}))
        self.assertIn("nope", str(caught.exception))

    def test_all_known_passes(self):
        template = load_legoset(self.write('members = ["golem", "refactorer"]'))
        validate(template, frozenset({"golem", "refactorer", "other"}))


class TestInstantiate(LegosetTmp):
    """Parsing yields EMPTY builds (a template names agents, not tags);
    `instantiate` is what gives each member its agent's own `.lego` defaults.
    Regression: without this step a `refactorer` member arrived with no `[code]`
    and no `<-gpush>` — a bare base image wearing the name."""

    def test_parsing_alone_leaves_builds_empty(self):
        template = load_legoset(self.write('members = ["refactorer"]'))
        self.assertEqual(template.members[0].build, AgentBuild())

    def test_instantiate_loads_each_agents_lego(self):
        (self.dir / "refactorer.lego").write_text(
            'engine = "thinker"\nprofessions = ["code"]\npolicies = ["vcs-safe"]')
        template = load_legoset(self.write('members = ["refactorer"]'))
        member = instantiate(template, self.dir)[0]
        self.assertEqual(member.build.engine, "thinker")
        self.assertEqual(member.build.professions, ("code",))
        self.assertEqual(member.build.policies, ("vcs-safe",))

    def test_two_members_of_one_agent_each_get_the_same_defaults(self):
        (self.dir / "researcher.lego").write_text('engine = "researcher"')
        template = load_legoset(self.write("""
members = [{ agent = "researcher", role = "a" }, { agent = "researcher", role = "b" }]
"""))
        members = instantiate(template, self.dir)
        self.assertEqual([m.role for m in members], ["a", "b"])
        self.assertEqual({m.build.engine for m in members}, {"researcher"})

    def test_an_agent_with_no_lego_is_an_all_defaults_build(self):
        # Legal for instances, so legal for members: absent .lego == empty build.
        template = load_legoset(self.write('members = ["golem"]'))
        self.assertEqual(instantiate(template, self.dir)[0].build, AgentBuild())

    def test_roles_and_ids_survive_instantiation(self):
        template = load_legoset(self.write(
            'members = [{ agent = "researcher", role = "primary" }]'))
        self.assertEqual(instantiate(template, self.dir)[0].id, "researcher__primary")


class TestAutoRoles(unittest.TestCase):
    """`auto_roles` — how the creation form's picks become unique member ids
    WITHOUT any mid-flow editing (decided: role fields during setup are noise;
    members are edited later, from the picker). The rules here ARE that
    decision, so each has its own pin."""

    def test_a_single_unroled_pick_stays_bare(self):
        # The common one-of-each cluster keeps clean ids.
        self.assertEqual(auto_roles([("golem", None)]), [("golem", "golem")])

    def test_duplicates_are_both_numbered_not_first_come_bare(self):
        # A bare `golem` beside `golem__2` would read as the senior of the
        # two, which a duplicate is not — so the moment an agent is picked
        # twice, EVERY unroled entry of it gets a number.
        self.assertEqual(auto_roles([("golem", None), ("golem", None)]),
                         [("golem", "1"), ("golem", "2")])

    def test_template_roles_are_kept_verbatim(self):
        picks = [("researcher", "primary"), ("researcher", "adversarial")]
        self.assertEqual(auto_roles(picks), picks)

    def test_an_added_duplicate_beside_template_roles_gets_a_number(self):
        # devteam's case: two named researchers ship; the user picks a third.
        self.assertEqual(
            auto_roles([("researcher", "primary"), ("researcher", "adversarial"),
                        ("researcher", None)]),
            [("researcher", "primary"), ("researcher", "adversarial"),
             ("researcher", "1")])

    def test_numbering_skips_a_number_an_explicit_role_claims(self):
        # A template role that happens to BE "1" must not collide with the
        # auto-numbering — the generated role steps over it.
        self.assertEqual(
            auto_roles([("golem", "1"), ("golem", None), ("golem", None)]),
            [("golem", "1"), ("golem", "2"), ("golem", "3")])

    def test_order_is_preserved_because_it_is_window_order(self):
        picks = [("b", None), ("a", None), ("b", None)]
        self.assertEqual([agent for agent, _ in auto_roles(picks)],
                         ["b", "a", "b"])

    def test_every_produced_id_is_unique(self):
        # The property from_template enforces — auto_roles must never hand it
        # a collision, whatever mix of template roles and picks arrives.
        picks = [("r", "primary"), ("r", None), ("r", None), ("r", "1"),
                 ("golem", None), ("s", None)]
        ids = [member_id(agent, role) for agent, role in auto_roles(picks)]
        self.assertEqual(len(ids), len(set(ids)))


class TestAssemble(LegosetTmp):
    """`assemble` — form picks to members, the form-side sibling of
    `instantiate`: same .lego inheritance, roles from auto_roles."""

    def test_members_carry_their_agents_lego_defaults(self):
        (self.dir / "refactorer.lego").write_text(
            'engine = "thinker"\nprofessions = ["code"]')
        (member,) = assemble([("refactorer", None)], self.dir)
        self.assertEqual(member.id, "refactorer")
        self.assertEqual(member.build.engine, "thinker")
        self.assertEqual(member.build.professions, ("code",))

    def test_duplicates_share_one_lego_read_and_differ_only_in_role(self):
        (self.dir / "researcher.lego").write_text('engine = "researcher"')
        members = assemble([("researcher", None), ("researcher", None)], self.dir)
        self.assertEqual([m.id for m in members],
                         ["researcher__1", "researcher__2"])
        self.assertEqual(members[0].build, members[1].build)

    def test_assembled_picks_survive_from_template(self):
        # End to end: whatever the form confirms must be creatable — the
        # duplicate-id refusal in from_template can never fire on form output.
        from launch.cluster import state
        members = assemble([("golem", None), ("golem", None),
                            ("researcher", "primary"), ("researcher", None)],
                           self.dir)
        with TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(paths, "AGENTS_STATE", Path(tmp)):
            cluster = state.from_template("form", Path("/tmp/p"), members)
            self.assertEqual(cluster.ids, ("golem__1", "golem__2",
                                           "researcher__primary", "researcher__1"))


class TestDiscovery(LegosetTmp):
    def test_finds_legosets_by_stem(self):
        self.write('members = ["golem"]', "devteam.legoset")
        self.write('members = ["golem"]', "philosophers.legoset")
        (self.dir / "golem.lego").write_text('engine = "golem"')     # not a legoset
        self.assertEqual(sorted(discover_templates(self.dir)),
                         ["devteam", "philosophers"])

    def test_empty_dir_yields_nothing(self):
        self.assertEqual(discover_templates(self.dir), {})


class TestShippedDevteam(unittest.TestCase):
    """The real template that ships — parsed against the real agents dir, so a
    renamed agent breaks here rather than at cluster-create time."""

    def test_devteam_parses_and_names_real_agents(self):
        from launch.file_access import agent_md_index
        template = load_legoset(paths.AGENTS_DIR / "devteam.legoset")
        validate(template, frozenset(agent_md_index()))
        self.assertEqual(
            [m.id for m in template.members],
            ["project-starter", "refactorer", "researcher__primary",
             "researcher__adversarial", "bug-investigator"])

    def test_devteam_members_inherit_their_real_lego_defaults(self):
        # End-to-end against the shipped tree: the refactorer member must arrive
        # with what agents/refactorer.lego declares.
        template = load_legoset(paths.AGENTS_DIR / "devteam.legoset")
        members = {m.id: m for m in instantiate(template, paths.AGENTS_DIR)}
        self.assertEqual(members["refactorer"].build.professions, ("code",))
        self.assertEqual(members["refactorer"].build.policies, ("vcs-safe",))
        self.assertEqual(members["researcher__primary"].build.engine, "researcher")
        # Both researchers get identical defaults; only the role differs.
        self.assertEqual(members["researcher__primary"].build,
                         members["researcher__adversarial"].build)

    def test_devteam_is_discoverable(self):
        self.assertIn("devteam", discover_templates(paths.AGENTS_DIR))


if __name__ == "__main__":
    unittest.main()

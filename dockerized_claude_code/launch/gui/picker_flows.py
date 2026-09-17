"""What each picker KEY does once a row is chosen — the create / edit /
destroy flows (launch/gui).

Split out of menu_picker 2026-09-03: the widget's job is to show rows and
report a (key, row) pair; deciding what F2 on a cluster row MEANS — two
forms, a re-derivation of every member's own tags, a rename that moves a
directory — is a separate concern that had grown to a third of that module.

  _edit_member_flow / _remove_member_flow      F2 / Del on a member row
  _destroy_cluster_flow                        Del on a cluster row
  _create_cluster_flow / _edit_cluster_flow    Enter on a template row / F2
                                               on a cluster row: the
                                               cluster-tag form, then the
                                               name+project+membership form
  _prompt_cluster_tags                         step one of both, with
                                               {mux}/{clstr} locked

Every flow takes PRIMITIVES (a session name, a member id, a template path),
never a picker row object — which is what lets this module sit below the
widget: `picker_prompts` → `picker_flows` → `menu_picker`, one way.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..cluster import state as cluster_state
from ..cluster.legoset import assemble, instantiate, load_legoset, prefill_picks, reassemble
from ..file_access import expand_user_path
from ..paths import AGENTS_DIR, DEFAULT_WORKSPACE
from ..tags import AgentBuild, Registry
from .cluster_form import prompt_members
from .forms import prompt_cluster_tags, prompt_tags
from .picker_prompts import (
    _agent_rows, _cluster_fields, _report_to_picker, confirm_dialog,
)


def _edit_member_flow(registry: Registry, session: str, member_id: str) -> None:
    """F2 on a member: the ordinary tag form, persisted into cluster.toml.

    Reloads the cluster (rows may be stale — see _ClusterRow) and shows the
    CLUSTER's own tags locked-and-checked: they apply to this member and it
    may not opt out, so the form states them rather than hiding them (the
    member sees its real build) while `Cluster.with_build` subtracts them
    again on save, so nothing is stored twice."""
    cluster = cluster_state.load(session)
    if cluster is None or (member := cluster.member(member_id)) is None:
        _report_to_picker(
            f"  Cluster '{session}' changed on disk — no member '{member_id}'.")
        return
    shared = frozenset({*cluster.tags.professions, *cluster.tags.specialties,
                        *cluster.tags.policies})
    new_build = prompt_tags(registry, cluster.member_build(member),
                            instance=f"{member_id}  (cluster: {session})", scope="member",
                            workspace=str(cluster.project),
                            locked=shared)
    if new_build is None:
        return
    assert isinstance(new_build, AgentBuild)   # fieldless call — narrow the union
    cluster_state.save(cluster.with_build(member_id, new_build))


def _remove_member_flow(session: str, member_id: str) -> None:
    """Del on a member: shrink the cluster by one, guarding the last member —
    an empty cluster is unrepresentable (Cluster refuses it), and the honest
    gesture for 'remove the only member' is destroying the cluster."""
    cluster = cluster_state.load(session)
    if cluster is None or cluster.member(member_id) is None:
        return
    if len(cluster.members) == 1:
        _report_to_picker(
            f"  '{member_id}' is the only member — a cluster cannot be "
            f"empty.\n  Del on the cluster row destroys '{session}' instead.")
        return
    if confirm_dialog(f"Remove member '{member_id}' from cluster '{session}'?"):
        cluster_state.save(cluster.without_member(member_id))


def _destroy_cluster_flow(session: str) -> None:
    """Del on a cluster row: the shared teardown (`state.destroy` — worktrees
    removed, branches kept, state dir deleted), behind a confirmation naming
    everything it takes with it."""
    cluster = cluster_state.load(session)
    if cluster is None:
        return
    if confirm_dialog(f"Destroy cluster '{session}' and its "
                      f"{len(cluster.members)} member(s)? (branches are kept)"):
        cluster_state.destroy(cluster)


def _prompt_cluster_tags(registry: Registry, session: str, *,
                         current: AgentBuild | None = None,
                         member_tags: frozenset[str] = frozenset()) -> AgentBuild | None:
    """Step one of both cluster flows: the cluster-wide tag form, with the
    locked pair pre-ticked and inert, its combo warnings judged over the
    cluster's set plus `member_tags` (what the members carry as their own).
    None on Esc — the caller aborts."""
    return prompt_cluster_tags(
        registry, current or AgentBuild(
            specialties=cluster_state.LOCKED_SPECIALTIES),
        session=session,
        locked=frozenset(cluster_state.LOCKED_SPECIALTIES),
        member_tags=member_tags)


def _refused(registry: Registry, cluster: cluster_state.Cluster) -> bool:
    """Report and refuse a cluster that carries a tag at a scope the tag
    forbids (`cluster_state.forbidden_tags`) — the same rule the launch
    would apply, met at creation instead: a member whose `.lego` defaults
    include `{frwl}` cannot join, and the message says that, not that its
    `.lego` is wrong."""
    problems = cluster_state.forbidden_tags(cluster, registry)
    if not problems:
        return False
    _report_to_picker("  Not saved — this cluster cannot launch:\n" + "\n".join(f"    {line}" for line in problems))
    return True


def _create_cluster_flow(registry: Registry, template_path: Path) -> None:
    """The whole creation flow for one template — ONE form: the cluster's
    name and project path as text fields above the agent list, membership by
    picking. Returns to the picker whatever happens; Enter on the created
    cluster's own row is what launches it."""
    template = load_legoset(template_path)   # row-build already validated it
    # STEP 1 — the cluster's own tags, forced on every member. First, because
    # it is the decision that shapes the whole cluster (operator request,
    # 2026-09-02); Esc here cancels the creation entirely, nothing written.
    # The template's members' own `.lego` tags feed the combo warnings, so a
    # cluster-wide {dood} over a template whose agent carries {auto} warns.
    template_members = instantiate(template, AGENTS_DIR)
    tags = _prompt_cluster_tags(registry, template.name, member_tags=frozenset(
        n for m in template_members for n in (*m.build.professions, *m.build.specialties, *m.build.policies)))
    if tags is None:
        return
    # STEP 2 — the existing form: name + project + membership.
    result = prompt_members(
        _agent_rows(registry), prefill_picks(template),
        title="New cluster  (type into the fields; Space adds an agent):",
        fields=_cluster_fields(DEFAULT_WORKSPACE, template.name,
                               derive=template.name))
    if result is None:
        return
    values, picks = result
    project = expand_user_path(values["project"])
    cluster = cluster_state.from_template(
        values["session"], Path(project), assemble(picks, AGENTS_DIR),
        template=template.name, tags=tags)
    if _refused(registry, cluster):
        return
    cluster_state.save(cluster)
    # No summary, no Enter-pause: the picker redraws with the new cluster row
    # (and its member rows) — that IS the confirmation, same as editing.


def _edit_cluster_flow(registry: Registry, session: str) -> None:
    """F2 on a cluster row: the SAME form as creation, prefilled — rename in
    the name field (the whole cluster directory moves), repoint the project,
    grow or shrink the membership. Surviving members keep their CURRENT
    builds (a rename must not wipe per-member tag edits back to `.lego`
    defaults — `legoset.reassemble` is that guarantee); added ones start from
    their agent's `.lego` plus the forced tags, exactly as creation would."""
    cluster = cluster_state.load(session)
    if cluster is None:
        _report_to_picker(f"  Cluster '{session}' is gone from disk.")
        return
    # STEP 1 — the cluster-wide tags, prefilled from what it carries now, the
    # combo warnings judged with the members' own tags (on disk).
    tags = _prompt_cluster_tags(registry, session, current=cluster.tags,
                                member_tags=cluster.members_own_tags())
    if tags is None:
        return
    prefill = [(m.agent, None if m.role == m.agent else m.role)
               for m in cluster_state.picker_order(cluster.members, registry)]
    result = prompt_members(
        _agent_rows(registry), prefill,
        title=f"Edit cluster '{session}'  (fields + membership):",
        fields=_cluster_fields(str(cluster.project), cluster.session,
                               current=cluster.session))
    if result is None:
        return
    values, picks = result
    updated = dataclasses.replace(
        cluster, project=Path(expand_user_path(values["project"])),
        members=reassemble(cluster.members, picks, AGENTS_DIR), tags=tags)
    # Re-derive each member's OWN build against the (possibly changed) cluster
    # set: a tag promoted to cluster-wide must stop being stored per member,
    # and one demoted from it stays only where it was already ticked.
    updated = dataclasses.replace(updated, members=tuple(
        dataclasses.replace(m, build=updated.own_build(m.build))
        for m in updated.members))
    if _refused(registry, updated):
        return
    if values["session"] != cluster.session:
        cluster_state.rename(updated, values["session"])
    else:
        cluster_state.save(updated)
    # No summary, no Enter-pause: the picker redraws with the edited row, and
    # that IS the confirmation (the pause here was reported as noise).


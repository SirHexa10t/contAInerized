"""The concrete forms the launcher asks its questions with (launch/gui).

Split out of tag_form 2026-09-03 — it had grown to hold three roles at once
(the style system, the shared form machinery, and these). What remains here
is only "what do we ask, and how is each row worded":

  prompt_tags            the kind-sectioned instance tag form
  prompt_cluster_tags    its cluster-wide sibling — no engine section, with
                         {mux}/{clstr} locked (a cluster's own tag set)
  edit_profiles_menu     the merged preferences form: one section per
                         profile file (per-profession toolkits + the
                         launcher UI), each saving to its own file
  edit_profiles_form     the section-concatenating middle handler behind it

Every row, warning and confirm rule comes from `form_core`; every colour
from `styles`. Nothing here builds a prompt_toolkit Application itself.
"""

from dataclasses import dataclass, replace
from typing import Callable, overload

from ..paths import toolkit_profile_path, ui_profile_path
from ..tags import (
    is_scope, scope_note,
    AgentBuild, Ai, Engine, Harness, Policy, Profession, Registry, Specialty, Tag, ToolkitEntry,
)
from ..tags.ai import sorted_ais
from ..tags.engine import sorted_engines
from ..tags.harness import sorted_harnesses
from ..tags.toolkit_profile import load_profile, save_profile
from ..tags.ui_profile import load_ui_form, load_ui_profile, save_ui_profile
from .form_core import (
    TITLE_TAGS_FORM, TOOLKIT_SIZE_NOTE, FormOption, TextField, checkbox_form,
)
from .styles import STYLE_UNDERLINE, UiClass, tag_style

def _tag_row(tag: Tag, checked: bool, group: str | None = None, *, note: str = "",
             inherited: bool = False, forbidden: str | None = None) -> FormOption:
    """One selectable form row: colored kind-punctuated label + the tag's
    short description, a dim `(requires: …)` parenthetical when it has
    prerequisites, and the full description as the focused-row body — led by
    the tag's underlined FULLNAME, so the abbreviation in the label is never
    a puzzle (`{dood}` focuses to `Docker-outside-of-Docker: can run …`).
    Keys are the tags' full names (what `.lego` / instances.toml store).

    An ENGINE row carries its capability standard — the engine's own budget
    value, in the launcher's vocabulary rather than any AI's.

    An always-on tag (a static policy like `<-su>`) renders locked: grayed,
    checked, inert to Space, with an `(always-on)` marker — the user sees
    it applies but can't change it (prompt_tags also filters it out of the
    returned build; it's never persisted). `note` is a dim trailer the
    caller adds (a harness row: the AIs it runs).

    Two more locked states, kept apart on purpose (gate tag-scopes): an
    INHERITED tag — the cluster gave it to this member — is locked and
    CHECKED with `(from the cluster)`, so it stays in the checked set and a
    combo warning it completes (cluster `{dood}` + this member's `{auto}`)
    still fires; a tag FORBIDDEN as this build's own (`forbidden` = the
    scope note, e.g. `{dood}` on a member row) is locked and UNCHECKED with
    the note, so it can neither be picked nor mistaken for active."""
    always_on = getattr(tag, "always_on", False)
    label: list[tuple[str, str]] = [(tag_style(tag), tag.label), ("", " ")]
    label.append(("", tag.short_description))
    if isinstance(tag, Engine) and tag.budget.effort_tier:
        # The engine's words come from tag.info; beside them sits its
        # CAPABILITY STANDARD — the quarter whose frontier it asks for
        # (`2026Q2`), or one of the two ends (`cheapest` / `best`). That is
        # the engine's own budget value, the same word its tag.budget names,
        # so the row says what was chosen rather than how some AI answers it
        # (operator, 2026-09-17). Which model and effort answer the standard
        # is the AI's business, and shows where that is the point: the F8
        # legend's "Pinned model" column, the preview's Engine fact, the
        # launch banner. An engine whose effective budget names no standard
        # adds nothing here.
        label.append((UiClass.STATUS.css, f"  {tag.budget.effort_tier}"))
    if note:
        label.append((UiClass.STATUS.css, f"  {note}"))
    if always_on:
        label.append((UiClass.STATUS.css, "  (always-on)"))
    if inherited:
        label.append((UiClass.STATUS.css, "  (from the cluster)"))
    elif forbidden:
        label.append((UiClass.STATUS.css, f"  ({forbidden})"))
    if tag.requires:
        label.append((UiClass.STATUS.css, f"  (requires: {', '.join(sorted(tag.requires))})"))
    return FormOption(
        key=tag.name,
        label=label,
        body=[(STYLE_UNDERLINE, tag.fullname), ("", f": {tag.full_description}")],
        checked=False if (forbidden and not inherited) else True if (always_on or inherited) else checked,
        group=group,
        locked=bool(always_on or inherited or forbidden),
    )


def _tag_form_options(registry: Registry, current: AgentBuild, *, scope: str,
                      engines: bool = True,
                      locked: frozenset[str] = frozenset(),
                      ) -> list[FormOption]:
    """The full sectioned form: one header per kind (its nutshell), the AI
    as a radio group at the very top (pre-dotted from `current.ai`, else the
    tree's default member), the harness as a radio group under it (pre-dotted
    from `current.harness`, else the AI's default), engines as a radio group beneath them (pre-dotted from `current.engine` — the
    caller passes the RESOLVED engine, so the dot shows what would actually
    run), then professions / specialties / policies as checkboxes pre-checked
    from `current`'s axis lists. Policies are ordered by shortname WITH its
    leading symbol (`!` < `+` < `-` in ASCII), so same-stance policies sit
    together: demands, then grants, then denials.

    `scope` is where the build being edited lives (`tags.SCOPES`: solo /
    cluster / member): a tag whose `forbid_on` names it renders locked,
    unchecked and grey with its `scope_note` — `{dood}` on a member row says
    "cluster-wide only: F2 on the cluster row" — so the form itself shows
    what a build cannot carry (operator request, 2026-09-16). `engines=False`
    drops the AI, harness and engine sections — the CLUSTER-level form, where
    per-member choices (which AI, which CLI, how hard it thinks) have no
    meaning. `locked` names tags that render checked-and-inert (the treatment
    an `always_on` policy gets): the cluster form locks {mux}/{clstr}, and a
    MEMBER's form locks whatever its cluster already imposes, marked `(from
    the cluster)`, so a member can see what applies to it without being able
    to opt out — and an inherited tag stays CHECKED even where a member could
    not add it itself, so the combo warnings it completes keep firing."""
    is_scope(scope)
    checked = {*current.professions, *current.specialties, *current.policies}

    def header(kind_cls: type[Tag]) -> FormOption:
        return FormOption(
            key=f"#{kind_cls.root}",
            label=[(UiClass.TITLE.css, f"{kind_cls.root.upper()} — {kind_cls.nutshell}")],
            header=True,
        )

    out: list[FormOption] = []
    if engines:
        # The AI leads: it decides which model each engine's standard below means,
        # and like the engine it is per member — the cluster form omits both.
        # Pre-dotted from the build's ai, else the tree's default member.
        effective_ai = registry.ai_for(current)
        if registry.ais:
            out.append(header(Ai))
            out += [_tag_row(tag, checked=(effective_ai is not None and tag.name == effective_ai.name), group="ai")
                    for tag in sorted_ais(registry.ais.values())]
        # Then the harness — the CLI around that AI; each row names the AIs
        # it runs, and the warning zone says when the dotted pair cannot work
        # (prompt_tags then falls back to the AI's own harness).
        effective_harness = registry.harness_for(current)
        if registry.harnesses:
            out.append(header(Harness))
            default_ai = registry.default_ai
            out += [_tag_row(tag, checked=(effective_harness is not None and tag.name == effective_harness.name), group="harness",
                             note="runs " + " ".join(registry.ais[a].label for a in tag.ais if a in registry.ais))
                    for tag in sorted_harnesses(registry.harnesses.values(), default_ai.name if default_ai else None)]
        out.append(header(Engine))
        out += [_tag_row(tag, checked=(tag.name == current.engine), group="engine")
                for tag in sorted_engines(registry.engines.values())]
    for kind_cls, members in ((Profession, list(registry.professions.values())),
                              (Specialty, list(registry.specialties.values())),
                              (Policy, sorted(registry.policies.values(),
                                              key=lambda p: p.shortname))):
        out.append(header(kind_cls))
        for tag in members:
            inherited = tag.name in locked
            forbidden = scope_note(tag, scope) if scope in tag.forbid_on else None
            out.append(_tag_row(tag, checked=tag.name in checked, inherited=inherited, forbidden=forbidden))
    return out


def prompt_cluster_tags(registry: Registry, current: AgentBuild, *,
                        session: str, locked: frozenset[str],
                        member_tags: frozenset[str] = frozenset(),
                        ) -> "AgentBuild | None":
    """The CLUSTER-level tag form — step one of creating or editing a cluster
    (operator request, 2026-09-02: set `{cc}` once for the cluster instead of
    once per member). Returns the cluster's tag set, or None on Esc.

    Three differences from the instance form, all deliberate: no AI, HARNESS
    or ENGINE section (which AI runs, in which CLI, how hard it thinks — all per member), `locked` rows for the tags
    that make a cluster a cluster ({mux}/{clstr} — checked and inert, the
    `always_on` treatment), and a preamble that says plainly what the
    selection does, because "these tags are forced on every member" is not
    something a tag list can imply on its own.

    `member_tags` are the names the members carry as their OWN (on disk for
    an edit; the template's `.lego` defaults for a creation): a combo warning
    is judged over the cluster's set UNION those, so ticking `{dood}`
    cluster-wide on a cluster whose members already carry `{auto}` warns at
    the moment of the decision — the moment cluster-wide `{dood}` makes that
    combination reachable for every member (strict-reviewer, gate tag-scopes)."""
    options = _tag_form_options(registry, current, scope="cluster", engines=False, locked=locked)
    result = checkbox_form(
        f"Cluster tags for '{session}'  (Space to toggle):", options,
        warnings=_warnings_given(_combo_warnings(registry), member_tags),
        requires=_form_requires(registry),
        wants=_form_wants(registry),
        labels=_form_labels(registry),
        preamble=["# EVERY member of this cluster is FORCED to carry the tags",
                  "# selected here — and only these are set cluster-wide.",
                  "# Per-member tags (and each member's engine) stay on the",
                  "# member rows: pick a member in the picker and press F2.",
                  f"# {', '.join(sorted(locked))} cannot be unticked: they are",
                  "# what makes this a cluster."])
    if result is None:
        return None
    picked = set(result.checked)
    # No engine axis, and always-on policies stay out of the build exactly as
    # in prompt_tags (they apply unconditionally and are never persisted).
    return AgentBuild(
        engine=None,
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on))


def _warnings_given(warnings: dict[frozenset[str], tuple[str, list[str]]],
                    already: frozenset[str]) -> dict[frozenset[str], tuple[str, list[str]]]:
    """`warnings` re-keyed for a form whose checked set is completed by tags
    checked ELSEWHERE (`already` — the members' own tags, for the cluster
    form): a combo minus those fires when this form ticks the rest. A combo
    `already` covers entirely is not this form's to warn about, and one it
    touches not at all is kept as is."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for combo, entry in warnings.items():
        remainder = combo - already
        if remainder:
            out[remainder] = entry
    return out


def _combo_warnings(registry: Registry) -> dict[frozenset[str], tuple[str, list[str]]]:
    """specialty/combos.info entries re-shaped for checkbox_form's warning
    zone: {tag-name set: (first message line, remaining lines)}."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for combo in registry.combos:
        first, *rest = combo.message.splitlines() or [""]
        out[combo.tags] = (first, rest)
    return out


def _harness_warnings(registry: Registry) -> dict[frozenset[str], tuple[str, list[str]]]:
    """A warning for every AI × harness pair that cannot run together, in the
    combo-warning shape, so the form's red zone lights up the moment both are
    dotted — and says what the launch will do instead (prompt_tags falls back
    to the AI's own harness)."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for ai in registry.ais.values():
        own = registry.harnesses.get(ai.harness)
        for harness in registry.harnesses.values():
            if not harness.runs(ai.name):
                out[frozenset({ai.name, harness.name})] = (
                    f"{harness.label} cannot run {ai.label} — it runs "
                    f"{' '.join(registry.ais[a].label for a in harness.ais if a in registry.ais)}.",
                    [f"The launch will use {ai.label}'s own harness, {own.label}, instead." if own else ""])
    return out


def _pairing_warnings(registry: Registry) -> dict[frozenset[str], tuple[str, list[str]]]:
    """A warning for every AI × harness pair the harness CAN run but that
    carries something a user should know before picking it. ONE entry per
    pair — the form's warning map is keyed by the pair, so a second entry
    would silently replace the first — carrying whichever of two independent
    claims apply:

    - a REPORT (`Ai.foreign_harness_report`): what running this AI outside its
      own CLI has been observed to cost, QUOTED AS WRITTEN. It LEADS, because
      a number an operator has been burned by is the loudest thing that can be
      said at pick time, and the tree's own sentence carries the hedge and the
      date that keep field evidence from reading like a vendor's published
      term (the scan refuses an unhedged one).
    - ELIGIBILITY (`Ai.plan_harnesses`): the vendor's plan is spendable only
      in the clients it allows; anywhere else the pairing runs on an API key.

    A header and at most two short lines, because a warning nobody finishes
    reading warns nobody (operator, 2026-09-17), and every line stands on its
    own: the header names the pair and leads with whichever claim applies,
    each body line states one fact, and nothing is reordered or cross-
    referenced. The four words "at the usual rate" carry what a paragraph used
    to — no vendor prices by client, so a header's number is not a surcharge.

    NO pointer to a plans/ file: that tree is the maintainers' record, not
    documentation for whoever is picking tags (operator, 2026-09-17). The
    warning carries its own provenance instead — who observed it and when —
    and README's harness section is where a user reads more. Both claim sets
    are per-AI data, because vendors and reports differ per vendor; an AI with
    neither warns about nothing, and silence is the honest default.

    Deliberately NOT conditional on the credentials on this host: an operator
    may hold both a plan and a key, the form is a pure function of the
    registry, and the sentences are phrased so they are true either way."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for ai in registry.ais.values():
        allowed = " ".join(registry.harnesses[name].label for name in ai.plan_harnesses
                           if name in registry.harnesses)
        own = registry.harnesses.get(ai.harness)
        for harness in registry.harnesses.values():
            if not harness.runs(ai.name):
                continue
            reported = ai.foreign_harness_report if (own is not None and harness.name != own.name) else ""
            outside_plan = bool(allowed) and harness.name not in ai.plan_harnesses
            if not reported and not outside_plan:
                continue
            # The header names the pair and leads with whichever claim is
            # loudest; each body line then stands alone, so no line depends on
            # another having been shown and none needs reordering.
            # The report is quoted as written: it carries its own hedge and
            # date (the scan insists), so wrapping it in a second "REPORTED /
            # Unverified" only said the same thing twice.
            header = f"{harness.label} + {ai.label} — " + (f"{reported}." if reported
                                                           else f"outside {ai.vendor}'s plan.")
            body: list[str] = []
            if reported:
                body.append("Suspect the harness's prompt-cache reuse, not the model.")
            if outside_plan:
                # "at the usual rate" is the anti-folklore clause in four
                # words: no vendor prices by client (checked across all four,
                # 2026-09-17), so a header's number cannot read as a
                # surcharge. The floor rides as a parenthetical, not a line.
                floor = f" ({ai.key_free_tier})" if ai.key_free_tier else ""
                body.append(f"{ai.vendor}'s plan runs only in {allowed}; here it's "
                            f"{ai.key_env}, per token at the usual rate{floor}.")
            out[frozenset({ai.name, harness.name})] = (header, body)
    return out


def _form_requires(registry: Registry) -> dict[str, frozenset[str]]:
    """{tag name: prerequisite tag names} across the three form kinds — the
    shape checkbox_form's check-cascade consumes. Tags without prerequisites
    are omitted (requires_closure treats absent keys as empty)."""
    return {
        tag.name: frozenset(tag.requires)
        for tag in (*registry.professions.values(), *registry.specialties.values(),
                    *registry.policies.values())
        if tag.requires
    }


def _form_wants(registry: Registry) -> dict[str, tuple[tuple[str, str], ...]]:
    """{tag name: its (wanted, message) requests} across the three form kinds
    — the shape checkbox_form's wants zone consumes. Tags without wants are
    omitted."""
    return {
        tag.name: tag.wants
        for tag in (*registry.professions.values(), *registry.specialties.values(),
                    *registry.policies.values())
        if tag.wants
    }


def _form_labels(registry: Registry) -> dict[str, str]:
    """{tag name: punctuated label} for EVERY tag, every kind. The wants
    zone displays through this map so its header shows what the rows show —
    `'{cowork}' wants '<+bash>'` — rather than bare manifest names the user
    would have to translate. Every kind, not just the form's three: a want may
    point at any real tag."""
    return {tag.name: tag.label for tag in registry.get_all()}


@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, scope: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: None = None) -> "AgentBuild | None": ...
@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, scope: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: list[TextField],
                ) -> "tuple[dict[str, str], AgentBuild] | None": ...
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, scope: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: list[TextField] | None = None,
                ) -> "AgentBuild | tuple[dict[str, str], AgentBuild] | None":
    """Run the tag form (see the module docstring for the full behavior) and
    return the selection as a new AgentBuild — as `(field values, build)` when
    `fields` were given — or None when the user cancels (Esc); callers abort
    their create / modify flow on None rather than persisting anything.

    Two shapes, one form: WITHOUT fields, `instance` + `workspace` are
    already-answered prompts echoed as the preamble (the member-tag-edit call
    site). WITH fields, the workspace/name ARE the fields — no terminal
    prompt precedes the form — and the preamble only names the agent.
    `scope` says where the build lives (solo / member) — the rows a tag's
    `forbid_on` keeps out of it render greyed and come back unpicked."""
    options = _tag_form_options(registry, current, scope=scope, locked=locked)
    preamble = ([f"# agent:  {instance}"] if fields is not None
                else [f"# instance:  {instance}",
                      f"# workspace: {workspace}"])
    result = checkbox_form(TITLE_TAGS_FORM, options,
                           warnings={**_combo_warnings(registry), **_harness_warnings(registry),
                                     **_pairing_warnings(registry)},
                           requires=_form_requires(registry),
                           wants=_form_wants(registry),
                           labels=_form_labels(registry),
                           preamble=preamble,
                           fields=fields)
    if result is None:
        return None
    values, keys = result.field_values, result.checked
    picked = set(keys)
    # Always-on (static) tags come back checked — they're locked rows — but
    # are never part of the build: applied unconditionally, never persisted.
    build = AgentBuild(
        ai=next((n for n in registry.ais if n in picked), current.ai),
        harness=next((n for n in registry.harnesses if n in picked), current.harness),
        engine=next((n for n in registry.engines if n in picked), current.engine),
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on),
    )
    # A harness that cannot run the picked AI is not stored: the instance
    # falls back to the AI's own harness (the form's warning zone said so).
    ai, harness = registry.ai_for(build), registry.harness_for(build)
    if build.harness and ai is not None and harness is not None and not harness.runs(ai.name):
        build = replace(build, harness=None)
    return build if fields is None else (values, build)

def _toolkit_size_text(entry: ToolkitEntry) -> str:
    """The size column for a toolkit row: `~NNNMb` for an install, `included`
    for a locked entry (in the base image, no added footprint)."""
    return "included" if entry.locked else f"~{entry.approx_size_mb}MB"


def _toolkit_form_options(entries: dict[str, ToolkitEntry], profile: dict[str, bool]) -> list[FormOption]:
    """One `FormOption` per `template.form` entry, key-sorted. Toggleable rows
    are checked from the current profile (a key the profile doesn't mention
    yet — a tool added to the manifest after the profile was written — falls
    back to the entry's own `default`, matching `load_profile`'s
    reconciliation); locked rows show their fixed `default` state, grayed and
    un-toggleable. Key and size columns are padded so the `—` separators
    align down the form. The focused row's body panel carries the flavor:
    how you run the tool + what kind of language it is. Callers guard
    non-empty `entries`."""
    sizes = {key: _toolkit_size_text(entry) for key, entry in entries.items()}
    key_width = max(len(key) for key in entries)
    size_width = max(len(size) for size in sizes.values())
    out: list[FormOption] = []
    for key, entry in sorted(entries.items()):
        checked = entry.default if entry.locked else profile.get(key, entry.default)
        out.append(FormOption(
            key=key,
            label=[("", f"{key:<{key_width}} — {sizes[key]:<{size_width}} — {entry.description}")],
            body=[("", "run with "), (STYLE_UNDERLINE, entry.run_command),
                  ("", f"   ·   {entry.language}")],
            checked=checked,
            locked=entry.locked,
        ))
    return out


@dataclass(frozen=True)
class ProfileSection:
    """One profile-backed slice of the merged preferences form: its
    comment-title, its rows, and where its checked set lands on confirm —
    each section saves to its OWN file, which is what lets the one form front
    any number of them."""
    title: str
    options: list[FormOption]
    save: Callable[[set[str]], None]


def _toolkit_section(profession: Profession) -> ProfileSection | None:
    """The toolkit slice for one configurable profession — None without a
    template.form. Saves to that profession's own profile; only the
    toggleable rows' states persist (locked rows, e.g. Python, carry no
    toggle)."""
    entries = profession.load_toolkit()
    if not entries:
        return None
    path = toolkit_profile_path(profession.name)
    current = load_profile(path, entries)   # toggleable keys only
    return ProfileSection(
        title=f"Edit {profession.label} toolkit  (Space to toggle):",
        options=_toolkit_form_options(entries, current),
        save=lambda checked: save_profile(
            path, {key: key in checked for key in current}, entries),
    )


def _ui_section() -> ProfileSection:
    """The launcher-UI slice — `settings/ui.form` rendered over
    `ui_profile.toml`. ALWAYS present, unlike the toolkit slices: its
    preferences (the muxer backend) are profession-independent, which is why
    the picker row that opens this form no longer hides when no profession is
    configurable."""
    entries = load_ui_form()
    path = ui_profile_path()
    current = load_ui_profile(path, entries)
    return ProfileSection(
        title="Edit UI configs  (Space to toggle):",
        options=[FormOption(key=key,
                            label=[("", entry.description)],
                            body=[("", entry.body)],
                            checked=current.get(key, entry.default))
                 for key, entry in sorted(entries.items())],
        save=lambda checked: save_ui_profile(
            path, {key: key in checked for key in current}, entries),
    )


def edit_profiles_form(sections: list[ProfileSection],
                       preamble: list[str] | None = None) -> None:
    """The middle-handler the profiles menu rides: concatenate ANY number of
    sections into ONE checkbox form — the first section's title is the form's
    title, every later one becomes a `header=True` row — then fan the
    confirmed set back out, each section saving to its own file. Esc saves
    nothing anywhere. Option keys are namespaced per section internally, so
    two files may reuse a name without colliding (`attached_to` is not
    remapped — no profile row uses it)."""
    merged: list[FormOption] = []
    for index, section in enumerate(sections):
        if index:
            # The next section's title lands three newlines after the
            # previous section's last row (operator's spec): two blank
            # header rows — skipped by navigation like any header — then
            # the title row itself.
            merged += [FormOption(key=f"#gap{index}-{blank}", label="",
                                  header=True)
                       for blank in range(2)]
            merged.append(FormOption(
                key=f"#section{index}",
                label=[(UiClass.TITLE.css, section.title)],
                header=True))
        merged += [replace(option, key=f"{index}:{option.key}")
                   for option in section.options]
    result = checkbox_form(sections[0].title, merged, preamble=preamble)
    if result is None:   # Esc — cancel, no file touched
        return
    checked = set(result.checked)
    for index, section in enumerate(sections):
        section.save({option.key for option in section.options
                      if f"{index}:{option.key}" in checked})


def edit_profiles_menu(registry: Registry) -> None:
    """Open the ONE merged preferences form: a toolkit section per
    configurable profession (today: [code] alone) plus the always-present UI
    section. The size-ballpark preamble only rides along when a toolkit
    section shows sizes."""
    sections = [section for profession in registry.professions.values()
                if (section := _toolkit_section(profession)) is not None]
    preamble = [TOOLKIT_SIZE_NOTE] if sections else None
    sections.append(_ui_section())
    edit_profiles_form(sections, preamble=preamble)

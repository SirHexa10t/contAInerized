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
from typing import Callable, cast, overload

from ..paths import toolkit_profile_path, ui_profile_path
from ..tags import (
    AgentBuild, Engine, Policy, Profession, Registry, Specialty, Tag,
    ToolkitEntry,
)
from ..tags.engine import sorted_engines
from ..tags.toolkit_profile import load_profile, save_profile
from ..tags.ui_profile import load_ui_form, load_ui_profile, save_ui_profile
from .form_core import (
    TITLE_TAGS_FORM, TOOLKIT_SIZE_NOTE, FormOption, TextField, checkbox_form,
)
from .styles import STYLE_UNDERLINE, UiClass, tag_style

def _tag_row(tag: Tag, checked: bool, group: str | None = None) -> FormOption:
    """One selectable form row: colored kind-punctuated label + the tag's
    short description, a dim `(requires: …)` parenthetical when it has
    prerequisites, and the full description as the focused-row body — led by
    the tag's underlined FULLNAME, so the abbreviation in the label is never
    a puzzle (`{dood}` focuses to `Docker-outside-of-Docker: can run …`).
    Keys are the tags' full names (what `.lego` / instances.toml store).

    An always-on tag (a static policy like `<-su>`) renders locked: grayed,
    checked, inert to Space, with an `(always-on)` marker — the user sees
    it applies but can't change it (prompt_tags also filters it out of the
    returned build; it's never persisted)."""
    always_on = getattr(tag, "always_on", False)
    label: list[tuple[str, str]] = [(tag_style(tag), tag.label), ("", " ")]
    label.append(("", tag.short_description))
    if always_on:
        label.append((UiClass.STATUS.css, "  (always-on)"))
    if tag.requires:
        label.append((UiClass.STATUS.css, f"  (requires: {', '.join(sorted(tag.requires))})"))
    return FormOption(
        key=tag.name,
        label=label,
        body=[(STYLE_UNDERLINE, tag.fullname), ("", f": {tag.full_description}")],
        checked=True if always_on else checked,
        group=group,
        locked=always_on,
    )


def _tag_form_options(registry: Registry, current: AgentBuild, *,
                      engines: bool = True,
                      locked: frozenset[str] = frozenset(),
                      ) -> list[FormOption]:
    """The full sectioned form: one header per kind (its nutshell), engines
    as a radio group at the top (pre-dotted from `current.engine` — the
    caller passes the RESOLVED engine, so the dot shows what would actually
    run), then professions / specialties / policies as checkboxes pre-checked
    from `current`'s axis lists. Policies are ordered by shortname WITH its
    leading symbol (`!` < `+` < `-` in ASCII), so same-stance policies sit
    together: demands, then grants, then denials.

    `engines=False` drops that whole section — the CLUSTER-level form, where
    per-member thinking budgets have no meaning. `locked` names tags that
    render checked-and-inert (the treatment an `always_on` policy gets):
    the cluster form locks {mux}/{clstr}, and a MEMBER's form locks whatever
    its cluster already imposes, so a member can see what applies to it
    without being able to opt out."""
    checked = {*current.professions, *current.specialties, *current.policies}

    def header(kind_cls: type[Tag]) -> FormOption:
        return FormOption(
            key=f"#{kind_cls.root}",
            label=[(UiClass.TITLE.css, f"{kind_cls.root.upper()} — {kind_cls.nutshell}")],
            header=True,
        )

    out: list[FormOption] = []
    if engines:
        out.append(header(Engine))
        out += [_tag_row(tag, checked=(tag.name == current.engine), group="engine")
                for tag in sorted_engines(registry.engines.values())]
    for kind_cls, members in ((Profession, list(registry.professions.values())),
                              (Specialty, list(registry.specialties.values())),
                              (Policy, sorted(registry.policies.values(),
                                              key=lambda p: p.shortname))):
        out.append(header(kind_cls))
        for tag in members:
            row = _tag_row(tag, checked=tag.name in checked or tag.name in locked)
            out.append(replace(row, locked=True) if tag.name in locked else row)
    return out


def prompt_cluster_tags(registry: Registry, current: AgentBuild, *,
                        session: str, locked: frozenset[str],
                        ) -> "AgentBuild | None":
    """The CLUSTER-level tag form — step one of creating or editing a cluster
    (operator request, 2026-09-02: set `{cc}` once for the cluster instead of
    once per member). Returns the cluster's tag set, or None on Esc.

    Three differences from the instance form, all deliberate: no ENGINE
    section (a thinking budget is per member), `locked` rows for the tags
    that make a cluster a cluster ({mux}/{clstr} — checked and inert, the
    `always_on` treatment), and a preamble that says plainly what the
    selection does, because "these tags are forced on every member" is not
    something a tag list can imply on its own."""
    options = _tag_form_options(registry, current, engines=False, locked=locked)
    result = checkbox_form(
        f"Cluster tags for '{session}'  (Space to toggle):", options,
        warnings=_combo_warnings(registry),
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
    picked = set(cast("list[str]", result))
    # No engine axis, and always-on policies stay out of the build exactly as
    # in prompt_tags (they apply unconditionally and are never persisted).
    return AgentBuild(
        engine=None,
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on))


def _combo_warnings(registry: Registry) -> dict[frozenset[str], tuple[str, list[str]]]:
    """specialty/combos.info entries re-shaped for checkbox_form's warning
    zone: {tag-name set: (first message line, remaining lines)}."""
    out: dict[frozenset[str], tuple[str, list[str]]] = {}
    for combo in registry.combos:
        first, *rest = combo.message.splitlines() or [""]
        out[combo.tags] = (first, rest)
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
    """{tag name: punctuated label} for EVERY tag, all four kinds. The wants
    zone displays through this map so its header shows what the rows show —
    `'{cowork}' wants '<+bash>'` — rather than bare manifest names the user
    would have to translate. All kinds, not just the form's three: a want may
    point at any real tag."""
    return {tag.name: tag.label for tag in registry.get_all()}


@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: None = None) -> "AgentBuild | None": ...
@overload
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
                locked: frozenset[str] = frozenset(),
                fields: list[TextField],
                ) -> "tuple[dict[str, str], AgentBuild] | None": ...
def prompt_tags(registry: Registry, current: AgentBuild, *,
                instance: str, workspace: str | None = None,
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
    prompt precedes the form — and the preamble only names the agent."""
    options = _tag_form_options(registry, current, locked=locked)
    preamble = ([f"# agent:  {instance}"] if fields is not None
                else [f"# instance:  {instance}",
                      f"# workspace: {workspace}"])
    result = checkbox_form(TITLE_TAGS_FORM, options,
                           warnings=_combo_warnings(registry),
                           requires=_form_requires(registry),
                           wants=_form_wants(registry),
                           labels=_form_labels(registry),
                           preamble=preamble,
                           fields=fields)
    if result is None:
        return None
    values: dict[str, str] = {}
    if fields is not None:
        values, keys = cast("tuple[dict[str, str], list[str]]", result)
    else:
        keys = cast("list[str]", result)
    picked = set(keys)
    # Always-on (static) tags come back checked — they're locked rows — but
    # are never part of the build: applied unconditionally, never persisted.
    build = AgentBuild(
        engine=next((n for n in registry.engines if n in picked), current.engine),
        professions=tuple(n for n in registry.professions if n in picked),
        specialties=tuple(n for n in registry.specialties if n in picked),
        policies=tuple(n for n, p in registry.policies.items()
                       if n in picked and not p.always_on),
    )
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
    checked = set(result)
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

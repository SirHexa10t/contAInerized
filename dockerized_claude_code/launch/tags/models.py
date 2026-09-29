"""An AI's MODELS — the ids its vendor serves today, read from
`agents/ai/<ai>/models.list`: the options the tag form offers for an
instance's model (operator, 2026-09-28), strongest family first and newest
version first, in the order the file lists them — and each one's EFFORT
RANGE, the levels the form offers for it (operator, 2026-09-29).

One model per line, its id first, then `key=value` tokens:

    claude-opus-5-5              efforts=low,medium,high,xhigh,max
    claude-opus-4-6              efforts=low,medium,high,max
    claude-haiku-4-5-20251001    alias=claude-haiku-4-5  efforts=-

  efforts=  REQUIRED: the levels the model takes, in its AI's [scale] words
            (efforts.tiers) — any order, read in the scale's order, gaps
            allowed (Opus 4.6 takes max but not xhigh) — or `-` for a model
            that takes no effort parameter at all. `-` because the vendors'
            own `none` is a LEVEL (a request for no reasoning), which is not
            the same as sending nothing (strict-reviewer, gate model-picker-3)
  alias=    another spelling of the same model — vendors publish a dated id
            AND an alias, and efforts.tiers may pin either; repeatable
  display=  the label the picker shows after the AI's name, for an id whose
            derived label reads wrong (`model_label`)

A model the vendor retires is deleted outright (operator, 2026-09-29), so an
instance still pinned to it meets a spelling no list carries: `StaleModel`.
`#` starts a comment. The file must record when it was last checked against
the vendor — a line containing `verified YYYY-MM-DD`, like efforts.tiers — and
`ai_project-update-models` keeps it current, ranges included.

Leaf within the tag package: imports `base` only, so `ai.py` (which owns the
file) and the registry (which resolves a build's pick) can both use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .base import TagError

MODELS_FILE = "models.list"
NO_EFFORT = "-"                  # `efforts=-`: the model takes no effort parameter
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_VERIFIED = re.compile(r"verified \d{4}-\d{2}-\d{2}")
_KEYS = ("alias", "display", "efforts")
_SINGLE = ("display", "efforts")
# A version group is one or two digits (the 4 and 5 of claude-opus-4-5); a
# date stamp is four or more — MMDD (grok's 0309) or YYYYMMDD (Anthropic's
# 20251001) — so four digits is where a part stops being a version.
_DATE_STAMP_DIGITS = 4


@dataclass(frozen=True)
class Model:
    """One line of a models.list. `efforts` is the model's range in its
    AI's scale order, weakest first — () for a model that takes none."""
    id: str
    aliases: tuple[str, ...] = ()
    display: str = ""                 # the picker label after the AI's name; "" = derived (`model_label`)
    efforts: tuple[str, ...] = ()     # the levels it takes, weakest first; () = `efforts=-`

    def spells(self, text: str) -> bool:
        """Whether `text` names this model — its id or one of its aliases."""
        return text == self.id or text in self.aliases

    @property
    def effortless(self) -> bool:
        """Whether the model takes no effort parameter (`efforts=-`)."""
        return not self.efforts

    @property
    def top_effort(self) -> str | None:
        """Its strongest level — what an instance that pinned this model and
        no level runs (operator, 2026-09-29) — or None: send none."""
        return self.efforts[-1] if self.efforts else None

    def effort_for(self, pin: str | None, scale: tuple[str, ...]) -> str | None:
        """The level to send on this model for a stored `pin`: the pin when
        the model takes it; no pin, the top (a pinned model's default — an
        instance following its engine takes the tier's level instead, see
        `Instance.effort`). A pin it does NOT take falls back PRESERVING
        DIRECTION — the nearest level at or below the pin, else the lowest —
        because a person pins a level to go below the top, and landing on the
        top would do the opposite of what they asked (bug-investigator,
        strict-reviewer, gate model-picker-3). A pin that is no word of the
        AI's `scale` has no direction: the top. A model with no range sends
        nothing, whatever the pin."""
        if not self.efforts:
            return None
        if pin is None or pin in self.efforts:
            return pin or self.top_effort
        if pin not in scale:
            return self.top_effort
        rank = scale.index(pin)
        below = [level for level in self.efforts if scale.index(level) <= rank]
        return below[-1] if below else self.efforts[0]


@dataclass(frozen=True)
class StaleModel:
    """A stored pick its AI's list no longer carries — retired and deleted
    by the vendor, or never there (a typo, another AI's id). Never a reason
    to block: the launch runs the engine's model instead and says so, and
    the store keeps the spelling, so the picker goes on flagging it until
    the pick is changed."""
    spelling: str

    def why(self, ai_label: str) -> str:
        """Why the pick cannot run, in the words every notice of it uses."""
        return f"not among {ai_label}'s models"

    @property
    def shown(self) -> Model:
        """The model to label the pick by, read off the stored spelling."""
        return Model(id=self.spelling)


def parse_models(text: str, path: Path, scale: tuple[str, ...]) -> tuple[Model, ...]:
    """A models.list's text → its models, in file order, each line checked:
    an id, then `key=value` tokens from the keys above, with `efforts=`
    required and its levels words of the AI's `scale` (returned in the
    scale's order). Anything else — an unknown key, a level outside the
    scale, a spelling used twice, no verified line, an empty list — is a
    `TagError` naming the file and line."""
    if not _VERIFIED.search(text):
        raise TagError(f"{path}: needs a line saying when it was checked against the vendor "
                       f"(`# Ids verified YYYY-MM-DD against …`)")
    models: list[Model] = []
    owner: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        model_id, *tokens = line.split()
        if not _ID.fullmatch(model_id):
            raise TagError(f"{path}:{number}: {model_id!r} is not a model id")
        fields: dict[str, list[str]] = {key: [] for key in _KEYS}
        for token in tokens:
            key, sep, value = token.partition("=")
            if not sep or key not in _KEYS or not value:
                raise TagError(f"{path}:{number}: {token!r} is not one of {', '.join(k + '=' for k in _KEYS)}")
            fields[key].append(value)
        for key in _SINGLE:
            if len(fields[key]) > 1:
                raise TagError(f"{path}:{number}: {key}= appears twice")
        if not fields["efforts"]:
            raise TagError(f"{path}:{number}: {model_id} needs efforts= — the levels it takes "
                           f"({', '.join(scale) or 'this AI has none'}), or {NO_EFFORT} for none")
        model = Model(id=model_id, aliases=tuple(fields["alias"]),
                      display=fields["display"][0] if fields["display"] else "",
                      efforts=_levels(fields["efforts"][0], scale, path, number))
        for spelling in (model.id, *model.aliases):
            if not _ID.fullmatch(spelling):
                raise TagError(f"{path}:{number}: {spelling!r} is not a model id")
            if spelling in owner:
                raise TagError(f"{path}:{number}: {spelling!r} already names {owner[spelling]}")
            owner[spelling] = model.id
        models.append(model)
    if not models:
        raise TagError(f"{path}: lists no model")
    return tuple(models)


def _levels(value: str, scale: tuple[str, ...], path: Path, number: int) -> tuple[str, ...]:
    """An `efforts=` value → its levels in the scale's order (weakest first),
    or () for `-`. Order in the file is free: a diff of two ranges is read
    the same either way."""
    if value == NO_EFFORT:
        return ()
    levels = value.split(",")
    unknown = [level for level in levels if level not in scale]
    if unknown:
        raise TagError(f"{path}:{number}: efforts= {', '.join(unknown)} — not in this AI's scale "
                       f"({', '.join(scale) or 'none'}; efforts.tiers [scale])")
    if len(set(levels)) != len(levels):
        raise TagError(f"{path}:{number}: efforts= names a level twice")
    return tuple(sorted(levels, key=scale.index))


def model_label(model: Model, prefix: str) -> str:
    """The model as the picker shows it after the AI's name: its `display=`
    if the file gives one, else derived from the id — the AI's `prefix`
    dropped, a date stamp dropped (so a dated snapshot reads as its alias
    does: claude-haiku-4-5-20251001 → Haiku-4.5), words capitalised, and a
    run of dash-separated digit groups joined by dots (claude-opus-5-5 →
    Opus-5.5; gemini-3.8-flash → 3.8-Flash, already dotted). Best-effort by
    design: a test renders every shipped id's label, and `display=` fixes
    the ones that read wrong."""
    if model.display:
        return model.display
    words: list[str] = []
    version: list[str] = []
    for part in model.id.removeprefix(prefix).split("-"):
        if part.isdigit() and len(part) >= _DATE_STAMP_DIGITS:
            continue
        if part.isdigit():
            version.append(part)
            continue
        if version:
            words.append(".".join(version))
            version = []
        words.append(part[:1].upper() + part[1:])
    if version:
        words.append(".".join(version))
    return "-".join(words) or model.id

"""The TUI style system + display coercers — what EVERY launcher surface
draws with (launch/gui).

Extracted from tag_form 2026-09-03: these are not form machinery. The picker
uses them as much as the forms do (a Cont row's tag chips, the F8 legend's
rich tables), and keeping them behind a module named after one form made
`menu_picker` import "tag_form" for colours it uses on rows that have no
form in sight.

  UiClass / STYLE_DICT   the prompt_toolkit CSS classes and their styles
  STYLE_TAG_* / tag_style / squashed_tag_style / RICH_BY_STYLE / rich_style
                         field-driven tag colouring: a new tag never touches
                         UI code, because the colour is derived from the
                         tag's own fields (warn / stance / conf)
  STYLE_AGENT_NAME / RICH_AGENT_NAME
                         the one blue every NAME wears (agent, instance,
                         cluster, member, a form field's value), in both
                         toolkits' spellings
  _normalize / _plain    the display coercers filter matching runs on
  _fragment_source       the typing-only adapter every FormattedTextControl
                         call site passes its fragment builder through
  TITLE_HEIGHT / STATUS_HEIGHT
                         the chrome row counts every full-screen surface
                         reserves — one title line, two status lines

Leaf within gui/: imports `..tags` for the Tag type it dispatches on, and
nothing from its siblings.
"""

from enum import Enum
from typing import Callable, Iterable, cast

from prompt_toolkit.formatted_text import AnyFormattedText

from ..tags import Ai, PolicyStance, Tag

# ============================================================
# Shared style system (used by the form AND menu_picker)
# ============================================================

class UiClass(Enum):
    """prompt_toolkit CSS-like class names + the style applied to spans tagged
    with each. Bundling both on the enum member keeps the class name and its
    style co-located (adding a new entry threads through STYLE_DICT below
    without a second hand-maintained list). Members expose:
      .cls_name — the CSS-like class string used in prompt_toolkit style refs
      .style    — the prompt_toolkit style string applied to that class
      .css      — `class:<cls_name>` — what span tuples want as the style key
    """
    TITLE    = ("picker-title",    "bold fg:ansibrightcyan")
    DIVIDER  = ("picker-divider",  "fg:ansibrightblack")
    STATUS   = ("picker-status",   "fg:ansibrightblack")
    FILTER   = ("picker-filter",   "bold fg:ansiyellow")
    CURSOR   = ("picker-cursor",   "reverse")
    PREVIEW  = ("picker-preview",  "")
    NO_MATCH = ("picker-no-match", "italic fg:ansibrightblack")
    WARNING  = ("picker-warning",  "bold fg:ansibrightred")

    def __init__(self, cls_name: str, style: str) -> None:
        self.cls_name = cls_name
        self.style = style

    @property
    def css(self) -> str:
        return f"class:{self.cls_name}"


STYLE_DICT = {e.cls_name: e.style for e in UiClass}

# Chrome heights, shared by every full-screen surface: the forms and the
# picker both reserve one line for the title and two for the status/hint rows
# below. One definition, because a surface that reserved a different number
# would misalign against its siblings for no reason a reader could find.
TITLE_HEIGHT  = 1
STATUS_HEIGHT = 2

# Tag coloring is field-driven, so adding a new tag never touches the UI:
#   warn-flagged specialty      → bold bright red (danger)
#   policy, stance=DENY         → blue       (tightens the leash)
#   policy, stance=ALLOW        → orange     (grants ability, loosens it)
#   policy, stance=DEMAND       → bold white (mandates a behavior)
#   engine                      → cyan       (a budget, not a capability/risk)
#   everything else             → bright green
STYLE_TAG_WARN       = "bold fg:ansibrightred"
STYLE_TAG_SAFE       = "fg:ansibrightgreen"
STYLE_TAG_DENY       = "bold fg:ansibrightblue"
STYLE_TAG_ALLOW      = "bold fg:#ff8700"
STYLE_TAG_DEMAND     = "bold fg:ansiwhite"
STYLE_TAG_ENGINE     = "fg:ansibrightcyan"
STYLE_UNDERLINE      = "underline"   # the fullname lead-in of description text
STYLE_TAG_INVALID    = "fg:ansiblack bg:ansired"   # a stored tag name that no longer resolves (picker Cont rows)
STYLE_LOCKED         = "fg:ansibrightblack"         # a form row the user can't toggle (grayed; e.g. [code]'s always-on Python)
# Every NAME the launcher shows — agent, instance, cluster, member — and the
# value typed into a form field wear this one blue, so a name reads the same
# wherever it appears. It was defined four times under four names (2026-09-09)
# before landing here; the rich spelling exists because previews render
# through rich while rows render through prompt_toolkit.
STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
RICH_AGENT_NAME      = "bold bright_blue"

_STYLE_BY_STANCE = {
    PolicyStance.ALLOW:      STYLE_TAG_ALLOW,
    PolicyStance.DENY:       STYLE_TAG_DENY,
    PolicyStance.DEMAND:     STYLE_TAG_DEMAND,
}

# Rich equivalents for the same tag styles (rich and prompt_toolkit name
# colors differently) — consumed by menu_picker's F8 legend tables.
RICH_BY_STYLE = {
    STYLE_TAG_WARN:       "bold bright_red",
    STYLE_TAG_SAFE:       "bright_green",
    STYLE_TAG_DENY:       "bold bright_blue",
    STYLE_TAG_ALLOW:      "bold #ff8700",
    STYLE_TAG_DEMAND:     "bold white",
    STYLE_TAG_ENGINE:     "bright_cyan",
}


def squashed_tag_style(style: str) -> str:
    """The chip form of a tag style: the tag's usual foreground color turned
    into the BACKGROUND, with a black glyph on top. Used wherever SQUASH_AT or
    more tags share a row and each collapses to `Tag.squash_glyph` — with the
    name gone, the color block is what still says "specialty, dangerous" or
    "policy, deny" at a glance. Derived from the style string rather than
    listed per-constant so a new tag color cannot be forgotten here. A style
    that already paints a background (an AI tag's logo colours) IS a chip and
    is kept whole — turning Grok's black glyph into a black block would erase
    it."""
    tokens = style.split()
    if any(token.startswith("bg:") for token in tokens):
        return style
    color = next((token.removeprefix("fg:") for token in tokens
                  if token.startswith("fg:")), "ansiwhite")
    return f"fg:ansiblack bg:{color}"


def tag_style(tag: Tag) -> str:
    """The style for one tag's label — the AI's own logo colours (the one kind
    coloured per MEMBER, from its tag.info), else dispatched on the
    kind-specific fields (duck-typed: only specialties carry `warn`, only
    policies carry `stance`, only engines carry `budget`)."""
    if isinstance(tag, Ai):
        return tag.style
    if getattr(tag, "warn", False):
        return STYLE_TAG_WARN
    stance = getattr(tag, "stance", None)
    if stance is not None:
        return _STYLE_BY_STANCE[stance]
    if hasattr(tag, "budget"):
        return STYLE_TAG_ENGINE
    return STYLE_TAG_SAFE


def rich_style(style: str) -> str:
    """A prompt_toolkit style string as rich spells it — the picker draws rows
    with prompt_toolkit and previews / the legend with rich, and one tag must
    look the same in both. The fixed tag styles map through RICH_BY_STYLE; any
    other (an AI's `fg:#hex bg:#hex`, a chip) converts token by token:
    `fg:X` → `X`, `bg:Y` → `on Y`, attributes pass through, and rich's
    `ansibrightred` is `bright_red`."""
    if style in RICH_BY_STYLE:
        return RICH_BY_STYLE[style]
    parts: list[str] = []
    for token in style.split():
        if token.startswith("fg:"):
            parts.append(_rich_color(token[3:]))
        elif token.startswith("bg:"):
            parts.append(f"on {_rich_color(token[3:])}")
        else:
            parts.append(token)
    return " ".join(parts)


def _rich_color(color: str) -> str:
    """`ansibrightred` → `bright_red`, `ansired` → `red`; hex and rich names unchanged."""
    if color.startswith("ansi"):
        name = color.removeprefix("ansi")
        return f"bright_{name.removeprefix('bright')}" if name.startswith("bright") else name
    return color


def _normalize(display: str | Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _plain(display: str | Iterable[tuple[str, str]]) -> str:
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


def _fragment_source(build: Callable[[], list[tuple[str, str]]]) -> AnyFormattedText:
    """Hand one of our fragment builders to a prompt_toolkit FormattedTextControl.

    Purely a typing adapter — it returns `build` unchanged, so there is no
    runtime effect whatsoever. It exists because prompt_toolkit's
    `AnyFormattedText` accepts `list[tuple[str, str] | tuple[str, str,
    MouseHandler]]`, while our builders return the narrower `list[tuple[str,
    str]]`. `list` is invariant, so the two are not assignable even though every
    value we produce is valid — the mismatch is at the library boundary, not in
    our code.

    Fixing it here rather than by widening our own annotations keeps our
    contracts honest: this codebase never emits the 3-tuple mouse-handler form,
    so declaring that it might would make every fragment helper less precise.
    One cast in one place beats ten scattered suppressions."""
    return cast("AnyFormattedText", build)



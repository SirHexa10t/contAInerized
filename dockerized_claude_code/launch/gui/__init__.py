"""The launcher's interactive TUI, isolated in one package:

  - `tag_form` — every form (the kind-sectioned tag form, the toolkit form,
    the generic `checkbox_form` primitive) plus the shared style system
    (`UiClass`, `tag_style`, the stance colors) both surfaces use.
  - `menu_picker` — the full-screen picker (main menu, deletion submenu,
    the "Edit Toolkits" opener), the F8 legend, and line prompts
    (workspace, session).

Dependency direction *within* the package: menu_picker imports from
tag_form, never the reverse. Nothing outside this package imports
prompt_toolkit — run.py drives the whole TUI through the names re-exported
here."""

from .menu_picker import ask_for_workspace, prompt_session, select_agent
from .tag_form import checkbox_form, edit_toolkits_menu, prompt_tags

__all__ = [
    "select_agent", "ask_for_workspace", "prompt_session",
    "prompt_tags", "checkbox_form", "edit_toolkits_menu",
]

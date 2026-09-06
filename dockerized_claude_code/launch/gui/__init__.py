"""The launcher's interactive TUI, isolated in one package. Nine modules, one
role each (`tag_form` held the first three at once until 2026-09-03):

  - `styles`      — the style system every surface draws with: the `UiClass`
                    CSS classes, the field-driven tag colours (`tag_style`,
                    `squashed_tag_style`, `RICH_BY_STYLE`), and the display
                    coercers. The picker uses these as much as the forms do.
  - `form_core`   — the generic form: `FormOption`, `TextField`, the shared
                    confirm/really-done? gate, and the `checkbox_form`
                    primitive. No form may restate any of it.
  - `forms`       — what the launcher ASKS: the instance tag form, its
                    cluster-wide sibling, the merged preferences form.
  - `cluster_form`— the membership form, whose accumulator semantics (picking
                    an agent adds ANOTHER) need their own key map; it runs on
                    form_core's scaffold and takes its pick algebra from
                    `cluster.legoset`.
  - `picker_previews`— every row kind's preview text, and the child-process
                    read that keeps a huge transcript off the UI thread.
  - `picker_prompts`— the non-fullscreen bits BOTH the picker and its flows
                    need: line prompts, inline dialogs, field builders.
  - `picker_flows`— what each picker key MEANS once a row is chosen: the
                    create / edit / destroy flows.
  - `picker_widget`— the reusable picker: the row models (`PickerEntry` and
                    its deferred-preview slots), the preview LOADER, cursor
                    movement over skippable rows, and the selection loop.
                    Knows nothing about agents.
  - `menu_picker` — the launcher's three MENUS built on that widget: the main
                    agent picker, the deletion submenu, `--stop`'s selector,
                    plus the F8 composition legend.

Dependency direction *within* the package, strictly one-way:
`styles` → `form_core` → {`forms`, `cluster_form`} → {`picker_previews`,
`picker_prompts`} → {`picker_flows`, `picker_widget`} → `menu_picker`.
Nothing outside this package imports prompt_toolkit — run.py drives the whole
TUI through the names re-exported here."""

from .form_core import checkbox_form
from .forms import edit_profiles_menu, prompt_tags
from .menu_picker import prompt_stop, select_agent
from .picker_prompts import ask_for_workspace, instance_fields

__all__ = [
    "select_agent", "ask_for_workspace", "instance_fields", "prompt_stop",
    "prompt_tags", "checkbox_form", "edit_profiles_menu",
]

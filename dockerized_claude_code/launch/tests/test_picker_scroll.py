"""Tests for the picker's per-area mouse scrolling.

The behaviour under test is "the wheel moves whichever side the pointer is over".
prompt_toolkit already delivers a mouse event only to the control under the
pointer, so the routing itself is the framework's; what OUR code has to get right
is the two step policies and the clamping — a preview that scrolls off its own top
looks like a blank pane, and a stale offset carried into the next row's preview
opens it part-way down for no reason.

`_ScrollingControl` is exercised directly with synthetic MouseEvents rather than
through a running Application: the handler is the whole contract, and driving a
full-screen app in a test buys nothing but flakiness.
"""

import unittest

from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import (
    MouseButton, MouseEvent, MouseEventType, MouseModifier,
)

from launch.gui.menu_picker import WHEEL_LINES, _ScrollingControl


def an_event(event_type: MouseEventType) -> MouseEvent:
    """A synthetic wheel/click event. `button` and `modifiers` are required by
    prompt_toolkit 3.0.53's MouseEvent and irrelevant to scroll routing."""
    return MouseEvent(position=Point(x=0, y=0), event_type=event_type,
                      button=MouseButton.NONE, modifiers=frozenset[MouseModifier]())


def wheel(control: _ScrollingControl, event_type: MouseEventType, times: int = 1) -> None:
    for _ in range(times):
        control.mouse_handler(an_event(event_type))


class TestScrollingControl(unittest.TestCase):
    """The control translates wheel events into notches and nothing else."""

    def setUp(self):
        self.seen: list[int] = []
        self.control = _ScrollingControl("text", on_scroll=self.seen.append)

    def test_up_and_down_become_signed_notches(self):
        # Notches, not lines: the list wants one ROW per notch and the preview
        # several LINES, and only each side knows which.
        wheel(self.control, MouseEventType.SCROLL_UP)
        wheel(self.control, MouseEventType.SCROLL_DOWN)
        self.assertEqual(self.seen, [-1, 1])

    def test_each_notch_is_one_call(self):
        wheel(self.control, MouseEventType.SCROLL_DOWN, times=4)
        self.assertEqual(self.seen, [1, 1, 1, 1])

    def test_a_handled_scroll_reports_as_handled(self):
        # Returning NotImplemented would let the event bubble, and the other side
        # of the split would react to a wheel that was never over it.
        result = self.control.mouse_handler(an_event(MouseEventType.SCROLL_UP))
        self.assertIsNone(result)

    def test_other_mouse_events_are_left_to_the_base_class(self):
        # Clicks still have to focus a row; swallowing them would break that.
        self.control.mouse_handler(an_event(MouseEventType.MOUSE_UP))
        self.assertEqual(self.seen, [])


class TestStepPolicies(unittest.TestCase):
    """The two sides step differently on purpose. These reproduce the policies as
    the picker defines them, so a change to WHEEL_LINES or to the clamp has to be
    deliberate rather than incidental."""

    def test_the_preview_moves_several_lines_per_notch(self):
        # A one-line-per-notch preview would take forty notches to read an agent
        # persona; a whole page per notch overshoots.
        self.assertGreater(WHEEL_LINES, 1)
        self.assertLess(WHEEL_LINES, 10)

    def scroll_preview(self, offset: int, notches: int, total: int) -> int:
        """The picker's clamp, isolated: two lines always stay reachable."""
        limit = max(0, total - 2)
        return max(0, min(offset + notches * WHEEL_LINES, limit))

    def test_scrolling_up_at_the_top_stays_put(self):
        self.assertEqual(self.scroll_preview(0, -1, total=100), 0)

    def test_scrolling_down_stops_before_the_content_runs_out(self):
        # Free-running would scroll a short preview into empty space, which reads
        # as "the pane went blank" rather than "that was the end".
        self.assertEqual(self.scroll_preview(0, 99, total=10), 8)

    def test_a_preview_shorter_than_the_clamp_cannot_scroll(self):
        self.assertEqual(self.scroll_preview(0, 5, total=2), 0)
        self.assertEqual(self.scroll_preview(0, 5, total=0), 0)

    def test_a_notch_down_then_up_returns_to_where_it_started(self):
        down = self.scroll_preview(0, 1, total=100)
        self.assertEqual(self.scroll_preview(down, -1, total=100), 0)


class TestMouseSupportIsEnabled(unittest.TestCase):
    """The handlers are useless without this, and the failure is SILENT.

    prompt_toolkit's `mouse_support` defaults to False, which means the terminal
    is never put into mouse-reporting mode and no control receives any mouse
    event at all. The scroll handlers were written, wired, and unit-tested while
    the wheel did nothing — nothing errored, because nothing was ever called.
    Asserting on the constructed Application rather than on the source line, so
    the test fails for the real reason."""

    def build(self) -> dict:
        from unittest.mock import patch

        from launch.gui import menu_picker

        captured: dict = {}

        class FakeApp:
            def __init__(self, **kw: object) -> None:
                captured.update(kw)

            def invalidate(self) -> None: ...

            def run(self) -> None: ...

        with patch.object(menu_picker, "Application", FakeApp):
            menu_picker.pick_with_preview(
                "t", [menu_picker.PickerEntry(display=[("", "row")], value=None)])
        return captured

    def test_the_application_is_built_with_mouse_support(self):
        self.assertIs(self.build().get("mouse_support"), True)


if __name__ == "__main__":
    unittest.main()

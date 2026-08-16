"""Tests for the Steam Input template generator (deck/install-controller-template.py)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

DECK = Path(__file__).resolve().parents[2] / "deck"
sys.path.insert(0, str(DECK))

import vdf  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sdss_controller_template", DECK / "install-controller-template.py"
)
assert _spec and _spec.loader
template = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(template)


BASE = """\ufeff"controller_mappings"
{
\t"version" "3"
\t"title" "#Title"
\t"controller_type"\t\t"controller_neptune"
\t"localization"
\t{
\t\t"english"
\t\t{
\t\t\t"title"\t\t"Gamepad"
\t\t}
\t}
\t"group"
\t{
\t\t"id"\t\t"0"
\t\t"mode"\t\t"four_buttons"
\t}
\t"group"
\t{
\t\t"id"\t\t"7"
\t\t"mode"\t\t"switches"
\t\t"inputs"
\t\t{
\t\t\t"button_escape"
\t\t\t{
\t\t\t\t"activators"
\t\t\t\t{
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


CONDITIONAL = """"controller_mappings"
{
\t"version"\t\t"3" [$WIN32]
\t"group"
\t{
\t\t"id"\t\t"0" [!$PS3]
\t\t"mode"\t\t"switches"
\t}
\t"settings"
\t{
\t\t"left_trackpad_mode"\t\t"0"
\t} [$OSX]
\t"tail"\t\t"end"
}
"""


class ConditionalTest(unittest.TestCase):
    """A platform conditional used to be parsed as the next key, silently shifting
    every following pair and breaking nesting depth."""

    def test_conditional_after_value_does_not_shift_pairs(self) -> None:
        parsed = vdf.loads('"a" { "b" "c" [$WIN32] "d" "e" }')
        self.assertEqual(parsed, [("a", [("b", "c"), ("d", "e")])])

    def test_conditional_inside_nested_block(self) -> None:
        root = vdf.get(vdf.loads(CONDITIONAL), "controller_mappings")
        group = vdf.get(root, "group")
        self.assertEqual(group, [("id", "0"), ("mode", "switches")])
        self.assertEqual(vdf.get(root, "tail"), "end")
        self.assertEqual(vdf.get(root, "version"), "3")

    def test_conditional_is_preserved(self) -> None:
        root = vdf.get(vdf.loads(CONDITIONAL), "controller_mappings")
        self.assertEqual(vdf.get(root, "version").conditional, "[$WIN32]")
        self.assertEqual(vdf.get(root, "settings").conditional, "[$OSX]")

    def test_roundtrip_preserves_conditionals(self) -> None:
        parsed = vdf.loads(CONDITIONAL)
        text = vdf.dumps(parsed)
        self.assertIn('"version"\t\t"3" [$WIN32]', text)
        self.assertIn("} [$OSX]", text)
        self.assertEqual(vdf.dumps(vdf.loads(text)), text)

    def test_conditional_where_a_key_is_expected_is_loud(self) -> None:
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"a" { [$WIN32] "b" "c" }')

    def test_unterminated_conditional_is_loud(self) -> None:
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"a" "b" [$WIN32')

    def test_build_preserves_conditionals(self) -> None:
        base = BASE.replace('\t"version" "3"', '\t"version" "3" [$WIN32]')
        built = template.build(base)
        self.assertIn("[$WIN32]", built)
        self.assertIn(template.NATIVE_TOUCH_BINDING, built)


class VdfTest(unittest.TestCase):
    def test_roundtrip_is_stable(self) -> None:
        parsed = vdf.loads(BASE)
        self.assertEqual(parsed, vdf.loads(vdf.dumps(parsed)))

    def test_repeated_keys_are_preserved(self) -> None:
        mappings = vdf.get(vdf.loads(BASE), "controller_mappings")
        self.assertEqual(sum(1 for key, _ in mappings if key == "group"), 2)

    def test_comments_and_bom_are_ignored(self) -> None:
        parsed = vdf.loads('\ufeff// hi\n"a"\n{\n"b" "c"\n}\n')
        self.assertEqual(parsed, [("a", [("b", "c")])])

    def test_unterminated_string_raises(self) -> None:
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"a"\n{\n"b" "c\n')

    def test_trailing_comment_without_newline_is_ignored(self) -> None:
        # Previously the tokenizer broke out of the loop here, silently dropping every
        # token after the comment instead of only the comment.
        self.assertEqual(vdf.loads('"a"\n{\n"b" "c"\n}\n// trailing'), [("a", [("b", "c")])])

    def test_comment_only_ends_at_its_own_newline(self) -> None:
        self.assertEqual(
            vdf.loads('"a"\n{\n// note\n"b" "c"\n}\n'), [("a", [("b", "c")])]
        )

    def test_quotes_and_backslashes_round_trip(self) -> None:
        pairs = [("key", [("path", "C:\\Games\\x"), ("name", 'a "quoted" name')])]
        self.assertEqual(vdf.loads(vdf.dumps(pairs)), pairs)

    def test_escapes_are_decoded_on_read(self) -> None:
        self.assertEqual(vdf.loads(r'"a" "b\"c"'), [("a", 'b"c')])


class TemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.built = vdf.loads(template.build(BASE))
        self.mappings = vdf.get(self.built, "controller_mappings")

    def test_marked_as_a_template(self) -> None:
        self.assertEqual(vdf.get(self.mappings, "export_type"), "template")
        self.assertEqual(vdf.get(self.mappings, "title"), template.TITLE)
        self.assertEqual(
            vdf.get(self.mappings, "url"), f"template://{template.TEMPLATE_NAME}"
        )

    def test_adds_always_on_native_touch(self) -> None:
        group = template._switch_group(self.mappings)
        inputs = vdf.get(group, "inputs")
        always_on = vdf.get(inputs, "always_on_action")
        self.assertIsNotNone(always_on)
        self.assertIn(template.NATIVE_TOUCH_BINDING, vdf.dumps(always_on))

    def test_is_idempotent(self) -> None:
        once = template.build(BASE)
        self.assertEqual(once, template.build(once))
        group = template._switch_group(vdf.get(vdf.loads(once), "controller_mappings"))
        inputs = vdf.get(group, "inputs")
        self.assertEqual(sum(1 for key, _ in inputs if key == "always_on_action"), 1)

    def test_other_groups_survive(self) -> None:
        self.assertEqual(sum(1 for key, _ in self.mappings if key == "group"), 2)

    def test_missing_switch_group_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            template.build('"controller_mappings"\n{\n"version" "3"\n}\n')


if __name__ == "__main__":
    unittest.main()

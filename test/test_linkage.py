# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Tests for subfield 6 linkage parsing, validation and strict queries."""

import unittest

from pymarc import (
    Field,
    Indicators,
    InvalidLinkage,
    Linkage,
    LinkageIndex,
    Record,
    Subfield,
    parse_linkage,
)


def make_field(tag, *subfields, indicators=(" ", " ")):
    """Build a data field from (code, value) pairs."""
    return Field(
        tag=tag,
        indicators=Indicators(*indicators),
        subfields=[Subfield(code, value) for code, value in subfields],
    )


def linked_record():
    """A record with a multi-880 group, a single-880 group and a standalone 880."""
    record = Record()
    record.add_field(
        make_field("245", ("6", "880-01"), ("a", "Title"), indicators=("1", "0")),
        make_field("880", ("6", "245-01/$1"), ("a", "題名"), indicators=("1", "0")),
        make_field("880", ("6", "245-01/(N"), ("a", "Zaglavie"), indicators=("1", "0")),
        make_field("100", ("6", "880-02"), ("a", "Author"), indicators=("1", " ")),
        make_field("880", ("6", "100-02"), ("a", "著者"), indicators=("1", " ")),
        make_field("500", ("a", "A plain note.")),
        make_field("880", ("6", "880-00"), ("a", "注記")),
    )
    return record


class ParseLinkageTestCase(unittest.TestCase):
    """parse_linkage: the single strict reading of a subfield 6."""

    def test_valid_values(self):
        self.assertEqual(parse_linkage("880-01"), Linkage("880", "01", ""))
        self.assertEqual(parse_linkage("245-99"), Linkage("245", "99", ""))
        self.assertEqual(parse_linkage("245-13/(2/r"), Linkage("245", "13", "/(2/r"))
        self.assertEqual(parse_linkage("100-42/$1"), Linkage("100", "42", "/$1"))
        self.assertEqual(parse_linkage("880-00"), Linkage("880", "00", ""))

    def test_malformed_values(self):
        for value in (
            "",
            "880",
            "880-",
            "880-1",
            "880-001",
            "88-01",
            "880-0a",
            "880 01",
            "880-01-",
            "-01",
        ):
            with self.assertRaises(ValueError, msg=value):
                parse_linkage(value)


class FieldLinkageTestCase(unittest.TestCase):
    """Field.linkage: the Field level reading both modes agree on."""

    def test_no_subfield_6(self):
        field = make_field("245", ("a", "Title"))
        self.assertIsNone(field.linkage())
        self.assertIsNone(field.linkage(strict=True))

    def test_control_field(self):
        self.assertIsNone(Field(tag="008", data="x" * 40).linkage(strict=True))

    def test_lenient_and_strict_agree(self):
        field = make_field("245", ("6", "880-01/$1"), ("a", "Title"))
        self.assertEqual(field.linkage(), Linkage("880", "01", "/$1"))
        self.assertEqual(field.linkage(strict=True), Linkage("880", "01", "/$1"))

    def test_lenient_malformed_returns_none(self):
        field = make_field("245", ("6", "880-1"), ("a", "Title"))
        self.assertIsNone(field.linkage())

    def test_strict_malformed_raises(self):
        field = make_field("245", ("6", "880-1"), ("a", "Title"))
        with self.assertRaises(InvalidLinkage):
            field.linkage(strict=True)

    def test_strict_duplicate_subfield_6(self):
        field = make_field("245", ("6", "880-01"), ("6", "880-02"), ("a", "Title"))
        with self.assertRaisesRegex(InvalidLinkage, "duplicate subfield 6"):
            field.linkage(strict=True)


class LinkageIndexTestCase(unittest.TestCase):
    """LinkageIndex: one validated pass over a record's linkage."""

    def test_groups_and_standalone(self):
        index = LinkageIndex(linked_record(), label="cat")
        self.assertEqual(sorted(index.groups), ["01", "02"])
        group = index.groups["01"]
        self.assertEqual(group.anchor.tag, "245")
        self.assertEqual([f.tag for f in group.alternates], ["880", "880"])
        self.assertEqual([f.tag for f in group.fields], ["245", "880", "880"])
        self.assertEqual(len(index.standalone), 1)
        self.assertEqual(index.standalone[0]["6"], "880-00")

    def test_free_numbers_skip_used(self):
        index = LinkageIndex(linked_record())
        free = index.free_numbers()
        self.assertEqual(next(free), "03")
        self.assertNotIn("01", list(LinkageIndex(linked_record()).free_numbers()))
        self.assertNotIn("00", list(LinkageIndex(linked_record()).free_numbers()))

    def test_unit_for_field(self):
        record = linked_record()
        index = LinkageIndex(record)
        group = index.unit_for_field(record["245"])
        self.assertEqual(group.number, "01")
        # an 880 member resolves to the same group
        self.assertIs(index.unit_for_field(group.alternates[0]), group)
        # an unlinked field is a singleton unit
        unit = index.unit_for_field(record["500"])
        self.assertIsNone(unit.number)
        self.assertEqual(unit.fields, (record["500"],))
        # a standalone 880 stays put and is not selectable
        self.assertIsNone(index.unit_for_field(index.standalone[0]))
        # a field of another record is not found
        self.assertIsNone(index.unit_for_field(make_field("245", ("a", "Elsewhere"))))

    def assert_invalid(self, fields, *needles):
        record = Record()
        record.add_field(*fields)
        with self.assertRaises(InvalidLinkage) as caught:
            LinkageIndex(record, label="broken.mrc")
        message = str(caught.exception)
        self.assertIn("broken.mrc", message)
        for needle in needles:
            self.assertIn(needle, message)
        return caught.exception

    def test_duplicate_ownership(self):
        err = self.assert_invalid(
            [
                make_field("245", ("6", "880-01"), ("a", "One")),
                make_field("650", ("6", "880-01"), ("a", "Two")),
                make_field("880", ("6", "245-01"), ("a", "題名")),
            ],
            "claimed by more than one field",
            "245",
            "650",
        )
        self.assertEqual(len(err.problems), 2)
        self.assertEqual([p.position for p in err.problems], [0, 1])

    def test_mismatched_ends(self):
        self.assert_invalid(
            [
                make_field("245", ("6", "880-01"), ("a", "Title")),
                make_field("880", ("6", "650-01"), ("a", "主題")),
            ],
            "links back to 650",
            "anchor is 245",
        )

    def test_dangling_anchor(self):
        self.assert_invalid(
            [make_field("245", ("6", "880-01"), ("a", "Title"))],
            "no 880 field links back to 245-01",
        )

    def test_dangling_880(self):
        self.assert_invalid(
            [make_field("880", ("6", "245-07"), ("a", "題名"))],
            "no such field exists",
        )

    def test_duplicate_subfield_6(self):
        self.assert_invalid(
            [make_field("245", ("6", "880-01"), ("6", "880-02"), ("a", "Title"))],
            "duplicate subfield 6",
        )

    def test_malformed_subfield_6(self):
        self.assert_invalid(
            [make_field("245", ("6", "880-1"), ("a", "Title"))],
            "malformed subfield 6",
            "880-1",
        )

    def test_880_linking_to_880(self):
        self.assert_invalid(
            [
                make_field("245", ("6", "880-05"), ("a", "Title")),
                make_field("880", ("6", "880-05"), ("a", "題名")),
            ],
            "880 field links to 880",
        )

    def test_non_880_linking_to_non_880(self):
        self.assert_invalid(
            [make_field("500", ("6", "245-01"), ("a", "Note"))],
            "links to 245 instead of 880",
        )

    def test_non_880_with_number_00(self):
        self.assert_invalid(
            [make_field("245", ("6", "880-00"), ("a", "Title"))],
            "occurrence number 00",
        )

    def test_880_without_subfield_6(self):
        self.assert_invalid(
            [make_field("880", ("a", "題名"))],
            "880 field without subfield 6",
        )

    def test_multiple_problems_reported_together(self):
        err = self.assert_invalid(
            [
                make_field("245", ("6", "880-01"), ("a", "Title")),
                make_field("880", ("6", "650-07"), ("a", "主題")),
            ],
            "no 880 field links back to 245-01",
            "no such field exists",
        )
        self.assertEqual(len(err.problems), 2)


class StrictQueryTestCase(unittest.TestCase):
    """Record.get_linked_fields(strict=True) and Record.linkage_index()."""

    def test_strict_returns_group_alternates(self):
        record = linked_record()
        linked = record.get_linked_fields(record["245"], strict=True)
        self.assertEqual([f["a"] for f in linked], ["題名", "Zaglavie"])

    def test_strict_unlinked_field(self):
        record = linked_record()
        self.assertEqual(record.get_linked_fields(record["500"], strict=True), [])

    def test_strict_raises_on_broken_record(self):
        record = Record()
        record.add_field(make_field("245", ("6", "880-01"), ("a", "Title")))
        with self.assertRaises(InvalidLinkage):
            record.get_linked_fields(record["245"], strict=True)

    def test_lenient_default_unchanged(self):
        record = linked_record()
        self.assertEqual(len(record.get_linked_fields(record["245"])), 2)

    def test_linkage_index_method(self):
        record = linked_record()
        index = record.linkage_index(label="mine")
        self.assertIsInstance(index, LinkageIndex)
        self.assertEqual(index.label, "mine")
        self.assertEqual(sorted(index.groups), ["01", "02"])


if __name__ == "__main__":
    unittest.main()

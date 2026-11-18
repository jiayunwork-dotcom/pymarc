# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Tests for preview_migration/MigrationPlan and the strict writers.

The two hand built records below describe the same edition in two
catalogs: "home" (the record we enrich) and "ext" (the record we pick
title/author/subject groups from). The external title group and the home
subject group both use occurrence number 01, so the import has to be
renumbered; the home subject's 880 must not end up mistaken for a title.
"""

import unittest
from io import BytesIO

from pymarc import (
    Field,
    ImportGroup,
    Indicators,
    InvalidLinkage,
    LinkageIndex,
    LinkageNumbersExhausted,
    MARCReader,
    MARCWriter,
    MigrationConflict,
    Record,
    ReplaceGroup,
    Subfield,
    XMLWriter,
    parse_xml_to_array,
    preview_migration,
)


def make_field(tag, *subfields, indicators=(" ", " ")):
    """Build a data field from (code, value) pairs."""
    return Field(
        tag=tag,
        indicators=Indicators(*indicators),
        subfields=[Subfield(code, value) for code, value in subfields],
    )


def home_record():
    """The home record: a romanized-only title, a thin author group (02),
    a subject group (01) and a standalone 880-00 note."""
    record = Record()
    record.add_field(Field(tag="001", data="home-001"))
    record.add_field(
        make_field("100", ("6", "880-02"), ("a", "Tanaka, K."), indicators=("1", " ")),
        make_field("880", ("6", "100-02/$1"), ("a", "田中K"), indicators=("1", " ")),
        make_field(
            "245",
            ("a", "Nihon no min'yō : "),
            ("b", "shūroku to kenkyū /"),
            ("c", "Tanaka Kenji."),
            indicators=("1", "0"),
        ),
        make_field(
            "264",
            ("a", "Tōkyō : "),
            ("b", "Heibonsha, "),
            ("c", "2020."),
            indicators=(" ", "1"),
        ),
        make_field(
            "300", ("a", "320 pages : "), ("b", "illustrations ; "), ("c", "23 cm")
        ),
        make_field(
            "650",
            ("6", "880-01"),
            ("a", "Min'yō"),
            ("z", "Japan."),
            indicators=(" ", "0"),
        ),
        make_field(
            "880",
            ("6", "650-01/$1"),
            ("a", "民謡"),
            ("z", "日本."),
            indicators=(" ", "0"),
        ),
        make_field("500", ("a", "Gift of the author.")),
        make_field("880", ("6", "880-00"), ("a", "著者の贈呈本。")),
    )
    return record


def ext_record():
    """The external record for the same edition: a richer author group (04),
    a title group (01) with two 880s in one group, and a subject group (03)
    that the plan leaves behind."""
    record = Record()
    record.add_field(Field(tag="001", data="ext-0901"))
    record.add_field(
        make_field(
            "100", ("6", "880-04"), ("a", "Tanaka, Kenji."), indicators=("1", " ")
        ),
        make_field("880", ("6", "100-04/$1"), ("a", "田中健二"), indicators=("1", " ")),
        make_field(
            "245",
            ("6", "880-01"),
            ("a", "Nihon no min'yō : "),
            ("b", "shūroku to kenkyū /"),
            ("c", "Tanaka Kenji."),
            indicators=("1", "0"),
        ),
        make_field(
            "880",
            ("6", "245-01/$1"),
            ("a", "日本の民謡 : "),
            ("b", "収録と研究"),
            indicators=("1", "0"),
        ),
        make_field(
            "880",
            ("6", "245-01/(N"),
            ("a", "I︠A︡ponskie narodnye pesni."),
            indicators=("1", "0"),
        ),
        make_field(
            "264",
            ("a", "Tōkyō : "),
            ("b", "Heibonsha, "),
            ("c", "2020."),
            indicators=(" ", "1"),
        ),
        make_field(
            "650",
            ("6", "880-03"),
            ("a", "Min'yō"),
            ("z", "Japan."),
            indicators=(" ", "0"),
        ),
        make_field(
            "880",
            ("6", "650-03/$1"),
            ("a", "民謡"),
            ("z", "日本."),
            indicators=(" ", "0"),
        ),
    )
    return record


def make_plan(home, ext):
    """The demo plan: import the external title group, replace the author group."""
    return preview_migration(
        ext,
        home,
        [ImportGroup("01"), ReplaceGroup(target="02", source="04")],
        source_label="ext",
        target_label="home",
    )


def group_signature(record):
    """A comparable snapshot of a record's linkage groups."""
    index = LinkageIndex(record)
    return {
        number: (
            group.anchor.tag,
            tuple(f.tag for f in group.alternates),
            tuple(
                (f.tag, tuple(f.indicators), tuple(f.subfields)) for f in group.fields
            ),
        )
        for number, group in index.groups.items()
    }


def packed_record(group_count):
    """A record whose occurrence numbers 01..group_count are all taken."""
    record = Record()
    for i in range(1, group_count + 1):
        number = f"{i:02d}"
        record.add_field(
            make_field("500", ("6", f"880-{number}"), ("a", f"note {number}")),
            make_field("880", ("6", f"500-{number}"), ("a", f"注記 {number}")),
        )
    return record


class PreviewTestCase(unittest.TestCase):
    def test_preview_leaves_both_records_untouched(self):
        home, ext = home_record(), ext_record()
        home_before, ext_before = str(home), str(ext)
        plan = make_plan(home, ext)
        self.assertEqual(str(home), home_before)
        self.assertEqual(str(ext), ext_before)
        text = str(plan)
        self.assertIn("import ext group 01 (245) as home group 03", text)
        self.assertIn("replace home group 02 (100) with ext group 04 (100)", text)

    def test_repeated_previews_are_deterministic(self):
        home, ext = home_record(), ext_record()
        self.assertEqual(str(make_plan(home, ext)), str(make_plan(home, ext)))

    def test_unknown_selector(self):
        home, ext = home_record(), ext_record()
        with self.assertRaisesRegex(ValueError, "no group with occurrence number 09"):
            preview_migration(ext, home, [ImportGroup("09")])
        # a field of the other record is not selectable here
        with self.assertRaisesRegex(ValueError, "not selectable"):
            preview_migration(ext, home, [ImportGroup(home["245"])])
        # a standalone 880 stays put and cannot be picked
        with self.assertRaisesRegex(ValueError, "not selectable"):
            preview_migration(home, ext, [ImportGroup(home.get_fields("880")[-1])])
        with self.assertRaises(ValueError):
            preview_migration(ext, ext, [ImportGroup("01")])

    def test_invalid_linkage_stops_preview_and_names_the_record(self):
        home, ext = home_record(), ext_record()
        ext.add_field(make_field("880", ("6", "700-09"), ("a", "浮気な880")))
        with self.assertRaises(InvalidLinkage) as caught:
            make_plan(home, ext)
        message = str(caught.exception)
        self.assertIn("ext", message)
        self.assertIn("700-09", message)
        self.assertIn("no such field exists", message)


class ApplyTestCase(unittest.TestCase):
    def test_import_and_replace(self):
        home, ext = home_record(), ext_record()
        make_plan(home, ext).apply()

        # the imported title group was renumbered away from the home subject's 01
        title = home.get_fields("245")[1]
        self.assertEqual(title["6"], "880-03")
        self.assertEqual(title.indicator1, "1")
        self.assertEqual(
            title.get_subfields("a", "b", "c"),
            ["Nihon no min'yō : ", "shūroku to kenkyū /", "Tanaka Kenji."],
        )
        title_alternates = home.get_linked_fields(title, strict=True)
        self.assertEqual([f["6"] for f in title_alternates], ["245-03/$1", "245-03/(N"])
        self.assertEqual(title_alternates[0]["a"], "日本の民謡 : ")

        # the home subject kept 01 and its 880 is not mistaken for a title
        subject_alternates = home.get_linked_fields(home["650"], strict=True)
        self.assertEqual([f["a"] for f in subject_alternates], ["民謡"])

        # the author group was replaced in place, reusing number 02
        self.assertEqual(home.fields[1].tag, "100")
        self.assertEqual(home.fields[1]["a"], "Tanaka, Kenji.")
        self.assertEqual(home.fields[1]["6"], "880-02")
        self.assertEqual(home.fields[2]["6"], "100-02/$1")
        self.assertEqual(home.fields[2]["a"], "田中健二")

        # untouched fields keep values and relative order
        self.assertEqual(
            [f.tag for f in home.fields],
            [
                "001",
                "100",
                "880",
                "245",
                "264",
                "300",
                "650",
                "880",
                "500",
                "880",
                "245",
                "880",
                "880",
            ],
        )
        self.assertEqual(home["500"]["a"], "Gift of the author.")
        standalone = LinkageIndex(home).standalone
        self.assertEqual([f["a"] for f in standalone], ["著者の贈呈本。"])

        # the whole result validates, and the source record is untouched
        self.assertEqual(sorted(LinkageIndex(home).groups), ["01", "02", "03"])
        self.assertEqual(ext["245"]["6"], "880-01")
        self.assertEqual(len(ext.get_fields("880")), 4)

    def test_imported_fields_are_isolated_from_the_source(self):
        home, ext = home_record(), ext_record()
        make_plan(home, ext).apply()
        # mutilate the external record after the write
        ext_title = ext["245"]
        ext_title["a"] = "REWRITTEN"
        ext_title.delete_subfield("6")
        ext.remove_fields("880")
        imported = home.get_fields("245")[1]
        self.assertEqual(imported["6"], "880-03")
        self.assertEqual(imported["a"], "Nihon no min'yō : ")
        self.assertEqual(len(home.get_linked_fields(imported, strict=True)), 2)

    def test_apply_twice_conflicts(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        plan.apply()
        before = str(home)
        with self.assertRaises(MigrationConflict):
            plan.apply()
        self.assertEqual(str(home), before)


class ConflictTestCase(unittest.TestCase):
    def test_source_group_changed_after_preview(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        ext["245"]["a"] = "A different title."
        before = str(home)
        with self.assertRaisesRegex(MigrationConflict, "ext group 01 .* changed"):
            plan.apply()
        self.assertEqual(str(home), before)

    def test_target_group_changed_after_preview(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        home["100"]["a"] = "Tanaka, Kenji, 1948-"
        before = str(home)
        with self.assertRaisesRegex(MigrationConflict, "home group 02 .* changed"):
            plan.apply()
        self.assertEqual(str(home), before)

    def test_target_group_removed_after_preview(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        # remove the whole author group (100 and its 880), cleanly
        home.remove_field(home.fields[1], home.fields[2])
        with self.assertRaisesRegex(MigrationConflict, "no longer exists"):
            plan.apply()

    def test_target_anchor_removed_after_preview_breaks_linkage(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        home.remove_fields("100")  # leaves its 880 dangling
        before = str(home)
        with self.assertRaises(InvalidLinkage):
            plan.apply()
        self.assertEqual(str(home), before)

    def test_assigned_number_snatched_after_preview(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        # someone else claims 03, the number the preview assigned to the import
        home.add_field(
            make_field(
                "700", ("6", "880-03"), ("a", "Latecomer."), indicators=("1", " ")
            ),
            make_field("880", ("6", "700-03"), ("a", "后来者"), indicators=("1", " ")),
        )
        before = str(home)
        with self.assertRaisesRegex(MigrationConflict, "number 03 .* now used by 700"):
            plan.apply()
        self.assertEqual(str(home), before)

    def test_linkage_broken_after_preview(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        home.remove_fields("880")  # breaks every group in home
        before = str(home)
        with self.assertRaises(InvalidLinkage):
            plan.apply()
        self.assertEqual(str(home), before)

    def test_unrelated_note_after_preview_applies_as_planned(self):
        home, ext = home_record(), ext_record()
        plan = make_plan(home, ext)
        home.add_field(make_field("500", ("a", "Second copy on order.")))
        plan.apply()
        self.assertIn("Second copy on order.", [f["a"] for f in home.get_fields("500")])
        self.assertEqual(home.get_fields("245")[1]["6"], "880-03")
        self.assertEqual(home["100"]["a"], "Tanaka, Kenji.")


class NumberAllocationTestCase(unittest.TestCase):
    def test_exhausted_numbers_fail_the_whole_plan(self):
        home, ext = packed_record(99), ext_record()
        before = str(home)
        with self.assertRaises(LinkageNumbersExhausted):
            preview_migration(ext, home, [ImportGroup("01")], target_label="home")
        self.assertEqual(str(home), before)

    def test_one_unplaceable_group_fails_the_whole_batch(self):
        home, ext = packed_record(98), ext_record()
        before = str(home)
        # the first import takes the last free number, the second has nowhere to go
        with self.assertRaises(LinkageNumbersExhausted):
            preview_migration(
                ext,
                home,
                [ImportGroup("01"), ImportGroup("03")],
                target_label="home",
            )
        self.assertEqual(str(home), before)

    def test_replace_of_unlinked_field_gets_a_fresh_number(self):
        home, ext = home_record(), ext_record()
        plan = preview_migration(
            ext, home, [ReplaceGroup(target=home["245"], source="01")]
        )
        plan.apply()
        self.assertEqual(len(home.get_fields("245")), 1)
        self.assertEqual(home["245"]["6"], "880-03")
        self.assertEqual(len(home.get_linked_fields(home["245"], strict=True)), 2)

    def test_import_of_unlinked_field_copies_as_is(self):
        home, ext = home_record(), ext_record()
        plan = preview_migration(ext, home, [ImportGroup(ext["264"])])
        plan.apply()
        self.assertEqual(len(home.get_fields("264")), 2)
        self.assertEqual(sorted(LinkageIndex(home).groups), ["01", "02"])

    def test_replace_with_unlinked_source_frees_the_number(self):
        home, ext = home_record(), ext_record()
        plan = preview_migration(
            ext, home, [ReplaceGroup(target="02", source=ext["264"])]
        )
        plan.apply()
        # the author group is gone, its number freed, the 264 sits in its place
        self.assertNotIn("100", home)
        self.assertEqual(sorted(LinkageIndex(home).groups), ["01"])
        self.assertEqual(home.fields[1].tag, "264")

    def test_number_freed_by_a_replace_is_reused_in_the_same_plan(self):
        home, ext = home_record(), ext_record()
        plan = preview_migration(
            ext,
            home,
            [ReplaceGroup(target="02", source=ext["264"]), ImportGroup("01")],
        )
        plan.apply()
        # the imported title group takes the number the replace just freed
        self.assertEqual(home.get_fields("245")[1]["6"], "880-02")
        self.assertEqual(sorted(LinkageIndex(home).groups), ["01", "02"])

    def test_same_target_replaced_twice_is_rejected(self):
        home, ext = home_record(), ext_record()
        with self.assertRaisesRegex(ValueError, "more than once"):
            preview_migration(
                ext,
                home,
                [
                    ReplaceGroup(target="02", source="04"),
                    ReplaceGroup(target="02", source="01"),
                ],
            )


class StrictWriterTestCase(unittest.TestCase):
    def broken_record(self):
        record = Record()
        record.add_field(make_field("245", ("6", "880-01"), ("a", "Dangling.")))
        return record

    def test_marc_writer_strict_rejects_and_recovers(self):
        good = home_record()
        stream = BytesIO()
        writer = MARCWriter(stream, strict_linkage=True)
        writer.write(good)
        length = len(stream.getvalue())
        with self.assertRaises(InvalidLinkage):
            writer.write(self.broken_record())
        # nothing of the bad record was written, and the writer still works
        self.assertEqual(len(stream.getvalue()), length)
        writer.write(good)
        writer.close(close_fh=False)
        records = list(MARCReader(BytesIO(stream.getvalue()), to_unicode=True))
        self.assertEqual(len(records), 2)

    def test_xml_writer_strict_rejects_and_recovers(self):
        good = home_record()
        stream = BytesIO()
        writer = XMLWriter(stream, strict_linkage=True)
        writer.write(good)
        length = len(stream.getvalue())
        with self.assertRaises(InvalidLinkage):
            writer.write(self.broken_record())
        self.assertEqual(len(stream.getvalue()), length)
        writer.write(good)
        writer.close(close_fh=False)
        self.assertEqual(len(parse_xml_to_array(BytesIO(stream.getvalue()))), 2)

    def test_default_writers_stay_lenient(self):
        marc_stream, xml_stream = BytesIO(), BytesIO()
        MARCWriter(marc_stream).write(self.broken_record())
        xml_writer = XMLWriter(xml_stream)
        xml_writer.write(self.broken_record())
        xml_writer.close(close_fh=False)
        self.assertTrue(len(marc_stream.getvalue()) > 0)
        self.assertIn(b"Dangling.", xml_stream.getvalue())

    def test_strict_write_catches_record_broken_after_apply(self):
        home, ext = home_record(), ext_record()
        make_plan(home, ext).apply()
        # hand-break the imported title's subfield 6 afterwards
        title = home.get_fields("245")[1]
        title.subfields[0] = Subfield("6", "880-3")
        with self.assertRaisesRegex(InvalidLinkage, "malformed"):
            MARCWriter(BytesIO(), strict_linkage=True).write(home)
        with self.assertRaisesRegex(InvalidLinkage, "malformed"):
            XMLWriter(BytesIO(), strict_linkage=True).write(home)


class RoundTripTestCase(unittest.TestCase):
    """A valid record written and read back in both formats keeps its groups."""

    def migrated_home(self):
        home, ext = home_record(), ext_record()
        make_plan(home, ext).apply()
        return home

    def test_binary_round_trip(self):
        home = self.migrated_home()
        stream = BytesIO()
        writer = MARCWriter(stream, strict_linkage=True)
        writer.write(home)
        writer.close(close_fh=False)
        back = next(MARCReader(BytesIO(stream.getvalue()), to_unicode=True))
        self.assertEqual(group_signature(back), group_signature(home))
        title = back.get_fields("245")[1]
        self.assertEqual(
            [f["6"] for f in back.get_linked_fields(title, strict=True)],
            ["245-03/$1", "245-03/(N"],
        )

    def test_xml_round_trip(self):
        home = self.migrated_home()
        stream = BytesIO()
        writer = XMLWriter(stream, strict_linkage=True)
        writer.write(home)
        writer.close(close_fh=False)
        back = parse_xml_to_array(BytesIO(stream.getvalue()))[0]
        self.assertEqual(group_signature(back), group_signature(home))

    def test_both_formats_agree(self):
        home = self.migrated_home()
        marc_stream, xml_stream = BytesIO(), BytesIO()
        marc_writer = MARCWriter(marc_stream, strict_linkage=True)
        marc_writer.write(home)
        marc_writer.close(close_fh=False)
        xml_writer = XMLWriter(xml_stream, strict_linkage=True)
        xml_writer.write(home)
        xml_writer.close(close_fh=False)
        from_marc = next(MARCReader(BytesIO(marc_stream.getvalue()), to_unicode=True))
        from_xml = parse_xml_to_array(BytesIO(xml_stream.getvalue()))[0]
        self.assertEqual(group_signature(from_marc), group_signature(from_xml))


if __name__ == "__main__":
    unittest.main()

# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Tests for writing Unicode records as MARC-8."""

import unicodedata
import unittest
from io import BytesIO

import pymarc
from pymarc import (
    END_OF_FIELD,
    END_OF_RECORD,
    Field,
    Indicators,
    InvalidLinkage,
    MARCReader,
    MARCWriter,
    RawField,
    Record,
    Subfield,
    unicode_to_marc8,
)
from pymarc.exceptions import MARC8EncodingError, RecordTooLargeError
from pymarc.marc8 import marc8_to_unicode

DIRECTORY_ENTRY_LEN = 12


def field(tag, *pairs, indicators=(" ", " ")):
    return Field(
        tag,
        Indicators(*indicators),
        [Subfield(code, value) for code, value in pairs],
    )


def nfc(value):
    return unicodedata.normalize("NFC", value)


def parse_directory(data):
    """Parse a binary record from raw bytes, trusting only the leader.

    Returns (leader, entries, fields) where entries are (tag, length, offset)
    and fields are the raw field byte strings (including the field
    terminator).
    """
    leader = data[0:24]
    base = int(data[12:17])
    record_length = int(data[0:5])
    directory = data[24 : base - 1]
    assert len(directory) % DIRECTORY_ENTRY_LEN == 0
    entries = []
    for start in range(0, len(directory), DIRECTORY_ENTRY_LEN):
        entry = directory[start : start + DIRECTORY_ENTRY_LEN]
        entries.append(
            (
                entry[0:3].decode("ascii"),
                int(entry[3:7]),
                int(entry[7:12]),
            )
        )
    fields = [data[base + offset : base + offset + length] for _t, length, offset in entries]
    return record_length, leader, entries, fields


class MARC8EncodingUnitTest(unittest.TestCase):
    def test_latin_combining_mark_precedes_base(self):
        self.assertEqual(unicode_to_marc8("é"), b"\xe2e")
        self.assertEqual(unicode_to_marc8("e" + chr(0x301)), b"\xe2e")

    def test_nfc_and_nfd_produce_the_same_bytes(self):
        nfd = unicodedata.normalize("NFD", "Léon Łódź")
        self.assertNotEqual(nfd, "Léon Łódź")
        self.assertEqual(unicode_to_marc8("Léon Łódź"), unicode_to_marc8(nfd))

    def test_non_latin_charsets(self):
        self.assertEqual(unicode_to_marc8("Москва"), b"\x1b(NmOSKWA\x1b(B")
        self.assertEqual(unicode_to_marc8("Αθήνα"), b'\x1b(SAk"jpa\x1b(B')
        self.assertEqual(unicode_to_marc8("東"), b"\x1b$1!D&\x1b(B")

    def test_combining_marks_follow_the_base_charset(self):
        # Greek tonos uses the Greek combining acute, not ANSEL's.
        self.assertEqual(unicode_to_marc8("ά"), b'\x1b(S"a\x1b(B')

    def test_orphan_combining_mark_is_an_error(self):
        with self.assertRaises(MARC8EncodingError) as ctx:
            unicode_to_marc8(chr(0x301))
        self.assertEqual(ctx.exception.character, chr(0x301))
        self.assertEqual(ctx.exception.char_offset, 0)
        self.assertIn("without a base", str(ctx.exception))

    def test_unmappable_character_is_an_error(self):
        with self.assertRaises(MARC8EncodingError) as ctx:
            unicode_to_marc8("a☃")
        self.assertEqual(ctx.exception.character, "☃")
        self.assertEqual(ctx.exception.char_offset, 1)

    def test_hangul_is_composed_before_encoding(self):
        self.assertEqual(
            unicode_to_marc8("한").hex(), "1b24316f5c651b2842"
        )
        self.assertEqual(
            unicode_to_marc8("한"), unicode_to_marc8("한")
        )

    def test_marks_are_reordered_canonically(self):
        # acute (class 230) then dot below (class 220) is not canonical order;
        # MARC-8 output and the decoded text normalise the same.
        text = "a" + chr(0x301) + chr(0x323)
        self.assertEqual(
            unicode_to_marc8(text), unicode_to_marc8(unicodedata.normalize("NFD", "ạ́"))
        )

    def test_reserved_controls_are_an_error(self):
        for control in ("\x1b", "\x1d", "\x1e", "\x1f"):
            with self.assertRaises(MARC8EncodingError):
                unicode_to_marc8(f"a{control}b")

    def test_no_utf8_fallback(self):
        """An unmappable character never leaks through as UTF-8 bytes."""
        with self.assertRaises(MARC8EncodingError):
            unicode_to_marc8("é☃")


class MARC8RecordSerializationTest(unittest.TestCase):
    def mixed_record(self):
        record = Record()
        record.add_field(Field("001", data="ocm12345"))
        record.add_field(
            field("245", ("a", "Léon: "), ("b", "Москва 東京 /"), indicators=("1", "0"))
        )
        record.add_field(field("650", ("a", "Poésie"), ("z", "Ελλάδα.")))
        record.add_field(field("880", ("a", "שלום"), ("b", "العربية")))
        return record

    def test_leader_position_9_is_blank(self):
        record = self.mixed_record()
        data = record.as_marc(encoding="marc-8")
        self.assertEqual(data[9:10], b" ")
        # the source leader is untouched
        self.assertNotEqual(str(record.leader)[9], "\x00")

    def test_leader_and_directory_match_actual_bytes(self):
        data = self.mixed_record().as_marc(encoding="marc-8")
        record_length, leader, entries, fields = parse_directory(data)
        self.assertEqual(record_length, len(data))
        self.assertEqual(leader[0:5].decode(), str(len(data)).zfill(5))
        base = int(leader[12:17])
        self.assertEqual(base, 24 + len(entries) * DIRECTORY_ENTRY_LEN + 1)
        self.assertEqual(data[base - 1], ord(END_OF_FIELD))
        self.assertEqual(data[-1], ord(END_OF_RECORD))
        self.assertEqual([entry[0] for entry in entries], ["001", "245", "650", "880"])
        offset = 0
        for tag, length, entry_offset in entries:
            self.assertEqual(entry_offset, offset)
            start = base + offset
            chunk = data[start : start + length]
            self.assertEqual(len(chunk), length)
            self.assertEqual(chunk[-1], ord(END_OF_FIELD))
            offset += length
        self.assertEqual(sum(entry[1] for entry in entries) + 1, len(data) - base)
        # each entry points at exactly its own field
        self.assertTrue(fields[1].startswith(b"10\x1fa"))
        self.assertIn(b"\x1b(NmOSKWA", data)

    def test_round_trip_preserves_fields_and_text(self):
        original = self.mixed_record()
        data = original.as_marc(encoding="marc-8")
        back = next(MARCReader(BytesIO(data), to_unicode=True))
        self.assertEqual([f.tag for f in back.fields], [f.tag for f in original.fields])
        self.assertEqual(
            [f.indicators for f in back.fields], [f.indicators for f in original.fields]
        )
        for before, after in zip(original.fields, back.fields):
            if before.control_field:
                self.assertEqual(before.data, after.data)
            else:
                self.assertEqual(
                    [(s.code, s.value) for s in before.subfields],
                    [(s.code, s.value) for s in after.subfields],
                )
        self.assertEqual(back["245"].value(), original["245"].value())

    def test_default_output_unchanged(self):
        record = self.mixed_record()
        default = record.as_marc()
        self.assertEqual(default[9:10], b"a")
        # the explicit UTF-8 output still reads back to the same text
        back = next(MARCReader(BytesIO(default), to_unicode=True))
        self.assertEqual(back["880"]["a"], "שלום")

    def test_source_record_is_not_mutated(self):
        record = self.mixed_record()
        leader_before = str(record.leader)
        fields_before = [
            (f.tag, f.indicators, [(s.code, s.value) for s in f.subfields])
            for f in record.fields
        ]
        record.as_marc(encoding="marc-8")
        self.assertEqual(str(record.leader), leader_before)
        self.assertEqual(
            [
                (f.tag, f.indicators, [(s.code, s.value) for s in f.subfields])
                for f in record.fields
            ],
            fields_before,
        )

    def test_direct_serialization_matches_writer(self):
        record = self.mixed_record()
        stream = BytesIO()
        writer = MARCWriter(stream, encoding="marc-8")
        writer.write(record)
        writer.close(close_fh=False)
        self.assertEqual(stream.getvalue(), record.as_marc(encoding="marc-8"))

    def test_field_as_marc_marc8(self):
        marc_field = field("245", ("a", "Léon"), indicators=("1", "0"))
        self.assertEqual(marc_field.as_marc(encoding="marc-8"), b"10\x1faL\xe2eon\x1e")

    def test_explicit_leader_is_preserved_except_position_9(self):
        record = Record(leader="00000nam a2200000   4500")
        # Record.__init__ normalises leader[10:12] to "22"; compare the
        # bytes we actually set rather than the raw input.
        record.leader = "00000nam a2200000   4500"
        record.add_field(field("245", ("a", "Léon")))
        data = record.as_marc(encoding="marc-8")
        self.assertEqual(data[5:9], b"nam ")
        self.assertEqual(data[9:10], b" ")
        self.assertEqual(data[17:24], b"   4500")


class MARC8WriterTest(unittest.TestCase):
    def test_writer_rejects_raw_field(self):
        record = Record()
        # build a valid RawField rather than the placeholder above
        raw = RawField(
            tag="245",
            indicators=Indicators("1", "0"),
            subfields=[Subfield("a", b"raw")],
        )
        record.add_field(raw)
        stream = BytesIO()
        writer = MARCWriter(stream, encoding="marc-8")
        with self.assertRaises(MARC8EncodingError) as ctx:
            writer.write(record)
        self.assertEqual(stream.getvalue(), b"")
        self.assertIn("RawField", str(ctx.exception))

    def test_control_field_must_be_ascii(self):
        record = Record()
        record.add_field(Field("001", data="café"))
        with self.assertRaises(MARC8EncodingError) as ctx:
            record.as_marc(encoding="marc-8")
        self.assertEqual(ctx.exception.tag, "001")
        self.assertEqual(ctx.exception.char_offset, 3)
        # plain ASCII control fields still work
        record.fields[0] = Field("001", data="ocm12345")
        self.assertIn(b"ocm12345", record.as_marc(encoding="marc-8"))

    def test_failed_record_leaves_writer_usable(self):
        good = Record()
        good.add_field(field("245", ("a", "Léon"), indicators=("1", "0")))
        bad = Record()
        bad.add_field(field("245", ("a", "snowman ☃"), indicators=("1", "0")))

        stream = BytesIO()
        writer = MARCWriter(stream, encoding="marc-8")
        writer.write(good)
        written = stream.getvalue()
        with self.assertRaises(MARC8EncodingError):
            writer.write(bad)
        self.assertEqual(stream.getvalue(), written)
        writer.write(good)
        writer.close(close_fh=False)

        records = list(MARCReader(BytesIO(stream.getvalue()), to_unicode=True))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["245"]["a"], "Léon")

    def test_error_reports_tag_and_subfield(self):
        record = Record()
        record.add_field(field("245", ("a", "ok"), ("b", "bad ☃"), indicators=("0", "0")))
        record.add_field(field("245", ("a", "☃ again"), indicators=("0", "0")))
        with self.assertRaises(MARC8EncodingError) as ctx:
            record.as_marc(encoding="marc-8")
        err = ctx.exception
        self.assertEqual((err.tag, err.tag_occurrence), ("245", 1))
        self.assertEqual((err.subfield_index, err.subfield_code), (1, "b"))
        self.assertEqual(err.char_offset, 4)
        message = str(err)
        self.assertIn("245", message)
        self.assertIn("subfield #1", message)
        self.assertIn("$b", message)
        self.assertIn("character offset 4", message)

    def test_raw_field_round_trip_still_works_for_utf8(self):
        raw = RawField(
            tag="245",
            indicators=Indicators("1", "0"),
            subfields=[Subfield("a", b"raw \xe9 bytes")],
        )
        record = Record()
        record.add_field(raw)
        data = record.as_marc()
        back = next(MARCReader(BytesIO(data), to_unicode=False))
        self.assertIsInstance(back["245"], RawField)
        self.assertEqual(back["245"]["a"], b"raw \xe9 bytes")

    def test_strict_linkage_combined_with_marc8(self):
        good = Record()
        good.add_field(field("245", ("a", "Léon"), indicators=("1", "0")))
        stream = BytesIO()
        writer = MARCWriter(stream, encoding="marc-8", strict_linkage=True)
        writer.write(good)
        length = len(stream.getvalue())

        broken = Record()
        broken.add_field(field("245", ("6", "880-01"), ("a", "Léon")))
        with self.assertRaises(InvalidLinkage):
            writer.write(broken)
        self.assertEqual(len(stream.getvalue()), length)

        unencodable = Record()
        unencodable.add_field(field("245", ("a", "☃"), indicators=("1", "0")))
        with self.assertRaises(MARC8EncodingError):
            writer.write(unencodable)
        self.assertEqual(len(stream.getvalue()), length)

        writer.write(good)
        writer.close(close_fh=False)
        self.assertEqual(len(list(MARCReader(BytesIO(stream.getvalue())))), 2)


class MARC8LengthLimitsTest(unittest.TestCase):
    def test_field_length_limit(self):
        record = Record()
        record.add_field(field("245", ("a", "a" * 10000)))
        with self.assertRaises(RecordTooLargeError):
            record.as_marc(encoding="marc-8")
        # 9998 bytes of payload + indicators + subfield markers + terminator
        record = Record()
        record.add_field(Field("245", Indicators(" ", " "), [Subfield("a", "a" * 9994)]))
        data = record.as_marc(encoding="marc-8")
        self.assertEqual(int(data[0:5]), len(data))

    def test_offset_limit_zero_padded_entries_stay_aligned(self):
        record = Record()
        record.add_field(Field("001", data="x"))
        for _ in range(300):
            record.add_field(field("500", ("a", "y" * 300)))
        data = record.as_marc(encoding="marc-8")
        _length, _leader, entries, _fields = parse_directory(data)
        self.assertEqual(len(entries), 301)
        for tag, _length, offset in entries:
            self.assertGreaterEqual(offset, 0)
            self.assertLessEqual(offset, 99999)

    def test_record_length_limit(self):
        record = Record()
        for _ in range(1200):
            record.add_field(field("500", ("a", "y" * 80)))
        with self.assertRaises(RecordTooLargeError):
            record.as_marc(encoding="marc-8")


class MARC8BoundaryTest(unittest.TestCase):
    def test_escape_state_does_not_leak_between_subfields(self):
        record = Record()
        record.add_field(
            field(
                "245",
                ("a", "Москва"),
                ("b", "plain"),
                ("c", "東"),
                ("d", "plain again"),
                indicators=("1", "0"),
            )
        )
        data = record.as_marc(encoding="marc-8")
        back = next(MARCReader(BytesIO(data), to_unicode=True))
        self.assertEqual(
            [s.value for s in back["245"].subfields],
            ["Москва", "plain", "東", "plain again"],
        )
        # every Cyrillic/EACC run is explicitly closed
        self.assertNotIn(b"\x1b(N plain", data)

    def test_escape_state_does_not_leak_between_records(self):
        first = Record()
        first.add_field(field("245", ("a", "Москва"), indicators=("1", "0")))
        second = Record()
        second.add_field(field("245", ("a", "plain"), indicators=("1", "0")))
        first_data = first.as_marc(encoding="marc-8")
        second_data = second.as_marc(encoding="marc-8")
        # serializing in either order is independent
        self.assertEqual(second.as_marc(encoding="marc-8"), second_data)
        combined = first_data + second_data
        records = list(MARCReader(BytesIO(combined), to_unicode=True))
        self.assertEqual(records[0]["245"]["a"], "Москва")
        self.assertEqual(records[1]["245"]["a"], "plain")

    def test_combining_marks_do_not_leak(self):
        record = Record()
        record.add_field(field("245", ("a", "e" + chr(0x301)), ("b", "x"), indicators=("0", "0")))
        back = next(MARCReader(BytesIO(record.as_marc(encoding="marc-8")), to_unicode=True))
        self.assertEqual(back["245"]["a"], "é")
        self.assertEqual(back["245"]["b"], "x")

    def test_consecutive_combining_marks(self):
        # a + dot below + acute; NFC keeps both marks
        text = "a" + chr(0x323) + chr(0x301)
        record = Record()
        record.add_field(field("245", ("a", text), indicators=("0", "0")))
        data = record.as_marc(encoding="marc-8")
        back = next(MARCReader(BytesIO(data), to_unicode=True))
        self.assertEqual(nfc(back["245"]["a"]), nfc(text))
        # both marks precede the base character
        self.assertIn(b"\xf2\xe2a", data)


class MARC8MixedScriptsTest(unittest.TestCase):
    def test_latin_to_cyrillic_to_east_asian(self):
        record = Record()
        record.add_field(
            field(
                "245",
                ("a", "Léon"),
                ("b", "Москва"),
                ("c", "東京"),
                indicators=("1", "0"),
            )
        )
        record.add_field(field("650", ("a", "Ελληνικά"), ("a", "العربية"), ("a", "שלום")))
        data = record.as_marc(encoding="marc-8")
        back = next(MARCReader(BytesIO(data), to_unicode=True))
        self.assertEqual(
            [s.value for s in back["245"].subfields], ["Léon", "Москва", "東京"]
        )
        self.assertEqual(
            [s.value for s in back["650"].subfields],
            ["Ελληνικά", "العربية", "שלום"],
        )

    def test_directory_offsets_account_for_escapes(self):
        plain = Record()
        plain.add_field(field("245", ("a", "aaaa"), indicators=("1", "0")))
        escaped = Record()
        escaped.add_field(field("245", ("a", "東京"), indicators=("1", "0")))
        plain_data = plain.as_marc(encoding="marc-8")
        escaped_data = escaped.as_marc(encoding="marc-8")
        # each East Asian character is three bytes plus designations
        self.assertGreater(len(escaped_data), len(plain_data))
        length, _leader, entries, _fields = parse_directory(escaped_data)
        self.assertEqual(length, len(escaped_data))
        self.assertEqual(entries[0][1], int(escaped_data[27:31].decode()))

    def test_nfc_nfd_records_agree(self):
        nfc_record = Record()
        nfc_record.add_field(field("245", ("a", "Léon Łódź"), indicators=("1", "0")))
        nfd_record = Record()
        nfd_record.add_field(
            field(
                "245",
                ("a", unicodedata.normalize("NFD", "Léon Łódź")),
                indicators=("1", "0"),
            )
        )
        self.assertEqual(
            nfc_record.as_marc(encoding="marc-8"),
            nfd_record.as_marc(encoding="marc-8"),
        )


class MARC8ReaderCorpusTest(unittest.TestCase):
    def test_corpus_values_reencode_and_decode(self):
        """Every table character we can emit decodes back through the reader."""
        from pymarc import marc8_mapping

        bases = {0x32: "א", 0x33: "ب", 0x34: "ب", 0x53: "α", 0x45: "a"}
        for charset, table in marc8_mapping.CODESETS.items():
            for code, (codepoint, combining) in table.items():
                char = chr(codepoint)
                if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
                    continue
                if combining:
                    if charset not in bases:
                        continue
                    text = bases[charset] + char
                elif char in ("‌", "‍"):
                    continue  # decoder drops them as control bytes
                else:
                    text = char
                with self.subTest(charset=hex(charset), code=hex(code)):
                    data = unicode_to_marc8(text)
                    self.assertEqual(nfc(marc8_to_unicode(data)), nfc(text))


if __name__ == "__main__":
    unittest.main()

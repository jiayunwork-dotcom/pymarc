# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Regression tests for corrupted-batch recovery and short-read handling.

All inputs are synthetic records produced through the public pymarc API,
with damaged regions spliced between them. No external catalog service is
involved.
"""

import unittest
from typing import List, Optional, Tuple

import pymarc
from pymarc import Field, Indicators, MARCReader, Record, Subfield
from pymarc.exceptions import (
    EndOfRecordNotFound,
    FieldOutOfBounds,
    FramingError,
    RecordLengthInvalid,
    RecoveryLimitExceeded,
    TruncatedRecord,
)
from pymarc.field import RawField
from pymarc.reader import READ_CHUNK_SIZE


def make_record(control: str, title: str = "title") -> bytes:
    """Build a MARC21 frame carrying a 001 control field via the public API."""
    record = Record()
    record.add_field(Field("001", data=control))
    record.add_field(
        Field(
            "245",
            indicators=Indicators("0", "1"),
            subfields=[Subfield(code="a", value=title)],
        )
    )
    return record.as_marc()


class ChunkedStream:
    """A synchronous binary stream without seek/tell and short reads.

    ``read(n)`` returns at most ``cap`` bytes (and sometimes fewer when
    ``vary`` is set), never more than requested. Only an empty read marks
    EOF, mirroring the contract described for non-blocking-free streams.
    """

    def __init__(self, data: bytes, cap: int = 3, vary: bool = False):
        self.data = bytearray(data)
        self.cap = cap
        self.vary = vary
        self.calls = 0
        self.max_request = 0
        self.reads_after_empty = 0
        self._empty_seen = False

    def read(self, n: int) -> bytes:
        if self._empty_seen:
            self.reads_after_empty += 1
        self.calls += 1
        self.max_request = max(self.max_request, n)
        size = min(n, self.cap, len(self.data))
        if self.vary and size > 1:
            size = min(size, 1 + (self.calls % self.cap))
        chunk = bytes(self.data[:size])
        del self.data[:size]
        if not chunk:
            self._empty_seen = True
        return chunk


Item = Tuple[Optional[str], Optional[Tuple[int, int]], Optional[str]]


def collect(reader: MARCReader) -> List[Item]:
    """Return (control-id-or-None, byte range, exception name) per item."""
    out: List[Item] = []
    for record in reader:
        if record is None:
            exc = reader.current_exception
            out.append(
                (
                    None,
                    reader.current_range,
                    type(exc).__name__ if exc is not None else None,
                )
            )
        else:
            out.append((record["001"].data, reader.current_range, None))
    return out


def good_ids(items: List[Item]) -> List[str]:
    return [item[0] for item in items if item[0] is not None]


class ShortReadTest(unittest.TestCase):
    """Valid records must survive arbitrarily short reads."""

    def test_clean_batch_one_to_three_byte_reads(self):
        batch = make_record("a1") + make_record("a2") + make_record("a3")
        for cap in (1, 2, 3):
            stream = ChunkedStream(batch, cap=cap, vary=True)
            reader = MARCReader(stream)
            self.assertEqual(good_ids(collect(reader)), ["a1", "a2", "a3"])
            # no read may ask for more than the bounded chunk size
            self.assertLessEqual(stream.max_request, READ_CHUNK_SIZE)
            self.assertEqual(stream.reads_after_empty, 0)

    def test_bytes_input_equivalent_to_stream(self):
        batch = make_record("b1") + make_record("b2")
        from_stream = collect(MARCReader(ChunkedStream(batch, cap=2)))
        from_bytes = collect(MARCReader(batch))
        self.assertEqual(from_stream, from_bytes)

    def test_empty_input(self):
        self.assertEqual(collect(MARCReader(b"")), [])
        self.assertEqual(collect(MARCReader(ChunkedStream(b""))), [])

    def test_rawfield_and_decode_options_preserved(self):
        frame = make_record("r1")
        # RawField path (to_unicode=False) over short reads
        reader = MARCReader(ChunkedStream(frame, cap=2), to_unicode=False)
        record = next(reader)
        self.assertIsInstance(record["001"], RawField)
        self.assertIsInstance(record["001"].data, bytes)
        # unicode path with replace handling
        reader = MARCReader(frame, to_unicode=True, force_utf8=True)
        self.assertEqual(next(reader)["245"]["a"], "title")


class NonPermissiveTest(unittest.TestCase):
    """Without permissive mode a fatal error ends iteration as before."""

    def test_bad_length_header_stops_iteration(self):
        good1 = make_record("g1")
        batch = good1 + b"XXXXX" + make_record("g2")
        reader = MARCReader(ChunkedStream(batch, cap=3))
        first = next(reader)
        self.assertEqual(first["001"].data, "g1")
        bad = next(reader)
        self.assertIsNone(bad)
        self.assertIsInstance(reader.current_exception, RecordLengthInvalid)
        # historical behavior: the five bad header bytes form the chunk
        self.assertEqual(reader.current_chunk, b"XXXXX")
        self.assertEqual(reader.current_range,
                         (len(good1), len(good1) + 5))
        # repeated next() ends cleanly without touching the stream again
        stream = reader.file_handle
        calls = stream.calls
        for _ in range(3):
            with self.assertRaises(StopIteration):
                next(reader)
        self.assertEqual(stream.calls, calls)

    def test_bad_tail_reported_once(self):
        batch = make_record("g1") + b"00127abc"
        reader = MARCReader(ChunkedStream(batch, cap=2))
        self.assertEqual(next(reader)["001"].data, "g1")
        self.assertIsNone(next(reader))
        self.assertIsInstance(reader.current_exception, TruncatedRecord)
        with self.assertRaises(StopIteration):
            next(reader)

    def test_partial_length_header(self):
        reader = MARCReader(b"0012")
        items = collect(reader)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0][0])
        self.assertIsInstance(reader.current_exception, TruncatedRecord)
        self.assertEqual(reader.current_chunk, b"0012")


class PermissiveRecoveryTest(unittest.TestCase):
    """permissive=True recovers good records after a damaged region."""

    def test_bad_length_header_between_good_records(self):
        good1, good2 = make_record("g1"), make_record("g2")
        junk = b"XXXXX some garbage padding padding"
        batch = good1 + junk + good2
        reader = MARCReader(ChunkedStream(batch, cap=3), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g2"])
        none_items = [item for item in items if item[0] is None]
        self.assertEqual(len(none_items), 1)
        _, rng, exc_name = none_items[0]
        self.assertEqual(rng, (len(good1), len(good1) + len(junk)))
        self.assertEqual(exc_name, "RecordLengthInvalid")

    def test_declared_length_too_short(self):
        good1, good3 = make_record("g1"), make_record("g3")
        # a 30-byte prefix of a real record, with its real length header,
        # cannot be a complete frame
        fragment = make_record("g2")[:30]
        batch = good1 + fragment + good3
        reader = MARCReader(ChunkedStream(batch, cap=2), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g3"])
        none_items = [item for item in items if item[0] is None]
        self.assertEqual(len(none_items), 1)
        self.assertEqual(none_items[0][2], "EndOfRecordNotFound")

    def test_declared_length_too_long_within_stream(self):
        good1, good3 = make_record("g1"), make_record("g3")
        broken = bytearray(make_record("g2"))
        broken[0:5] = b"00100"  # real frame is shorter; lands inside g3
        batch = good1 + bytes(broken) + good3
        reader = MARCReader(ChunkedStream(batch, cap=2), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g3"])

    def test_declared_length_past_eof_is_bad_tail(self):
        good1 = make_record("g1")
        broken = bytearray(make_record("g2"))
        broken[0:5] = b"99999"
        reader = MARCReader(ChunkedStream(good1 + bytes(broken), cap=2),
                            permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1"])
        self.assertEqual(items[-1][0], None)
        self.assertEqual(items[-1][2], "TruncatedRecord")
        with self.assertRaises(StopIteration):
            next(reader)

    def test_fake_leader_and_fake_terminator(self):
        good1, good2 = make_record("g1"), make_record("g2")
        junk = b"00120cam  2200037   4500" + b"\x1d" + b"junkjunkjunk"
        batch = good1 + junk + good2
        reader = MARCReader(ChunkedStream(batch, cap=3), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g2"])
        # the junk's five-digit header and embedded 0x1D did not pass as a
        # record; exactly one damaged region was reported
        self.assertEqual(sum(1 for item in items if item[0] is None), 1)

    def test_repeated_bad_regions_each_reported_once(self):
        g1, g2, g3 = make_record("g1"), make_record("g2"), make_record("g3")
        batch = g1 + b"XXXXX" + g2 + b"YYYYY" + g3
        reader = MARCReader(ChunkedStream(batch, cap=3), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g2", "g3"])
        nones = [item for item in items if item[0] is None]
        self.assertEqual(len(nones), 2)
        self.assertEqual(nones[0][1], (len(g1), len(g1) + 5))
        second_start = len(g1) + 5 + len(g2)
        self.assertEqual(nones[1][1], (second_start, second_start + 5))

    def test_bad_tail_at_true_eof_reported_once(self):
        g1 = make_record("g1")
        batch = g1 + b"00127" + b"partial-garbage"
        reader = MARCReader(ChunkedStream(batch, cap=3), permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1"])
        self.assertEqual(items[-1][0], None)
        self.assertIsInstance(reader.current_exception, FramingError)
        with self.assertRaises(StopIteration):
            next(reader)

    def test_damaged_region_byte_range_and_no_swallowed_record(self):
        g1, g2 = make_record("g1"), make_record("g2")
        junk = b"!" * 25
        batch = g1 + junk + g2
        reader = MARCReader(batch, permissive=True)
        none_range = None
        none_exc = None
        for record in reader:
            if record is None:
                none_range = reader.current_range
                none_exc = reader.current_exception
        self.assertEqual(none_range, (len(g1), len(g1) + len(junk)))
        # the exception object carries the identical half-open range
        self.assertEqual(none_exc.byte_range,
                         (len(g1), len(g1) + len(junk)))

    def test_non_ascii_content_round_trip(self):
        record = Record()
        record.add_field(Field("001", data="u1"))
        record.add_field(
            Field("245", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "日本語のタイトル")])
        )
        uni = record.as_marc()
        reader = MARCReader(ChunkedStream(uni + make_record("g2"), cap=1),
                            permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["u1", "g2"])


class FrameValidationTest(unittest.TestCase):
    """Record and reader reject the same out-of-bounds frames."""

    def _tampered_record(self):
        frame = bytearray(make_record("g2"))
        # first directory entry lives at bytes 24..36; its length is 27:31
        frame[27:31] = b"9999"
        return bytes(frame)

    def test_record_constructor_rejects_oob_directory(self):
        with self.assertRaises(FieldOutOfBounds):
            Record(self._tampered_record())

    def test_reader_rejects_oob_directory_but_continues(self):
        g1, g3 = make_record("g1"), make_record("g3")
        bad = self._tampered_record()
        reader = MARCReader(ChunkedStream(g1 + bad + g3, cap=3),
                            permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g3"])
        none_item = [item for item in items if item[0] is None][0]
        self.assertEqual(none_item[2], "FieldOutOfBounds")

    def test_no_terminator_bytes_leak_into_field_values(self):
        g1, g3 = make_record("g1"), make_record("g3")
        reader = MARCReader(g1 + self._tampered_record() + g3,
                            permissive=True)
        for record in reader:
            if record is None:
                continue
            for field in record.fields:
                value = field.data if field.control_field else str(
                    field.subfields)
                self.assertNotIn("\x1d", value)
                self.assertNotIn("\x1e", value)

    def test_directory_order_may_differ_from_physical_order(self):
        record = Record()
        record.add_field(Field("001", data="x"))
        record.add_field(
            Field("245", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "Title")]))
        record.add_field(
            Field("650", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "Topic")]))
        raw = record.as_marc()
        base = int(raw[12:17])
        entries = [raw[24 + i:24 + i + 12]
                   for i in range(0, base - 25, 12)]
        # reorder directory entries, leave physical field data untouched
        reordered = raw[:24] + entries[0] + entries[2] + entries[1] + \
            b"\x1e" + raw[base:]
        parsed = Record(reordered)
        self.assertEqual([f.tag for f in parsed.fields], ["001", "650", "245"])

    def test_utf8_multibyte_lengths_are_byte_lengths(self):
        record = Record()
        record.add_field(Field("001", data="m1"))
        record.add_field(
            Field("245", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "日本語")]))
        frame = record.as_marc()
        parsed = Record(frame)
        self.assertEqual(parsed["245"]["a"], "日本語")
        # same frame accepted when read one byte at a time
        reader = MARCReader(ChunkedStream(frame, cap=1))
        self.assertEqual(next(reader)["245"]["a"], "日本語")

    def test_missing_field_terminator_rejected_by_strict_scan(self):
        record = Record()
        record.add_field(Field("001", data="x1"))
        record.add_field(
            Field("245", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "hi")]))
        raw = bytearray(record.as_marc())
        base = int(raw[12:17])
        # overwrite the first field terminator with an ordinary byte
        terminator = raw.index(b"\x1e", base)
        raw[terminator] = ord("A")
        # the in-range mis-sized frame keeps the historical lenient behavior
        self.assertEqual([f.tag for f in Record(bytes(raw)).fields],
                         ["001", "245"])
        # but recovery scanning must not accept it as an anchor frame
        from pymarc.frame import probe_candidate
        self.assertIsNone(probe_candidate(bytes(raw)))


class ContentVsFramingTest(unittest.TestCase):
    """A sound frame with bad text is a content error, not a bad boundary."""

    def test_utf8_decode_failure_keeps_boundary(self):
        record = Record()
        record.add_field(Field("001", data="c1"))
        record.add_field(
            Field("245", indicators=Indicators(" ", " "),
                  subfields=[Subfield("a", "ok")]))
        frame = bytearray(record.as_marc())
        # inject an invalid UTF-8 byte into the field area
        terminator = frame.index(b"\x1e")
        frame[terminator - 1] = 0xFF
        g2 = make_record("g2")
        reader = MARCReader(bytes(frame) + g2, permissive=True,
                           utf8_handling="strict")
        first = next(reader)
        self.assertIsNone(first)
        self.assertIsInstance(reader.current_exception, UnicodeDecodeError)
        self.assertEqual(reader.current_range, (0, len(bytes(frame))))
        # the next record follows immediately; no pseudo-record was scanned
        second = next(reader)
        self.assertEqual(second["001"].data, "g2")

    def test_framing_error_class_is_distinct_from_content(self):
        # every boundary-related exception is a FramingError; a plain decode
        # error (content) is not
        self.assertTrue(issubclass(RecoveryLimitExceeded, FramingError))
        self.assertTrue(issubclass(TruncatedRecord, FramingError))
        self.assertFalse(issubclass(UnicodeDecodeError, FramingError))

    def test_utf8_replace_handling_survives_short_reads(self):
        record = Record()
        record.add_field(Field("001", data="c2"))
        record.add_field(
            Field("245", indicators=Indicators("0", "1"),
                  subfields=[Subfield("a", "café")]))
        frame = bytearray(record.as_marc())
        base = int(frame[12:17])
        subfield = frame.index(b"\x1f", base)
        frame[subfield + 2] = 0xFF  # invalid byte inside subfield text
        strict = MARCReader(bytes(frame), utf8_handling="strict")
        self.assertIsNone(next(strict))
        self.assertIsInstance(strict.current_exception, UnicodeDecodeError)
        replaced = MARCReader(ChunkedStream(bytes(frame), cap=2),
                              utf8_handling="replace")
        parsed = next(replaced)
        self.assertIn("�", parsed["245"]["a"])


class RecoveryLimitTest(unittest.TestCase):
    def test_validation_rejects_bad_arguments(self):
        for bad in (0, -1, 1.5, True, False, "100"):
            with self.assertRaises((TypeError, ValueError)):
                MARCReader(b"", recovery_limit=bad)

    def test_limit_exceeded_reports_and_ends(self):
        g1 = make_record("g1")
        junk = b"J" * 5000 + b"0" * 3000
        g2 = make_record("g2")
        reader = MARCReader(g1 + junk + g2, permissive=True,
                            recovery_limit=1000)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1"])
        self.assertIsInstance(reader.current_exception,
                              RecoveryLimitExceeded)
        rng = reader.current_range
        self.assertEqual(rng[0], len(g1))
        self.assertGreaterEqual(rng[1] - rng[0], 1000)
        with self.assertRaises(StopIteration):
            next(reader)

    def test_limit_half_open_window(self):
        g1, g2 = make_record("g1"), make_record("g2")
        junk = b"Q" * 100
        batch = g1 + junk + g2  # g2 starts exactly len(g1) + 100
        # a window reaching the record start recovers it
        okay = MARCReader(batch, permissive=True, recovery_limit=101)
        self.assertEqual(good_ids(collect(okay)), ["g1", "g2"])
        # a window ending right before the record start cannot
        blocked = MARCReader(batch, permissive=True, recovery_limit=100)
        self.assertEqual(good_ids(collect(blocked)), ["g1"])
        self.assertIsInstance(blocked.current_exception,
                              RecoveryLimitExceeded)

    def test_long_damage_keeps_read_requests_bounded(self):
        g1, g2 = make_record("g1"), make_record("g2")
        junk = b"Z" * 200000
        stream = ChunkedStream(g1 + junk + g2, cap=4096)
        reader = MARCReader(stream, permissive=True)
        items = collect(reader)
        self.assertEqual(good_ids(items), ["g1", "g2"])
        self.assertLessEqual(stream.max_request, READ_CHUNK_SIZE)
        self.assertEqual(stream.reads_after_empty, 0)


class BlockSizeInvarianceTest(unittest.TestCase):
    """Results and reported ranges must not depend on read chunk size."""

    def test_same_batch_different_chunk_sizes(self):
        g1, g2, g3 = make_record("g1"), make_record("g2"), make_record("g3")
        batch = (g1 + b"XXXXX garbage" + g2 + b"00006 " + g3 +
                 b"tail-junk")
        reference = collect(MARCReader(batch, permissive=True))
        for cap in (1, 2, 3, 7, 64, 1000):
            stream = ChunkedStream(batch, cap=cap, vary=True)
            got = collect(MARCReader(stream, permissive=True))
            self.assertEqual(got, reference, f"cap={cap} changed results")

    def test_ranges_are_absolute_and_contiguous(self):
        g1, g2 = make_record("g1"), make_record("g2")
        junk = b"@" * 17
        batch = g1 + junk + g2
        items = collect(MARCReader(ChunkedStream(batch, cap=2),
                                   permissive=True))
        cursor = 0
        for _, rng, _ in items:
            self.assertEqual(rng[0], cursor)
            cursor = rng[1]
        self.assertEqual(cursor, len(batch))


if __name__ == "__main__":
    unittest.main()

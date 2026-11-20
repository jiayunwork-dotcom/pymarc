#!/usr/bin/env python3
"""Demonstrate corrupted-batch handling in MARCReader.

Run with::

    python3 examples/recovery_demo.py

The batch is fully synthetic: two good MARC21 records (each with a 001
control number) with a damaged region spliced between them. The same bytes
are fed to the reader three ways:

1. default mode -- iteration stops at the first fatal framing error;
2. ``permissive=True`` -- the damaged region is reported once as ``None``
   with its byte range, and the good record after it is still delivered;
3. ``permissive=True`` through a stream that returns at most three bytes
   per ``read()`` (no seek/tell), showing short reads never truncate a
   valid record.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymarc import Field, Indicators, MARCReader, Record, Subfield  # noqa: E402


def make_record(control: str, title: str) -> bytes:
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


class ThreeByteStream:
    """A read-only binary stream: no seek/tell, at most 3 bytes per read."""

    def __init__(self, data: bytes):
        self._data = bytearray(data)

    def read(self, n: int) -> bytes:
        size = min(n, 3, len(self._data))
        chunk = bytes(self._data[:size])
        del self._data[:size]
        return chunk  # b"" signals EOF; a short chunk is normal, not an error


def summarize(reader: MARCReader):
    for record in reader:
        if record is None:
            start, end = reader.current_range
            print(
                f"  None  bytes [{start}:{end}]  "
                f"{type(reader.current_exception).__name__}: "
                f"{reader.current_exception}"
            )
        else:
            start, end = reader.current_range
            print(
                f"  record 001={record['001'].data!r:8} "
                f"bytes [{start}:{end}]"
            )


def main() -> None:
    good_before = make_record("BK-0001", "First good title")
    good_after = make_record("BK-0002", "Second good title")
    # a non-numeric length header followed by arbitrary damaged bytes
    damaged = b"XXXXX" + b"corrupted-framing-bytes"
    batch = good_before + damaged + good_after

    print("input layout:")
    print(f"  good record : bytes [0:{len(good_before)}]")
    print(f"  damaged area: bytes [{len(good_before)}:"
          f"{len(good_before) + len(damaged)}]")
    print(f"  good record : bytes [{len(good_before) + len(damaged)}:"
          f"{len(batch)}]")

    print("\n1) default mode (permissive=False): stops at the bad region")
    summarize(MARCReader(batch))

    print("\n2) permissive=True: one None for the region, then recovery")
    summarize(MARCReader(batch, permissive=True))

    print("\n3) permissive=True over a 3-bytes-per-read stream")
    summarize(MARCReader(ThreeByteStream(batch), permissive=True))

    print(
        "\nAmbiguity policy: a five-digit header or an 0x1D byte alone never "
        "proves a frame.\n"
        "A candidate is accepted only when its declared length ends on an "
        "end-of-record byte\n"
        "and strict structural validation (base address, directory entry "
        "ranges and field\n"
        "terminators) succeeds. When several interpretations are possible, "
        "the first position\n"
        "that produces such a fully self-consistent frame wins; if none does "
        "within\n"
        "recovery_limit bytes, RecoveryLimitExceeded ends iteration instead "
        "of consuming\n"
        "the input without bound."
    )


if __name__ == "__main__":
    main()

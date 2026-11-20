#!/usr/bin/env python3
# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Demo: export Unicode records as MARC-8 instead of UTF-8 (Python 3.11).

Some union catalogues / exchange endpoints still only accept MARC-8. Editing
in pymarc works on Unicode strings, so the records are built (or read) as
Unicode and only serialized as MARC-8 at the very end.

Run it:

    python3 examples/write_marc8.py

Calling patttern
----------------

    # one record, in memory
    data = record.as_marc(encoding="marc-8")

    # a batch
    with open("out.dat", "wb") as fh:
        writer = MARCWriter(fh, encoding="marc-8")
        for record in records:
            writer.write(record)
        writer.close()

    # strict linkage validation and MARC-8 can be combined
    writer = MARCWriter(fh, encoding="marc-8", strict_linkage=True)

The default calls (``record.as_marc()`` and ``MARCWriter(fh)``) are unchanged:
they still follow the leader and write UTF-8 (setting leader position 9 to
"a").

Key tradeoffs
-------------

* Characters are looked up through a reverse map built once from the
  existing ``pymarc.marc8_mapping`` tables. Nothing is cached per record, so
  memory does not grow while a large file is exported.
* The encoder is chosen for byte growth and simplicity rather than shortest
  possible output: combining marks go immediately before their base, a mark
  stays in the base character's charset when that charset has it, and
  otherwise ANSEL in G1 is used (no G0 escape needed).
* Each subfield is self contained: it starts from Basic Latin/ANSEL and
  restores those designations at the end, so state never leaks between
  subfields, fields, or records.
* A record is fully serialized before the writer touches the file. A record
  that cannot be encoded raises and file bytes for it are never written;
  already written records stay and the next record can be written normally.
"""

import unicodedata
from io import BytesIO

from pymarc import (
    Field,
    Indicators,
    MARC8EncodingError,
    MARCReader,
    MARCWriter,
    RawField,
    Record,
    Subfield,
    unicode_to_marc8,
)


def make_field(tag, *pairs, indicators=(" ", " ")):
    """Build a data field from (code, value) pairs."""
    return Field(
        tag=tag,
        indicators=Indicators(*indicators),
        subfields=[Subfield(code, value) for code, value in pairs],
    )


def mixed_script_record():
    """A title that moves from Latin to Cyrillic to East Asian in one field.

    The accented Latin text is given in NFD ("e" + combining acute) on
    purpose; MARC-8 output is identical to the NFC spelling.
    """
    record = Record()
    record.add_field(Field(tag="001", data="demo-marc8-0001"))
    record.add_field(
        make_field(
            "100",
            ("a", "Лев Толстой."),
            indicators=("1", " "),
        )
    )
    record.add_field(
        make_field(
            "245",
            ("a", "Léo и война : "),
            ("b", "東京都の物語 /"),
            ("c", unicodedata.normalize("NFD", "Léon Tolstoï.")),
            indicators=("1", "0"),
        )
    )
    record.add_field(
        make_field(
            "264",
            ("a", "Москва :"),
            ("b", "Издательство, "),
            ("c", "2021."),
            indicators=(" ", "1"),
        )
    )
    record.add_field(make_field("650", ("a", "Greek: Αθήνα"), ("a", "שלום")))
    return record


def show_bytes(label, data):
    print(f"--- {label} ({len(data)} bytes) ---")
    print(data)
    print()


def main():
    record = mixed_script_record()

    print("=== one subfield encodes across charsets ===")
    print(unicode_to_marc8("Léo и война 東京"))
    print()

    print("=== as_marc(encoding='marc-8') ===")
    data = record.as_marc(encoding="marc-8")
    show_bytes("leader position 9 is a blank", data[:24])
    print("leader position 9:", repr(data[9:10]), "(blank = MARC-8)")
    print("source leader untouched:", repr(str(record.leader)[9]))
    print()

    print("=== read back through the normal reader ===")
    back = next(MARCReader(BytesIO(data), to_unicode=True))
    for original, decoded in zip(record.fields, back.fields):
        if original.control_field:
            continue
        before = [(s.code, s.value) for s in original.subfields]
        after = [(s.code, s.value) for s in decoded.subfields]
        same = unicodedata.normalize("NFC", "|".join(
            v for _c, v in before
        )) == unicodedata.normalize("NFC", "|".join(v for _c, v in after))
        print(f"{original.tag}: {after} (NFC-equivalent: {same})")
    print()

    print("=== direct serialization and the writer agree ===")
    stream = BytesIO()
    writer = MARCWriter(stream, encoding="marc-8", strict_linkage=True)
    writer.write(record)
    writer.close(close_fh=False)
    print("bytes identical:", stream.getvalue() == data)
    print()

    print("=== a bad record does not ruin a batch ===")
    batch = BytesIO()
    writer = MARCWriter(batch, encoding="marc-8")
    writer.write(record)
    good_length = len(batch.getvalue())
    bad = Record()
    bad.add_field(
        make_field("245", ("a", "snowman ☃"), indicators=("1", "0"))
    )
    bad.add_field(
        RawField(
            tag="500",
            indicators=Indicators(" ", " "),
            subfields=[Subfield("a", b"raw bytes")],
        )
    )
    try:
        writer.write(bad)
    except MARC8EncodingError as err:
        print("skipped:", err)
    print("nothing of the bad record was written:", len(batch.getvalue()) == good_length)
    writer.write(record)
    writer.close(close_fh=False)
    print("records in batch:", len(list(MARCReader(BytesIO(batch.getvalue())))))
    print()

    print("=== decomposed and precomposed input produce the same bytes ===")
    nfd = Record()
    nfd.add_field(
        make_field("245", ("a", unicodedata.normalize("NFD", "Léon")), indicators=("1", "0"))
    )
    nfc = Record()
    nfc.add_field(make_field("245", ("a", "Léon"), indicators=("1", "0")))
    print(
        "identical:",
        nfd.as_marc(encoding="marc-8") == nfc.as_marc(encoding="marc-8"),
    )


if __name__ == "__main__":
    main()

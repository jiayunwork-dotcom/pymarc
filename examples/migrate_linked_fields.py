#!/usr/bin/env python3
# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Demo: preview and apply a subfield 6 linkage group migration (Python 3.11).

Two hand built records describe the same edition in two catalogs. "home" is
the record we enrich; "ext" is the record we pick groups from. The external
title group and the home subject group both use occurrence number 01, so the
import must be renumbered -- and the home subject's 880 must not end up
mistaken for a title 880.

Run it:

    python3 examples/migrate_linked_fields.py

Calling pattern
---------------

    from pymarc import ImportGroup, ReplaceGroup, preview_migration

    plan = preview_migration(
        ext, home,
        [ImportGroup("01"), ReplaceGroup(target="02", source="04")],
        source_label="ext", target_label="home",
    )
    print(plan)     # the cataloger reviews fields, numbers, replacements
    plan.apply()    # re-validates against the current records, then writes

Groups are selected by occurrence number ("01"), by Field object, or by a
FieldGroup from ``record.linkage_index()``; several operations are planned
and applied as one transaction. Strict spot checks use
``record.get_linked_fields(field, strict=True)``; strict output uses
``MARCWriter(fh, strict_linkage=True)`` / ``XMLWriter(fh, strict_linkage=True)``.

Key tradeoffs
-------------

* The plan stores isolated, already renumbered copies of the fields to
  import plus content snapshots of the involved units -- not whole-record
  copies. So apply() can tell relevant mid-flight changes (an imported or
  replaced unit changed, an assigned number got taken, linkage broke) from
  irrelevant ones (an added note), and temporary space stays proportional
  to what is imported.
* Previewing never mutates and is deterministic: repeated previews of the
  same records assign the same numbers; applying one plan makes a sibling
  plan conflict instead of double-applying.
* New groups take the lowest free number 01-99 (deterministic, easy to
  verify by eye) rather than reusing the source number when it happens to
  be free; replacements reuse the replaced group's number.
* Scanning is one pass per record per phase (preview, apply), shared by all
  operations in the plan -- not one rescan per group.
"""

from io import BytesIO

from pymarc import (
    Field,
    ImportGroup,
    Indicators,
    InvalidLinkage,
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
    """本馆记录: romanized-only title, thin author group (02), subject
    group (01) and a standalone 880-00 vernacular note."""
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
    """外馆记录, same edition: richer author group (04), title group (01)
    with two 880s in different scripts, subject group (03) we leave behind."""
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


def group_signature(record):
    """A printable snapshot of a record's linkage groups."""
    return {
        number: (group.anchor.tag, [f["a"] for f in group.alternates])
        for number, group in record.linkage_index().groups.items()
    }


def main():
    home, ext = home_record(), ext_record()
    print("=== home (本馆) ===")
    print(home)
    print("=== ext (外馆, same edition) ===")
    print(ext)

    print("=== strict spot check of ext before planning ===")
    for field in ext.get_fields("100", "245", "650"):
        linked = ext.get_linked_fields(field, strict=True)
        print(f"{field.tag} {field['6']}: {len(linked)} linked 880(s)")

    plan = preview_migration(
        ext,
        home,
        [ImportGroup("01"), ReplaceGroup(target="02", source="04")],
        source_label="ext",
        target_label="home",
    )
    print("=== preview (nothing written yet) ===")
    print(plan)

    plan.apply()
    print("=== home after apply ===")
    print(home)
    print("groups now:", group_signature(home))
    subject_880s = home.get_linked_fields(home["650"], strict=True)
    print("home subject 650 linked 880(s):", [f["a"] for f in subject_880s])
    print("ext still has its title group:", ext["245"]["6"])

    print("=== strict writers: binary and MARCXML round trips agree ===")
    marc_stream, xml_stream = BytesIO(), BytesIO()
    marc_writer = MARCWriter(marc_stream, strict_linkage=True)
    marc_writer.write(home)
    marc_writer.close(close_fh=False)
    xml_writer = XMLWriter(xml_stream, strict_linkage=True)
    xml_writer.write(home)
    xml_writer.close(close_fh=False)
    from_marc = next(MARCReader(BytesIO(marc_stream.getvalue()), to_unicode=True))
    from_xml = parse_xml_to_array(BytesIO(xml_stream.getvalue()))[0]
    print(
        "binary == memory == xml:",
        group_signature(from_marc)
        == group_signature(home)
        == group_signature(from_xml),
    )

    print("=== what if the record changes between preview and apply? ===")
    home2, ext2 = home_record(), ext_record()
    plan2 = preview_migration(ext2, home2, [ImportGroup("01")])
    home2.add_field(make_field("500", ("a", "Second copy on order.")))
    plan2.apply()  # an unrelated note does not conflict and is preserved
    print(
        "note kept, import applied:",
        home2.get_fields("245")[1]["6"],
        "|",
        home2.get_fields("500")[-1]["a"],
    )

    home3, ext3 = home_record(), ext_record()
    plan3 = preview_migration(ext3, home3, [ImportGroup("01")])
    home3.add_field(  # someone else grabs the pre-assigned number 03
        make_field("700", ("6", "880-03"), ("a", "Latecomer."), indicators=("1", " ")),
        make_field("880", ("6", "700-03"), ("a", "后来者"), indicators=("1", " ")),
    )
    try:
        plan3.apply()
    except MigrationConflict as err:
        print("conflict caught, target untouched:")
        print(err)

    print("=== strict writers reject a record broken by hand ===")
    title = home.get_fields("245")[1]
    title.subfields[0] = Subfield("6", "880-3")  # hand-break the linkage
    try:
        MARCWriter(BytesIO(), strict_linkage=True).write(home)
    except InvalidLinkage as err:
        print(err)


if __name__ == "__main__":
    main()

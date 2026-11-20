# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Structural validation of a MARC21 record frame.

A MARC21 transmission-format record is::

    leader (24 bytes)
    directory (12-byte entries terminated by an end-of-field byte 0x1E)
    field data (addressed by the directory, terminated by an end-of-record
    byte 0x1D)

The directory is the authoritative index of the field area. A five digit
number at the start of a chunk, or an end-of-record byte inside it, does not
prove the chunk is a record. :func:`validate_frame` verifies that the frame
is internally consistent without decoding any field text, so it is cheap
enough to be used both when constructing a :class:`~pymarc.record.Record`
and when a reader is scanning a corrupted stream for the next valid frame.
"""

from typing import List, NamedTuple, Optional

from pymarc.constants import (
    DIRECTORY_ENTRY_LEN,
    END_OF_FIELD,
    END_OF_RECORD,
    LEADER_LEN,
)
from pymarc.exceptions import (
    BaseAddressInvalid,
    BaseAddressNotFound,
    FieldOutOfBounds,
    FieldTerminatorMissing,
    NoFieldsFound,
    RecordDirectoryInvalid,
    RecordLeaderInvalid,
    TruncatedRecord,
)


class DirectoryEntry(NamedTuple):
    """One parsed 12-byte directory entry, with byte positions."""

    tag: str
    length: int
    offset: int
    #: half-open slice of the record bytes holding the field, including its
    #: terminating end-of-field byte
    field_start: int
    field_end: int


class ValidatedFrame(NamedTuple):
    """Result of a successful frame validation."""

    leader: str
    base_address: int
    entries: List[DirectoryEntry]


def validate_frame(marc: bytes, strict_terminators: bool = False) -> ValidatedFrame:
    """Check the structural integrity of one MARC21 frame.

    Only frame structure is inspected (leader, base address, directory and
    the byte ranges each entry addresses); field text is never decoded, so
    a record with bad text but a sound frame validates here and fails later
    during decoding. Raises a :class:`~pymarc.exceptions.FramingError`
    subclass when the frame cannot be trusted.

    Every directory entry must address bytes wholly inside the record's
    field area -- this check is unconditional, because an out-of-bounds
    slice would otherwise silently swallow field/record terminators or
    bytes belonging to the next record. With ``strict_terminators=True``
    each entry must additionally end on an end-of-field byte; the default
    keeps the historical tolerance for in-range, mis-sized entries found
    in some real-world catalogs. The recovery scanner uses the strict form
    so that only a fully self-consistent frame can end a damaged region.
    """
    # extract record leader
    try:
        leader = marc[0:LEADER_LEN].decode("ascii")
    except UnicodeDecodeError:
        raise RecordLeaderInvalid from None
    if len(leader) != LEADER_LEN:
        raise RecordLeaderInvalid

    # the declared record length is checked after the base address checks,
    # matching the historical order in which Record.decode_marc reported
    # problems for short, otherwise-malformed chunks
    try:
        declared_length = int(leader[0:5])
    except ValueError:
        raise RecordLeaderInvalid from None

    # extract the byte offset where the field data starts
    base_text = marc[12:17]
    if not base_text.isdigit():
        # keep the historical raw ValueError for non-numeric base addresses
        # when a full leader is present, except when only the last byte is
        # bad (a mis-typed terminator digit), which is reported as an invalid
        # base address
        if len(base_text) == 5 and base_text[:4].isdigit():
            raise BaseAddressInvalid
        int(base_text)
    base_address = int(base_text)
    if base_address <= 0:
        raise BaseAddressNotFound
    if base_address >= len(marc):
        raise BaseAddressInvalid

    if len(marc) < declared_length:
        raise TruncatedRecord

    # the directory ends with an END_OF_FIELD byte; the base address points
    # at the first field byte, so the terminator is at base_address - 1
    if marc[base_address - 1 : base_address] != END_OF_FIELD.encode("ascii"):
        raise RecordDirectoryInvalid

    directory = marc[LEADER_LEN : base_address - 1].decode("ascii")
    if len(directory) % DIRECTORY_ENTRY_LEN != 0:
        raise RecordDirectoryInvalid
    field_total = len(directory) // DIRECTORY_ENTRY_LEN

    entries: List[DirectoryEntry] = []
    # the field area spans [base_address, end of record marker)
    field_area_end = len(marc)
    if marc and marc[-1:] == END_OF_RECORD.encode("ascii"):
        field_area_end -= 1

    for index in range(field_total):
        entry_start = index * DIRECTORY_ENTRY_LEN
        entry = directory[entry_start : entry_start + DIRECTORY_ENTRY_LEN]
        entry_tag = entry[0:3]
        entry_length = int(entry[3:7])
        entry_offset = int(entry[7:12])
        if entry_length <= 0:
            raise RecordDirectoryInvalid

        field_start = base_address + entry_offset
        field_end = field_start + entry_length
        # every addressed byte, including the end-of-field terminator, must
        # lie inside the record's field area; otherwise slicing would silently
        # pull field/record terminators (or neighboring records) into a value
        if field_start < base_address or field_end > field_area_end:
            raise FieldOutOfBounds
        if (
            strict_terminators
            and marc[field_end - 1 : field_end] != END_OF_FIELD.encode("ascii")
        ):
            raise FieldTerminatorMissing
        entries.append(
            DirectoryEntry(
                tag=entry_tag,
                length=entry_length,
                offset=entry_offset,
                field_start=field_start,
                field_end=field_end,
            )
        )

    if not entries:
        raise NoFieldsFound
    return ValidatedFrame(leader=leader, base_address=base_address, entries=entries)


def probe_candidate(marc: bytes) -> Optional[ValidatedFrame]:
    """Return a strictly validated frame or ``None`` without raising.

    Used by the recovery scanner on many candidate positions, most of which
    are not records. In addition to :func:`validate_frame` with strict
    terminator checking, the declared frame must actually end on an
    end-of-record byte; otherwise a mis-declared length that merely
    contains a plausible directory could be mistaken for a record.
    """
    if not marc or marc[-1:] != END_OF_RECORD.encode("ascii"):
        return None
    try:
        return validate_frame(marc, strict_terminators=True)
    except Exception:
        return None

# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Exceptions for pymarc."""

from typing import NamedTuple, Optional, Tuple


class PymarcException(Exception):
    """Base pymarc Exception."""

    pass


class FatalReaderError(PymarcException):
    """Error preventing further reading.

    Exceptions in this branch mean the reader cannot trust the byte where
    the next record is supposed to start, so without a recovery scan
    iteration has to end.
    """

    pass


class FramingError(PymarcException):
    """A record frame (boundary) could not be established or verified.

    The byte range the reader believed to be one record cannot be parsed as
    a self-consistent MARC21 frame, so the position of the *next* record is
    unknown and has to be located by scanning ahead. Exceptions in this
    branch carry the half-open byte range (relative to the start of the
    reader's input) of the region that could not be framed.
    """

    def __init__(self, message: str = "", start: Optional[int] = None,
                 end: Optional[int] = None) -> None:
        super().__init__(message)
        self.start = start
        self.end = end

    @property
    def byte_range(self) -> Optional[Tuple[int, int]]:
        """Half-open byte range ``(start, end)`` of the un-frameable region."""
        if self.start is None or self.end is None:
            return None
        return self.start, self.end


class ContentError(PymarcException):
    """A frame with a trusted boundary holds invalid content.

    The record's length header, end-of-record marker, base address and
    directory all check out, so the next record still starts right after
    this frame; only this record's payload (for example undecodable text)
    is unusable.
    """

    pass


class RecordLengthInvalid(FatalReaderError, FramingError):
    """Invalid record length."""

    def __str__(self):
        return "Invalid record length in first 5 bytes of record"


class TruncatedRecord(FatalReaderError, FramingError):
    """Truncated record data."""

    def __str__(self):
        return "Record length in leader is greater than the length of data"


class EndOfRecordNotFound(FatalReaderError, FramingError):
    """Unable to locate end of record marker."""

    def __str__(self):
        return "Unable to locate end of record marker"


class RecoveryLimitExceeded(FatalReaderError, FramingError):
    """No valid record frame found within the recovery look-ahead window."""

    def __str__(self):
        rng = self.byte_range
        if rng is None:
            return "Recovery limit exceeded while searching for a record"
        return (
            "No valid MARC record frame could be found within the "
            f"recovery window covering bytes {rng[0]}..{rng[1]}"
        )


class RecordLeaderInvalid(FramingError):
    """Unable to extract record leader."""

    def __str__(self):
        return "Unable to extract record leader"


class RecordDirectoryInvalid(FramingError):
    """Invalid directory."""

    def __str__(self):
        return "Invalid directory"


class NoFieldsFound(FramingError):
    """Unable to locate fields in record data."""

    def __str__(self):
        return "Unable to locate fields in record data"


class BaseAddressInvalid(FramingError):
    """Base address exceeds size of record."""

    def __str__(self):
        return "Base address exceeds size of record"


class BaseAddressNotFound(FramingError):
    """Unable to locate base address of record."""

    def __str__(self):
        return "Unable to locate base address of record"


class FieldOutOfBounds(FramingError):
    """A directory entry addresses bytes outside the record's field area."""

    def __str__(self):
        return "Directory entry addresses bytes outside the record"


class FieldTerminatorMissing(FramingError):
    """The byte a directory entry says ends a field is not an end-of-field."""

    def __str__(self):
        return "Directory entry does not end on an end-of-field marker"


class WriteNeedsRecord(PymarcException):
    """Write requires a pymarc.Record object as an argument."""

    def __str__(self):
        return "Write requires a pymarc.Record object as an argument"


class NoActiveFile(PymarcException):
    """There is no active file to write to in call to write."""

    def __str__(self):
        return "There is no active file to write to in call to write"


class FieldNotFound(PymarcException):
    """Record does not contain the specified field."""

    def __str__(self):
        return "Record does not contain the specified field"


class BadSubfieldCodeWarning(Warning):
    """Warning about a non-ASCII subfield code."""

    pass


class BadLeaderValue(PymarcException):
    """Error when setting a leader value."""

    pass


class MissingLinkedFields(PymarcException):
    """Error when a non-880 field has a subfield 6 that cannot be matched to an 880."""

    def __init__(self, field):
        """Initialize MissingLinkedFields with the `field` that lacks one or more target 880s."""
        super().__init__(field)
        self.field = field

    def __str__(self):
        return f"{self.field.tag} field includes a subfield 6 but no linked fields could be found."


class LinkageProblem(NamedTuple):
    """A single subfield 6 linkage problem found in a record.

    `label` identifies the record (e.g. a file name or "source"/"target"),
    `position` is the field's index in the record's field list (None when the
    field is not tied to a record, e.g. raised from Field directly), `tag` is
    the field's tag and `message` describes what is wrong.
    """

    label: str
    position: Optional[int]
    tag: str
    message: str


class InvalidLinkage(PymarcException):
    """Error when subfield 6 linkage in a record is broken or ambiguous.

    Carries a list of LinkageProblem named tuples in `problems` so a human
    can locate the offending record and fields.
    """

    def __init__(self, problems):
        """Initialize InvalidLinkage with a list of LinkageProblem."""
        super().__init__(problems)
        self.problems = tuple(problems)

    def __str__(self):
        lines = ["Invalid subfield 6 linkage:"]
        for p in self.problems:
            where = p.label or "<record>"
            if p.position is not None:
                where += f" field #{p.position}"
            lines.append(f"  {where} {p.tag}: {p.message}")
        return "\n".join(lines)


class MigrationConflict(PymarcException):
    """Error when a migration plan no longer matches the records at apply time.

    Raised before the target record is touched. `conflicts` is a tuple of
    human readable descriptions of what changed since the plan was previewed.
    """

    def __init__(self, conflicts):
        """Initialize MigrationConflict with a list of conflict descriptions."""
        super().__init__(conflicts)
        self.conflicts = tuple(conflicts)

    def __str__(self):
        lines = ["Migration plan conflicts with the current records:"]
        lines.extend(f"  {c}" for c in self.conflicts)
        return "\n".join(lines)


class LinkageNumbersExhausted(PymarcException):
    """Error when no free linkage occurrence number (01-99) remains for a plan."""

    def __str__(self):
        return "No free linkage occurrence number (01-99) available for the plan"

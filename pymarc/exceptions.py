# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Exceptions for pymarc."""

from typing import NamedTuple, Optional


class PymarcException(Exception):
    """Base pymarc Exception."""

    pass


class FatalReaderError(PymarcException):
    """Error preventing further reading."""

    pass


class RecordLengthInvalid(FatalReaderError):
    """Invalid record length."""

    def __str__(self):
        return "Invalid record length in first 5 bytes of record"


class TruncatedRecord(FatalReaderError):
    """Truncated record data."""

    def __str__(self):
        return "Record length in leader is greater than the length of data"


class EndOfRecordNotFound(FatalReaderError):
    """Unable to locate end of record marker."""

    def __str__(self):
        return "Unable to locate end of record marker"


class RecordLeaderInvalid(PymarcException):
    """Unable to extract record leader."""

    def __str__(self):
        return "Unable to extract record leader"


class RecordDirectoryInvalid(PymarcException):
    """Invalid directory."""

    def __str__(self):
        return "Invalid directory"


class NoFieldsFound(PymarcException):
    """Unable to locate fields in record data."""

    def __str__(self):
        return "Unable to locate fields in record data"


class BaseAddressInvalid(PymarcException):
    """Base address exceeds size of record."""

    def __str__(self):
        return "Base address exceeds size of record"


class BaseAddressNotFound(PymarcException):
    """Unable to locate base address of record."""

    def __str__(self):
        return "Unable to locate base address of record"


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


class MARC8EncodingError(PymarcException):
    """A record cannot be represented in MARC-8 or is not valid input for it.

    `tag` is the field tag, `tag_occurrence` the 1-based occurrence of that
    tag in the record, `field_index` its 0-based position, `subfield_index`
    the 0-based position of the subfield within the field (None for control
    fields), `subfield_code` its code, `char_offset` the offset of the
    offending character in the original (pre-normalization) Python string,
    and `character` the offending character. Any attribute may be None for
    record-level problems such as a RawField.
    """

    def __init__(
        self,
        reason: str,
        *,
        tag: Optional[str] = None,
        tag_occurrence: Optional[int] = None,
        field_index: Optional[int] = None,
        subfield_index: Optional[int] = None,
        subfield_code: Optional[str] = None,
        char_offset: Optional[int] = None,
        character: Optional[str] = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tag = tag
        self.tag_occurrence = tag_occurrence
        self.field_index = field_index
        self.subfield_index = subfield_index
        self.subfield_code = subfield_code
        self.char_offset = char_offset
        self.character = character

    def __str__(self) -> str:
        where = []
        if self.tag is not None:
            where.append(f"tag {self.tag}")
        if self.tag_occurrence is not None:
            where.append(f"occurrence {self.tag_occurrence}")
        if self.field_index is not None:
            where.append(f"field index {self.field_index}")
        if self.subfield_index is not None:
            where.append(f"subfield #{self.subfield_index}")
        if self.subfield_code is not None:
            where.append(f"${self.subfield_code}")
        location = ", ".join(where) or "record"
        detail = self.reason
        if self.character is not None:
            detail += f" {self.character!r}"
            if len(self.character) == 1:
                detail += f" (U+{ord(self.character):04X})"
        if self.char_offset is not None:
            detail += f" at character offset {self.char_offset}"
        return f"{location}: {detail}"


class RecordTooLargeError(PymarcException):
    """A MARC record exceeds the fixed widths of the leader or directory.

    Field lengths are limited to four decimal digits, field offsets and the
    whole record to five. The record is rejected rather than allowing the
    directory to be silently corrupted.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message

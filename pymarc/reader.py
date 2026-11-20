# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Pymarc Reader."""

import json
import os
import sys
from collections.abc import Iterator
from io import BytesIO, IOBase, StringIO
from typing import IO, BinaryIO, Callable, Optional, Tuple, Union

from pymarc import Field, Indicators, Leader, Record, Subfield, exceptions
from pymarc.constants import END_OF_RECORD
from pymarc.exceptions import (
    EndOfRecordNotFound,
    FramingError,
    RecordLengthInvalid,
    RecoveryLimitExceeded,
    TruncatedRecord,
)
from pymarc.frame import probe_candidate, validate_frame

#: Maximum number of bytes requested from the underlying stream in one
#: ``read()`` call. Records larger than this are assembled over several reads,
#: so a single read request never has to grow with record or damage size.
READ_CHUNK_SIZE = 1 << 16  # 65536

#: Default limit on how many bytes ahead of a lost boundary the reader will
#: inspect while looking for the next valid record frame.
DEFAULT_RECOVERY_LIMIT = 1 << 20  # 1048576

#: Smallest record length we will ever attempt to parse.
MIN_RECORD_LENGTH = 24


class Reader:
    """A base class for all iterating readers in the pymarc package."""

    def __iter__(self):
        return self


class MARCReader(Reader):
    """An iterator class for reading a file of MARC21 records.

    Simple usage:

    .. code-block:: python

        from pymarc import MARCReader

        ## pass in a file object
        reader = MARCReader(open('file.dat', 'rb'))
        for record in reader:
            ...

        ## pass in marc in transmission format
        reader = MARCReader(rawmarc)
        for record in reader:
            ...

    If you would like to have your Record object contain unicode strings
    use the to_unicode parameter:

    .. code-block:: python

        reader = MARCReader(open('file.dat', 'rb'), to_unicode=True)

    This will decode from MARC-8 or utf-8 depending on the value in the
    MARC leader at position 9. Upon serialization of the Record object to MARC21,
    the resulting output will be utf-8 encoded and the value in the MARC
    leader at position 9 will be set appropriately to indicate the change of
    character encoding.

    If you find yourself in the unfortunate position of having data that
    is utf-8 encoded without the leader set appropriately you can use
    the force_utf8 parameter:

    .. code-block:: python

        reader = MARCReader(open('file.dat', 'rb'), to_unicode=True,
            force_utf8=True)

    If you find yourself in the unfortunate position of having data that is
    mostly utf-8 encoded but with a few non-utf-8 characters, you can also use
    the utf8_handling parameter, which takes the same values ('strict',
    'replace', and 'ignore') as the Python Unicode codecs (see
    http://docs.python.org/library/codecs.html for more info).

    Although, it's not legal in MARC-21 to use anything but MARC-8 or UTF-8, but
    if you have a file in incorrect encode and you know what it is, you can
    try to use your encode in parameter "file_encoding".

    MARCReader parses data in a permissive way and gives the user full control
    on what to do in case wrong record is encountered. Whenever any error is
    found reader returns ``None`` instead of regular record object.
    The exception information and corresponding data are available through
    reader.current_exception and reader.current_chunk properties:

    .. code-block:: python

        reader = MARCReader(open('file.dat', 'rb'))
        for record in reader:
            if record is None:
                print(
                    "Current chunk: ",
                    reader.current_chunk,
                    " was ignored because the following exception raised: ",
                    reader.current_exception
                )
            else:
                # do something with record

    The input may be ``bytes`` or any synchronous binary stream exposing
    ``read()`` (it need not support ``seek()`` or ``tell()``). A short read --
    ``read(n)`` returning fewer than ``n`` bytes -- is normal and never turns
    a valid record into an error; only an empty read marks end of file.

    With ``permissive=True`` a frame that cannot be established does not end
    iteration: the reader scans ahead, structurally validating candidate
    frames, until it finds the next good record and delivers every good
    record in order exactly once. The damaged region is reported as a single
    ``None`` whose :attr:`current_exception` is a
    :class:`~pymarc.exceptions.FramingError` carrying its half-open byte range
    (also available as :attr:`current_range`). ``recovery_limit`` bounds the
    look-ahead for one such resynchronization.
    """

    _current_chunk = None
    _current_exception = None
    _current_range: Optional[Tuple[int, int]] = None

    file_handle: IO

    def __init__(
        self,
        marc_target: Union[BinaryIO, bytes],
        to_unicode: bool = True,
        force_utf8: bool = False,
        hide_utf8_warnings: bool = False,
        utf8_handling: str = "strict",
        file_encoding: str = "iso8859-1",
        permissive: bool = False,
        *,
        recovery_limit: int = DEFAULT_RECOVERY_LIMIT,
    ) -> None:
        """The constructor to which you can pass either raw marc or a file-like object.

        Basically the argument you pass in should be raw MARC in transmission format or
        an object that responds to read().

        ``recovery_limit`` is keyword-only and limits how many bytes ahead of
        a lost frame boundary the reader may inspect while resynchronizing in
        permissive mode. It must be a positive integer (booleans are rejected).
        """
        super().__init__()
        self.to_unicode = to_unicode
        self.force_utf8 = force_utf8
        self.hide_utf8_warnings = hide_utf8_warnings
        self.utf8_handling = utf8_handling
        self.file_encoding = file_encoding
        self.permissive = permissive
        if isinstance(recovery_limit, bool) or not isinstance(recovery_limit, int):
            raise TypeError("recovery_limit must be a positive integer")
        if recovery_limit <= 0:
            raise ValueError("recovery_limit must be a positive integer")
        self.recovery_limit = recovery_limit
        if isinstance(marc_target, bytes):
            self.file_handle = BytesIO(marc_target)
        else:
            self.file_handle = marc_target

        # buffered, position-tracked view over a possibly short-reading stream
        self._buffer = bytearray()
        self._stream_offset = 0  # absolute position of self._buffer[0]
        self._eof = False
        # once set, iteration is finished; repeated next() must stay silent
        self._exhausted = False
        # diagnostics for the item most recently produced
        self._current_chunk = None
        self._current_exception = None
        self._current_range = None

    @property
    def current_chunk(self):
        """Bytes covered by the most recent record or damaged region."""
        return self._current_chunk

    @property
    def current_exception(self):
        """Exception describing the most recent failed item, or ``None``."""
        return self._current_exception

    @property
    def current_range(self) -> Optional[Tuple[int, int]]:
        """Half-open byte range ``(start, end)`` of the current item.

        For a record it is the range of its frame in the input; for a
        damaged region it is the range reported with the
        :class:`~pymarc.exceptions.FramingError`.
        """
        return self._current_range

    def close(self) -> None:
        """Close the handle."""
        self.file_handle.close()

    # ------------------------------------------------------------------
    # buffered stream handling
    # ------------------------------------------------------------------
    def _fill(self, wanted: int) -> None:
        """Read until at least ``wanted`` bytes are buffered or EOF is hit.

        Each underlying read requests at most :data:`READ_CHUNK_SIZE` bytes,
        so callers may ask for very large amounts without issuing one huge
        read. Short reads are simply followed by another read. Once EOF is
        observed no further reads are issued, which also keeps streams whose
        position has moved past the end from being asked for data again.
        """
        while len(self._buffer) < wanted and not self._eof:
            request = min(READ_CHUNK_SIZE, wanted - len(self._buffer))
            more = self.file_handle.read(request)
            if more is None:
                # defensive: a well-behaved binary stream returns b"" at EOF
                continue
            if not more:
                self._eof = True
                break
            self._buffer.extend(more)

    def _consume(self, count: int) -> bytes:
        """Remove and return the first ``count`` buffered bytes."""
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        self._stream_offset += count
        return data

    def _set_current(
        self,
        chunk: Optional[bytes],
        exception: Optional[Exception],
        start: int,
        end: int,
    ) -> None:
        self._current_chunk = chunk
        self._current_exception = exception
        self._current_range = (start, end)
        if isinstance(exception, FramingError):
            if exception.start is None:
                exception.start = start
            if exception.end is None:
                exception.end = end

    # ------------------------------------------------------------------
    # iteration
    # ------------------------------------------------------------------
    def __next__(self):
        """Read and parse the next record, or report one damaged region."""
        if self._exhausted:
            raise StopIteration

        # clear diagnostics from the previous item
        self._current_chunk = None
        self._current_exception = None
        self._current_range = None

        start = self._stream_offset
        # make sure we can see a five-byte length header
        self._fill(5)
        if not self._buffer:
            self._exhausted = True
            raise StopIteration

        if len(self._buffer) < 5:
            # a partial length header at true EOF is a truncated bad tail
            self._report_tail(TruncatedRecord(), start)
            return None

        header = bytes(self._buffer[:5])
        try:
            length = int(header)
        except ValueError:
            if self.permissive:
                self._resync(start, RecordLengthInvalid())
            else:
                # historical behavior: the five bad bytes are the chunk
                chunk = self._consume(5)
                self._stop_with(RecordLengthInvalid(), chunk, start, start + 5)
            return None

        if length < MIN_RECORD_LENGTH:
            # cannot hold a leader + directory terminator + record terminator
            self._fill(length)
            chunk = self._consume(min(length, len(self._buffer)))
            end = self._stream_offset
            if self.permissive:
                self._resync(start, EndOfRecordNotFound(), preconsumed=end)
            else:
                self._stop_with(EndOfRecordNotFound(), chunk, start, end)
            return None

        # fetch the whole declared frame, tolerating any number of short reads
        self._fill(length)
        frame = bytes(self._buffer[:length])
        if len(frame) < length:
            if self.permissive:
                # the declared frame runs past what the stream currently
                # holds: it may simply over-count (a long length header), so
                # resynchronize rather than assuming the stream is finished
                self._resync(start, TruncatedRecord())
            else:
                # the stream ended inside the declared frame
                self._report_tail(TruncatedRecord(), start)
            return None

        if frame[-1:] != END_OF_RECORD.encode("ascii"):
            # the declared length does not land on a record terminator: the
            # frame boundary cannot be trusted
            if self.permissive:
                self._resync(start, EndOfRecordNotFound())
            else:
                chunk = self._consume(length)
                self._stop_with(
                    EndOfRecordNotFound(), chunk, start, start + length
                )
            return None

        # the boundary looks right and the terminator is in place: this much
        # is enough to trust where the *next* record starts. Structural and
        # content failures inside the frame are therefore content errors:
        # the frame is consumed exactly and iteration continues, and pseudo
        # records inside its payload can never be mistaken for real ones.
        # Only failures at the stream layer above (bad length header,
        # truncated frame, missing end-of-record) require resynchronization.
        self._consume(length)
        try:
            validate_frame(frame)
            record = Record(
                frame,
                to_unicode=self.to_unicode,
                force_utf8=self.force_utf8,
                hide_utf8_warnings=self.hide_utf8_warnings,
                utf8_handling=self.utf8_handling,
                file_encoding=self.file_encoding,
            )
        except Exception as exc:  # content error inside a trusted frame
            self._set_current(frame, exc, start, start + length)
            return None

        self._set_current(frame, None, start, start + length)
        return record

    # ------------------------------------------------------------------
    # failure routing and recovery
    # ------------------------------------------------------------------
    def _stop_with(
        self, exception: FramingError, chunk: bytes, start: int, end: int
    ) -> None:
        """Report an un-frameable region once and end iteration."""
        self._set_current(chunk, exception, start, end)
        self._exhausted = True

    def _report_tail(self, exception: FramingError, start: int) -> None:
        """Consume everything still buffered and report it as a bad tail."""
        chunk = self._consume(len(self._buffer))
        end = self._stream_offset
        # even in permissive mode there is nothing beyond EOF to recover, so
        # report once and end iteration
        self._stop_with(exception, chunk, start, end)

    def _resync(
        self,
        start: int,
        cause: FramingError,
        preconsumed: Optional[int] = None,
    ) -> None:
        """Scan ahead from a lost boundary for the next valid frame.

        Reports the whole damaged region (``start`` up to the next accepted
        record) exactly once as ``None``, then leaves the accepted record in
        the buffer so the following :meth:`__next__` delivers it normally.
        ``preconsumed`` is the absolute position already reached when the
        caller consumed part of the region before detecting the damage.

        Candidate record *starts* are only considered within
        ``[start, start + recovery_limit)``; once such a start is eligible
        the whole frame it declares is read and validated, even if that
        frame extends past the window.
        """
        region_end_hint = preconsumed if preconsumed is not None else start
        limit = self.recovery_limit
        search_end = start + limit
        # buffer-relative index of the next candidate position to examine
        tested = 0
        # used to guarantee forward progress even against an unusual stream
        previous_buffer_len = -1

        while True:
            # grow the window so the pending candidate can be judged fully.
            # the fill loop itself caps each read at READ_CHUNK_SIZE, so
            # large frames/damaged regions never produce one huge read.
            window = bytes(self._buffer)
            want = max(tested + MIN_RECORD_LENGTH, len(self._buffer) + 1)
            if tested < len(window):
                header = window[tested : tested + 5]
                if len(header) == 5 and header.isdigit():
                    candidate_len = int(header)
                    if candidate_len >= MIN_RECORD_LENGTH:
                        want = tested + candidate_len
            self._fill(want)
            window = bytes(self._buffer)

            candidate_offset: Optional[int] = None
            found = False
            while tested < len(window):
                abs_pos = self._stream_offset + tested
                if abs_pos >= search_end:
                    # every candidate *start* inside the recovery window has
                    # now been considered
                    tested = len(window)
                    break
                header = window[tested : tested + 5]
                if len(header) < 5:
                    break  # need more bytes (or EOF ends the outer loop)
                if not header.isdigit():
                    tested += 1
                    continue
                candidate_len = int(header)
                if candidate_len < MIN_RECORD_LENGTH:
                    tested += 1
                    continue
                frame_end = tested + candidate_len
                if frame_end > len(window):
                    if self._eof:
                        # this candidate can never be completed; skip it and
                        # keep scanning later, possibly complete candidates
                        tested += 1
                        continue
                    break  # refill and re-judge this position
                if probe_candidate(window[tested:frame_end]) is not None:
                    candidate_offset = tested
                    found = True
                    break
                tested += 1

            if found:
                break

            window_abs_end = self._stream_offset + len(window)
            if self._eof:
                # damaged tail: no complete valid frame remains in the stream
                region = self._consume(len(self._buffer))
                end = self._stream_offset
                self._stop_with(cause, region, start, end)
                return

            if window_abs_end >= search_end and tested >= len(window):
                # every position inside the recovery window was examined
                # without producing a valid frame
                region = self._consume(len(self._buffer))
                end = self._stream_offset
                exc = RecoveryLimitExceeded(start=start, end=end)
                self._stop_with(exc, region, start, end)
                return

            # defensive: a stream that neither reaches EOF nor yields more
            # bytes cannot be resynchronized; report rather than spin forever
            if len(window) == previous_buffer_len and tested >= len(window):
                region = self._consume(len(self._buffer))
                end = self._stream_offset
                exc = RecoveryLimitExceeded(start=start, end=end)
                self._stop_with(exc, region, start, end)
                return
            previous_buffer_len = len(window)

        # accept the candidate: bytes before it form the damaged region.
        # the candidate frame stays buffered for the next __next__ call, so
        # its bytes can never be mixed into the reported bad region.
        bad_region = self._consume(candidate_offset)
        region_end = self._stream_offset
        # if the caller had already consumed part of the region, fold those
        # bytes into the reported chunk/range rather than dropping them
        if region_end_hint > start and not bad_region:
            region_end = max(region_end, region_end_hint)
        self._set_current(bad_region, cause, start, region_end)


def map_records(f: Callable, *files: BytesIO) -> None:
    """Applies a given function to each record in a batch.

    You can pass in multiple batches.

    .. code-block:: python

        def print_title(r):
            print(r['245'])
        map_records(print_title, file('marc.dat'))
    """
    for file in files:
        list(map(f, MARCReader(file)))


class JSONReader(Reader):
    """JSON Reader."""

    file_handle: IOBase

    def __init__(
        self,
        marc_target: Union[bytes, str],
        encoding: str = "utf-8",
        stream: bool = False,
    ) -> None:
        """The constructor to which you can pass either raw marc or a file-like object.

        Basically the argument you pass in should be raw JSON in transmission format
        or an object that responds to read().
        """
        self.encoding = encoding
        if isinstance(marc_target, IOBase):
            self.file_handle = marc_target
        else:
            if isinstance(marc_target, str) and os.path.exists(marc_target):
                self.file_handle = open(marc_target)  # noqa: SIM115
            else:
                self.file_handle = StringIO(marc_target)  # type: ignore
        if stream:
            sys.stderr.write(
                "Streaming not yet implemented, your data will be loaded into memory\n"
            )
        self.records = json.load(self.file_handle, strict=False)

    def __iter__(self) -> Iterator:
        if hasattr(self.records, "__iter__") and not isinstance(self.records, dict):
            self.iter = iter(self.records)
        else:
            self.iter = iter([self.records])
        return self

    def __next__(self) -> Iterator:
        jobj = next(self.iter)
        rec = Record()
        rec.leader = Leader(jobj["leader"])
        for field in jobj["fields"]:
            k, v = list(field.items())[0]
            if "subfields" in v and hasattr(v, "update"):
                # flatten m-i-j dict to list in pymarc
                subfields: list = []
                for sub in v["subfields"]:
                    for code, value in sub.items():
                        subfields.append(Subfield(code=code, value=value))
                fld = Field(
                    tag=k,
                    subfields=subfields,
                    indicators=Indicators(v["ind1"], v["ind2"]),
                )
            else:
                fld = Field(tag=k, data=v)
            rec.add_field(fld)
        return rec


class MARCMakerReader(Reader):
    r"""MARCMaker Reader.

    Converts a MARCMaker textual representation of a Marc 21 record into
    a pymarc Record.
    see :func:`Record.__str__() <pymarc.record.Record.__str__>` for more
    information.

    Simple usage:

    .. code-block:: python

        from pymarc import MARCMakerReader

        ## pass in a file object
        reader = MARCMakerReader(open('file.mrk', 'r'))
        for record in reader:
            ...

        ## pass a string
        reader = MARCReader("=LDR xxx\n=022  ##$a0000-0001\n\n=LDR yyy")
        for record in reader:
            ...
    """

    def __init__(self, target: Union[bytes, str], encoding: str = "utf-8") -> None:
        """The constructor to which you can pass either a str or a file-like object."""
        if isinstance(target, IOBase):
            file_handle = target
        else:
            if isinstance(target, str) and os.path.exists(target):
                file_handle = open(target, encoding=encoding)  # noqa: SIM115
            else:
                file_handle = StringIO(target)  # type: ignore
        file_content = file_handle.read()
        file_handle.close()
        self.records = list(file_content.split("\n\n"))
        self.iter = iter(self.records)

    def _parse_line(self, line: str) -> Union[Leader, Field]:
        """Parse a MARCMaker line.

        A line looks like
            =LDR  00755cam  22002414a 4500
            or
            =008  010314s1999fr||||||||||||||||fre
            or
            =028  00$aSTMA 8007$bTamla Motown Records
        """
        if line[0] != "=":
            raise ValueError('Line should start with a "=".')
        if line[4:6] != "  ":
            raise ValueError(
                "Tag should be separated from the rest of the field by two spaces."
            )
        tag = line[1:4]
        data = line[6:]
        if tag == "LDR":
            return Leader(data)
        elif tag < "010":
            return Field(tag, data=data)
        indicators = Indicators(data[0], data[1])
        # the first $ is ignored to avoid an empty list item after the split
        subfields: list[Subfield] = [
            Subfield(subfield[:1], subfield[1:]) for subfield in data[3:].split("$")
        ]
        return Field(tag, indicators=indicators, subfields=subfields)

    def __next__(self) -> Iterator:
        """Iterate over a record's line to parse the fields."""
        record_txt = next(self.iter)
        record = Record()
        for line in record_txt.splitlines():
            try:
                field = self._parse_line(line)
            except Exception as exc:
                raise exceptions.PymarcException(
                    f'Unable to parse line "{line}"'
                ) from exc
            if isinstance(field, Leader):
                record.leader = field
            else:
                record.add_field(field)
        return record

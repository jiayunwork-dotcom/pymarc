# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Pymarc Writer."""

import json
import xml.etree.ElementTree as ET
from typing import IO, Optional

import pymarc
from pymarc import Record, WriteNeedsRecord
from pymarc.linkage import LinkageIndex


class Writer:
    """Base Writer object."""

    def __init__(self, file_handle: IO) -> None:
        """Init."""
        self.file_handle = file_handle

    def write(self, record: Record) -> None:
        """Write."""
        if not isinstance(record, Record):
            raise WriteNeedsRecord

    def close(self, close_fh: bool = True) -> None:
        """Closes the writer.

        If close_fh is False close will also close the underlying file handle
        that was passed in to the constructor. The default is True.
        """
        if close_fh:
            self.file_handle.close()
        self.file_handle = None  # type: ignore


class JSONWriter(Writer):
    """A class for writing records as an array of MARC-in-JSON objects.

    IMPORTANT: You must the close a JSONWriter,
    otherwise you will not get valid JSON.

    Simple usage::

    .. code-block:: python

        from pymarc import JSONWriter

        # writing to a file
        writer = JSONWriter(open('file.json','wt'))
        writer.write(record)
        writer.close()  # Important!

        # writing to a string
        string = StringIO()
        writer = JSONWriter(string)
        writer.write(record)
        writer.close(close_fh=False)  # Important!
        print(string)
    """

    def __init__(self, file_handle: IO) -> None:
        """You need to pass in a text file like object."""
        super().__init__(file_handle)
        self.write_count = 0
        self.file_handle.write("[")

    def write(self, record: Record) -> None:
        """Writes a record."""
        Writer.write(self, record)
        if self.write_count > 0:
            self.file_handle.write(",")
        json.dump(record.as_dict(), self.file_handle, separators=(",", ":"))
        self.write_count += 1

    def close(self, close_fh: bool = True) -> None:
        """Closes the writer.

        If close_fh is False close will also close the underlying file
        handle that was passed in to the constructor. The default is True.
        """
        self.file_handle.write("]")
        Writer.close(self, close_fh)


class MARCWriter(Writer):
    """A class for writing MARC21 records in transmission format.

    Simple usage::

    .. code-block:: python

        from pymarc import MARCWriter

        # writing to a fileIO
        writer = MARCWriter(open('file.dat','wb'))
        writer.write(record)
        writer.close()

        # writing to a string (Python 2 only)
        string = StringIO()
        writer = MARCWriter(string)
        writer.write(record)
        writer.close(close_fh=False)
        print(string)

        # writing to memory (Python 3 only)

        memory = BytesIO()
        writer = MARCWriter(memory)
        writer.write(record)
        writer.close(close_fh=False)

    With ``strict_linkage=True`` each record's subfield 6 linkage is
    validated (see :class:`pymarc.linkage.LinkageIndex`) before anything is
    written; a record with invalid linkage raises
    :class:`InvalidLinkage <pymarc.exceptions.InvalidLinkage>`, no part of it
    is written, and the writer stays usable for the next record.

    Pass ``encoding="marc-8"`` to write MARC-8 field data (leader position 9
    a blank) instead of the default UTF-8/leader-driven output. The record is
    fully serialized in memory before its first byte is written, so a record
    that cannot be encoded as MARC-8 raises
    :class:`MARC8EncodingError <pymarc.exceptions.MARC8EncodingError>` (or
    :class:`RecordTooLargeError <pymarc.exceptions.RecordTooLargeError>`)
    with nothing written for that record; records already written stay, and
    the writer can be used for the next record. Both options may be combined.
    """

    def __init__(
        self,
        file_handle: IO,
        strict_linkage: bool = False,
        encoding: Optional[str] = None,
    ) -> None:
        """You need to pass in a byte file like object.

        `encoding` may be None (the default UTF-8/leader-driven behaviour) or
        ``"marc-8"``.
        """
        super().__init__(file_handle)
        if encoding is not None and encoding.lower() != "marc-8":
            raise ValueError(
                f"unsupported MARCWriter encoding {encoding!r}; use 'marc-8' "
                "or leave it unset"
            )
        self.strict_linkage = strict_linkage
        self.encoding = "marc-8" if encoding is not None else None

    def write(self, record: Record) -> None:
        """Writes a record.

        The record is serialized before anything is written, so on an
        encoding or record format error the file receives none of that
        record's bytes and the writer remains usable.
        """
        Writer.write(self, record)
        if self.strict_linkage:
            LinkageIndex(record, label="record")
        if self.encoding == "marc-8":
            data = record.as_marc(encoding="marc-8")
        else:
            data = record.as_marc()
        self.file_handle.write(data)


class TextWriter(Writer):
    """A class for writing records in prettified text MARCMaker format.

    A blank line separates each record.

    Simple usage:

    .. code-block:: python

        from pymarc import TextWriter

        # writing to a file
        writer = TextWriter(open('file.txt','wt'))
        writer.write(record)
        writer.close()

        # writing to a string
        string = StringIO()
        writer = TextWriter(string)
        writer.write(record)
        writer.close(close_fh=False)
        print(string)
    """

    def __init__(self, file_handle: IO) -> None:
        """You need to pass in a text file like object."""
        super().__init__(file_handle)
        self.write_count = 0

    def write(self, record: Record) -> None:
        """Writes a record."""
        Writer.write(self, record)
        if self.write_count > 0:
            self.file_handle.write("\n")
        self.file_handle.write(str(record))
        self.write_count += 1


class XMLWriter(Writer):
    """A class for writing records as a MARCXML collection.

    IMPORTANT: You must then close an XMLWriter, otherwise you will not get
    a valid XML document.

    Simple usage:

    .. code-block:: python

        from pymarc import XMLWriter

        # writing to a file
        writer = XMLWriter(open('file.xml','wb'))
        writer.write(record)
        writer.close()  # Important!

        # writing to a string (Python 2 only)
        string = StringIO()
        writer = XMLWriter(string)
        writer.write(record)
        writer.close(close_fh=False)  # Important!
        print(string)

        # writing to memory (Python 3 only)
        memory = BytesIO()
        writer = XMLWriter(memory)
        writer.write(record)
        writer.close(close_fh=False)  # Important!

    With ``strict_linkage=True`` each record's subfield 6 linkage is
    validated (see :class:`pymarc.linkage.LinkageIndex`) before anything is
    written; a record with invalid linkage raises
    :class:`InvalidLinkage <pymarc.exceptions.InvalidLinkage>`, no part of it
    is written, and the writer stays usable for the next record.
    """

    def __init__(self, file_handle: IO, strict_linkage: bool = False) -> None:
        """You need to pass in a binary file like object."""
        super().__init__(file_handle)
        self.strict_linkage = strict_linkage
        self.file_handle.write(b'<?xml version="1.0" encoding="UTF-8"?>')
        self.file_handle.write(b'<collection xmlns="http://www.loc.gov/MARC21/slim">')

    def write(self, record: Record) -> None:
        """Writes a record."""
        Writer.write(self, record)
        if self.strict_linkage:
            LinkageIndex(record, label="record")
        node = pymarc.record_to_xml_node(record)
        self.file_handle.write(ET.tostring(node, encoding="utf-8"))

    def close(self, close_fh: bool = True) -> None:
        """Closes the writer.

        If close_fh is False close will also close the underlying file handle
        that was passed in to the constructor. The default is True.
        """
        self.file_handle.write(b"</collection>")
        Writer.close(self, close_fh)

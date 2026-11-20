# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Handle MARC-8 files.

see http://www.loc.gov/marc/specifications/speccharmarc8.html
"""

import sys
import unicodedata
from typing import Optional

from pymarc import marc8_mapping
from pymarc.exceptions import MARC8EncodingError


def marc8_to_unicode(marc8, hide_utf8_warnings: bool = False) -> str:
    """Pass in a string, and get back a Unicode object.

    .. code-block:: python

        print marc8_to_unicode(record.title())
    """
    # XXX: might be good to stash away a converter somehow
    # instead of always re-creating it
    converter = MARC8ToUnicode(quiet=hide_utf8_warnings)
    try:
        return converter.translate(marc8)
    except IndexError as err:
        # convert IndexError into UnicodeDecodeErrors
        raise UnicodeDecodeError(
            "marc8_to_unicode",
            marc8,
            0,
            len(marc8),
            "invalid multibyte character encoding",
        ) from err
    except TypeError as err:
        # convert TypeError into UnicodeDecodeErrors
        raise UnicodeDecodeError(
            "marc8_to_unicode",
            marc8,
            0,
            len(marc8),
            "invalid multibyte character encoding",
        ) from err


class MARC8ToUnicode:
    """Converts MARC-8 to Unicode.

    Note that currently, unicode strings aren't normalized, and some codecs (e.g.
    iso8859-1) will fail on such strings.  When I can require python 2.3, this will go
    away.

    Warning: MARC-8 EACC (East Asian characters) makes some
    distinctions which aren't captured in Unicode.  The LC tables give
    the option of mapping such characters either to a Unicode private
    use area, or a substitute character which (usually) gives the
    sense.  I've picked the second, so this means that the MARC data
    should be treated as primary and the Unicode data used for display
    purposes only.  (If you know of either of fonts designed for use
    with LC's private-use Unicode assignments, or of attempts to
    standardize Unicode characters to allow round-trips from EACC,
    or if you need the private-use Unicode character translations,
    please inform me, asl2@pobox.com.
    """

    basic_latin = 0x42
    ansel = 0x45

    def __init__(
        self, G0: int = basic_latin, G1: int = ansel, quiet: bool = False
    ) -> None:
        """Init."""
        self.g0 = G0
        self.g0_set = {b"(", b",", b"$"}
        self.g1 = G1
        self.g1_set = {b")", b"-", b"$"}
        self.quiet = quiet

    def translate(self, marc8_string):
        """Translate."""
        # don't choke on empty marc8_string
        if not marc8_string:
            return ""
        uni_list = []
        combinings = []
        pos = 0
        while pos < len(marc8_string):
            # http://www.loc.gov/marc/specifications/speccharmarc8.html
            if marc8_string[pos : pos + 1] == b"\x1b":
                next_byte = marc8_string[pos + 1 : pos + 2]
                if next_byte in self.g0_set:
                    if len(marc8_string) >= pos + 3:
                        if (
                            marc8_string[pos + 2 : pos + 3] == b","
                            and next_byte == b"$"
                        ):
                            pos += 1
                        self.g0 = ord(marc8_string[pos + 2 : pos + 3])
                        pos = pos + 3
                        continue
                    else:
                        # if there aren't enough remaining characters, readd
                        # the escape character so it doesn't get lost; may
                        # help users diagnose problem records
                        uni_list.append(marc8_string[pos : pos + 1].decode("ascii"))
                        pos += 1
                        continue

                elif next_byte in self.g1_set:
                    if marc8_string[pos + 2 : pos + 3] == b"-" and next_byte == b"$":
                        pos += 1
                    self.g1 = ord(marc8_string[pos + 2 : pos + 3])
                    pos = pos + 3
                    continue
                else:
                    charset = ord(next_byte)
                    if charset in marc8_mapping.CODESETS:
                        self.g0 = charset
                        pos += 2
                    elif charset == 0x73:
                        self.g0 = self.basic_latin
                        pos += 2
                        if pos == len(marc8_string):
                            break

            def is_multibyte(charset):
                return charset == 0x31

            mb_flag = is_multibyte(self.g0)

            if mb_flag:
                # conditional check if string longer than pos+3 because of malformed marc8 string
                if len(marc8_string) < pos + 3:
                    sys.stderr.write(
                        f"Multi-byte position {pos + 3} exceeds length of marc8 string {len(marc8_string)}\n"
                    )
                    code_point = 32  # Sets last character as a blank string
                else:
                    code_point = (
                        ord(marc8_string[pos : pos + 1]) * 65536
                        + ord(marc8_string[pos + 1 : pos + 2]) * 256
                        + ord(marc8_string[pos + 2 : pos + 3])
                    )
                pos += 3
            else:
                code_point = ord(marc8_string[pos : pos + 1])
                pos += 1

            if code_point < 0x20 or 0x80 < code_point < 0xA0:
                uni = chr(code_point)
                continue

            try:
                if code_point > 0x80 and not mb_flag:
                    (uni, cflag) = marc8_mapping.CODESETS[self.g1][code_point]
                else:
                    (uni, cflag) = marc8_mapping.CODESETS[self.g0][code_point]
            except KeyError:
                try:
                    uni = marc8_mapping.ODD_MAP[code_point]
                    uni_list.append(chr(uni))
                    # we can short circuit because we know these mappings
                    # won't be involved in combinings.  (i hope?)
                    continue
                except KeyError:
                    pass
                if not self.quiet:
                    sys.stderr.write(
                        f"Unable to parse character 0x{code_point:x} in g0={self.g0} g1={self.g1}\n"
                    )
                uni = ord(" ")
                cflag = False

            if cflag:
                combinings.append(chr(uni))
            else:
                uni_list.append(chr(uni))
                if len(combinings) > 0:
                    uni_list.extend(combinings)
                    combinings = []

        # what to do if combining chars left over?
        uni_str = "".join(uni_list)
        return unicodedata.normalize("NFC", uni_str)


#: Default G0 designation: Basic Latin (ASCII).
MARC8_G0_DEFAULT = 0x42
#: Default G1 designation: Extended Latin (ANSEL).
MARC8_G1_DEFAULT = 0x45

#: Charsets that occupy the G1 area (their codes are the 0x80-0xFF bytes).
G1_CHARSETS = frozenset({0x45, 0x51, 0x34})

#: ISO 2022 escape sequences used to designate each MARC-8 charset.
ESCAPE_SEQUENCES = {
    0x42: b"\x1b(B",  # Basic Latin
    0x45: b"\x1b)E",  # Extended Latin (ANSEL)
    0x4E: b"\x1b(N",  # Basic Cyrillic
    0x51: b"\x1b)Q",  # Extended Cyrillic
    0x53: b"\x1b(S",  # Basic Greek
    0x32: b"\x1b(2",  # Basic Hebrew
    0x33: b"\x1b(3",  # Basic Arabic
    0x34: b"\x1b)4",  # Extended Arabic
    0x70: b"\x1b(p",  # Superscripts
    0x62: b"\x1b(b",  # Subscripts
    0x31: b"\x1b$1",  # EACC (three byte characters)
}

#: Order in which charsets are consulted when building the reverse map.
#: Earlier entries win when the same Unicode character exists in several
#: charsets, which keeps ASCII (and therefore the common case) first.
_CHARSET_PRIORITY = (
    0x42,  # Basic Latin
    0x45,  # ANSEL
    0x4E,  # Basic Cyrillic
    0x51,  # Extended Cyrillic
    0x53,  # Basic Greek
    0x32,  # Basic Hebrew
    0x33,  # Basic Arabic
    0x34,  # Extended Arabic
    0x70,  # Superscripts
    0x62,  # Subscripts
    0x31,  # EACC
)

#: Preferences for combining marks when the base character's own charset has
#: no mapping for the mark.
_MARK_CHARSET_PREFERENCE = (0x45, 0x34, 0x53)

# Build the reverse maps once, lazily. Nothing here is keyed by record content,
# so exporting many records does not grow these structures.
_direct_map: dict[int, tuple[int, bytes]] = {}
_mark_map: dict[int, list[tuple[int, bytes]]] = {}
_maps_built = False


def _build_reverse_maps() -> None:
    global _maps_built
    if _maps_built:
        return
    direct: dict[int, tuple[int, bytes]] = {}
    marks: dict[int, list[tuple[int, bytes]]] = {}
    combining_passthrough: dict[int, tuple[int, bytes]] = {}

    for charset in _CHARSET_PRIORITY:
        table = marc8_mapping.CODESETS[charset]
        for code, (codepoint, combining) in table.items():
            if charset == 0x31:
                # EACC characters are three bytes; some (the "odd" III
                # characters handled below) contain a space or 0x7F byte.
                value = code.to_bytes(3, "big")
            else:
                # Skip C0/C1 control bytes: they are not character data and
                # the decoder deliberately passes them through unchanged.
                # (The ANSEL bytes 0x88/0x89/0x8D/0x8E are skipped too: the
                # decoder treats the 0x80-0x9F range as control bytes and
                # never reaches their table entries, so emitting them would
                # not round-trip.)
                if code < 0x20 or 0x7F <= code <= 0x9F:
                    continue
                value = bytes([code])
            if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
                continue
            if combining:
                normalised_mark = unicodedata.normalize("NFC", chr(codepoint))
                mark_key = (
                    ord(normalised_mark)
                    if len(normalised_mark) == 1
                    else codepoint
                )
                candidates = marks.setdefault(mark_key, [])
                if all(known != charset for known, _ in candidates):
                    candidates.append((charset, value))
                continue
            normalised = unicodedata.normalize("NFC", chr(codepoint))
            if len(normalised) != 1:
                # MARC-8 has code points that Unicode normalisation would
                # split (for instance the Greek ano teleia). Emit them as
                # given; the reader composes them back to this same string.
                combining_passthrough[codepoint] = (charset, value)
                continue
            direct.setdefault(ord(normalised), (charset, value))

    # The "odd" III characters (ellipsis, quotes, dashes, trademark) are only
    # in ODD_MAP on the decoding side. Emit their EACC triple so text that
    # contains them round-trips through the same reader.
    for code, codepoint in marc8_mapping.ODD_MAP.items():
        direct.setdefault(codepoint, (0x31, code.to_bytes(3, "big")))

    for codepoint, unit in combining_passthrough.items():
        direct.setdefault(codepoint, unit)

    _direct_map.update(direct)
    _mark_map.update(marks)
    _maps_built = True


def _is_mark(char: str) -> bool:
    """True for a combining mark that belongs to the preceding base char."""
    return unicodedata.category(char).startswith("M") or bool(
        unicodedata.combining(char)
    )


def _canonical_order(
    decomposed: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    """Order a canonical decomposition the way Unicode normalisation does.

    Base characters keep their relative order; combining marks are sorted by
    combining class within their run. The offsets travel with the characters
    so errors still point at the right place in the original string.
    """
    result: list[tuple[str, int]] = []
    index = 0
    while index < len(decomposed):
        char, offset = decomposed[index]
        if unicodedata.combining(char) == 0:
            result.append((char, offset))
            index += 1
            continue
        end = index
        while end < len(decomposed) and unicodedata.combining(
            decomposed[end][0]
        ):
            end += 1
        result.extend(
            sorted(decomposed[index:end], key=lambda item: unicodedata.combining(item[0]))
        )
        index = end
    return result


def _is_hangul_jamo(char: str) -> bool:
    """True for conjoining Hangul jamo, which compose into a syllable."""
    codepoint = ord(char)
    return (
        0x1100 <= codepoint <= 0x11FF
        or 0xA960 <= codepoint <= 0xA97F
        or 0xD7B0 <= codepoint <= 0xD7FF
    )


def _check_control_characters(text: str) -> None:
    """Reject the MARC delimiter/escape control characters in field data."""
    for offset, char in enumerate(text):
        if char in ("\x1b", "\x1d", "\x1e", "\x1f"):
            raise MARC8EncodingError(
                "reserved MARC control character", char_offset=offset, character=char
            )


class UnicodeToMARC8:
    """Encode Unicode strings as MARC-8.

    The encoder is stateful only for the duration of a single :meth:`encode`
    call. Each call starts with G0 designated to Basic Latin and G1 to ANSEL,
    and ends by returning both to those defaults, so escape state and pending
    combining marks never leak into the next subfield, field or record.

    Behaviour worth knowing:

    * Text is handled a base character at a time. Canonically equivalent
      precomposed and decomposed spellings are accepted because each base and
      its combining marks are normalised before a mapping is looked up.
    * MARC-8 places combining marks before their base character; the encoder
      does the same. A mark with no base character in the same string is an
      error.
    * A mark is written in the base character's own charset when that charset
      has it (Greek, Hebrew, Arabic, extended Arabic), otherwise in ANSEL,
      which lives in G1 and needs no G0 escape.
    * Characters that no mapping can represent raise
      :class:`MARC8EncodingError` instead of being replaced or dropped.
    * The escape state is G0/G1 designations only. MARC-8 has no ESC sequences
      for the ANSEL combining marks, so no mark state can outlive the subfield.
    """

    def __init__(self) -> None:
        _build_reverse_maps()
        self._out = bytearray()
        self.g0 = MARC8_G0_DEFAULT
        self.g1 = MARC8_G1_DEFAULT

    def reset(self) -> None:
        """Return to the default G0/G1 designations."""
        self._out = bytearray()
        self.g0 = MARC8_G0_DEFAULT
        self.g1 = MARC8_G1_DEFAULT

    def encode(self, text: str) -> bytes:
        """Encode `text` as a self contained MARC-8 byte string."""
        if not isinstance(text, str):
            raise MARC8EncodingError(
                f"expected str field data, got {type(text).__name__}"
            )
        self.reset()
        if not text:
            return b""
        _check_control_characters(text)

        position = 0
        length = len(text)
        while position < length:
            start = position
            char = text[position]
            if _is_mark(char):
                # A mark that starts the string may still have its own
                # non-combining table entry (the reader emits such entries
                # in place). Otherwise it has no base to attach to.
                if ord(char) in _direct_map and ord(char) not in _mark_map:
                    position += 1
                    self._encode_segment(char, start)
                    continue
                raise MARC8EncodingError(
                    "combining mark without a base character",
                    char_offset=start,
                    character=char,
                )
            position += 1
            if _is_hangul_jamo(char):
                while position < length and (
                    _is_mark(text[position]) or _is_hangul_jamo(text[position])
                ):
                    position += 1
            else:
                while position < length and _is_mark(text[position]):
                    position += 1
            self._encode_segment(text[start:position], start)

        self._return_to_defaults()
        return bytes(self._out)

    # -- internals ---------------------------------------------------------

    def _encode_segment(self, segment: str, start: int) -> None:
        """Encode one base character together with its combining marks.

        The cluster is decomposed canonically and the longest prefix that
        composes to a character with a direct mapping is used as the base
        (so "u" + horn + dot below is written as ANSEL dot below plus the
        precomposed u-with-horn). Remaining marks are written before the
        base, as MARC-8 expects.
        """
        composed = unicodedata.normalize("NFC", segment)
        if len(composed) == 1:
            unit = _direct_map.get(ord(composed))
            if unit is not None:
                self._emit(unit[0], unit[1])
                return

        # Build the canonical decomposition, keeping each character's offset
        # in the original string for error reporting.
        decomposed: list[tuple[str, int]] = []
        for offset in range(len(segment)):
            for char in unicodedata.normalize("NFD", segment[offset]):
                decomposed.append((char, start + offset))
        decomposed = _canonical_order(decomposed)

        base_unit: Optional[tuple[int, bytes]] = None
        mark_start = len(decomposed)
        for split in range(len(decomposed), 0, -1):
            candidate = unicodedata.normalize(
                "NFC", "".join(char for char, _ in decomposed[:split])
            )
            if len(candidate) == 1:
                unit = _direct_map.get(ord(candidate))
                if unit is not None:
                    base_unit = unit
                    mark_start = split
                    break
        if base_unit is None:
            char, offset = decomposed[0]
            raise MARC8EncodingError(
                "character has no MARC-8 mapping",
                char_offset=offset,
                character=char,
            )

        base_charset = base_unit[0]
        for mark, offset in decomposed[mark_start:]:
            candidates = _mark_map.get(ord(mark))
            if not candidates:
                # Some combining characters in Unicode are stored in the
                # MARC-8 tables without a combining flag (the reader then
                # emits them in place). They can still follow the base.
                standalone = _direct_map.get(ord(mark))
                if standalone is None:
                    raise MARC8EncodingError(
                        "combining mark has no MARC-8 mapping",
                        char_offset=offset,
                        character=mark,
                    )
                self._emit(*standalone)
                continue
            charset, value = self._choose_mark(base_charset, candidates)
            self._emit(charset, value)

        self._emit(*base_unit)

    @staticmethod
    def _choose_mark(
        base_charset: int, candidates: list[tuple[int, bytes]]
    ) -> tuple[int, bytes]:
        """Pick the encoding of a mark that minimises charset changes."""
        for charset, value in candidates:
            if charset == base_charset:
                return charset, value
        for preferred in _MARK_CHARSET_PREFERENCE:
            for charset, value in candidates:
                if charset == preferred:
                    return charset, value
        return candidates[0]

    def _emit(self, charset: int, value: bytes) -> None:
        """Append `value`, designating `charset` first when needed."""
        if charset == 0x31:
            if self.g0 != 0x31:
                self._out += ESCAPE_SEQUENCES[0x31]
                self.g0 = 0x31
            self._out += value
            return

        # Any single-byte character needs G0 out of the multi-byte state,
        # otherwise the decoder would read three bytes.
        if self.g0 == 0x31:
            self._out += ESCAPE_SEQUENCES[MARC8_G0_DEFAULT]
            self.g0 = MARC8_G0_DEFAULT

        if charset in G1_CHARSETS:
            if self.g1 != charset:
                self._out += ESCAPE_SEQUENCES[charset]
                self.g1 = charset
        elif self.g0 != charset:
            self._out += ESCAPE_SEQUENCES[charset]
            self.g0 = charset
        self._out += value

    def _return_to_defaults(self) -> None:
        """Restore the default designations at the end of a subfield."""
        if self.g0 == 0x31 or self.g0 != MARC8_G0_DEFAULT:
            self._out += ESCAPE_SEQUENCES[MARC8_G0_DEFAULT]
            self.g0 = MARC8_G0_DEFAULT
        if self.g1 != MARC8_G1_DEFAULT:
            self._out += ESCAPE_SEQUENCES[MARC8_G1_DEFAULT]
            self.g1 = MARC8_G1_DEFAULT


def unicode_to_marc8(text: str) -> bytes:
    """Encode a Unicode string as MARC-8.

    .. code-block:: python

        unicode_to_marc8("Léon")  # b'\\xe2Leon'

    Raises :class:`MARC8EncodingError
    <pymarc.exceptions.MARC8EncodingError>` for characters without a MARC-8
    mapping, combining marks without a base character, and MARC control
    characters in the text.
    """
    return UnicodeToMARC8().encode(text)

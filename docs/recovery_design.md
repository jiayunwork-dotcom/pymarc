# MARC batch recovery: design notes

This note explains how `MARCReader` handles a corrupted MARC21 batch, which
recovery strategies were compared, and what the accepted trade-offs are.
The implementation lives in `pymarc/reader.py` (streaming and recovery),
`pymarc/frame.py` (structural validation) and `pymarc/exceptions.py`
(`FramingError` / `ContentError` split). A runnable demonstration is in
`examples/recovery_demo.py`.

## Two kinds of failure

A MARC21 frame is a length header, a 24-byte leader, a directory of 12-byte
entries terminated by `0x1E`, and field data terminated by `0x1D`. Failures
fall into two classes that must be handled differently:

* **Framing failure** — the reader cannot establish where this record ends
  and the next one begins (non-numeric length header, a declared length that
  is too short/long, a missing end-of-record marker, an out-of-bounds
  directory discovered while scanning). The position of the next record is
  unknown, so recovery requires looking ahead. These errors derive from
  `FramingError` and carry the half-open byte range of the damaged region.
* **Content failure** — the frame boundary itself is trustworthy (the
  declared length lands on `0x1D` and the frame is internally addressable),
  but its payload cannot be used, e.g. a `UnicodeDecodeError` in field text.
  The next record still starts exactly one frame later, so the record is
  rejected and iteration continues without any scanning. A pseudo-record
  embedded in such a payload can never be mistaken for a real record,
  because no scanning happens inside a trusted frame.

## Frame validation is shared

`frame.validate_frame()` encodes the structural rules once and is used both
by `Record.decode_marc()` (direct construction) and by the reader's recovery
scanner, so a frame is accepted or rejected identically in both paths. It
checks, without decoding any field text:

1. the leader is 24 ASCII bytes and its length field is numeric;
2. the base address is numeric, positive, and inside the record;
3. the directory ends on `0x1E` and is a whole number of 12-byte entries;
4. **every directory entry addresses a byte range wholly inside the field
   area** — an out-of-bounds slice is rejected (`FieldOutOfBounds`) instead
   of silently swallowing `0x1E`/`0x1D` terminators or neighboring records;
5. optionally (strict mode), every entry ends on an `0x1E` field terminator.

The strict terminator check is reserved for recovery anchoring. Direct
`Record` construction keeps the historical tolerance for in-range,
mis-sized field lengths found in some real-world catalogs
(`test/bad_subfield_code.dat`); what is always rejected is an entry pointing
outside the record, which is the actual terminator-leak vulnerability.

Text is never decoded during validation, so the check is cheap and a record
whose only problem is bad UTF-8 still has a valid frame and fails later as a
content error.

## Recovery strategies compared

Two practical strategies were considered for locating the next record after
a framing failure. Both are streaming (the whole batch is never loaded to
find boundaries, and consumed bytes are never requested again).

### Strategy A — terminator scan

From the lost boundary, scan raw bytes for the next `0x1D`
(end-of-record), then try to parse a record ending there.

* *False-positive risk: high.* `0x1D` can appear inside damaged bytes or
  even inside field data of a misframed record, and a five-digit number in
  front of it is not evidence of a leader. The task explicitly states that
  an encountered record terminator is not sufficient proof of a record.
* *Read latency / buffering: low.* One forward byte scan, small buffer.
* *Re-parse cost: low per hit, but many bogus hits would each be parsed.*

### Strategy B — header-and-strict-validation scan (chosen)

From the lost boundary, consider each position whose next five bytes are
ASCII digits as a candidate record start. Read enough bytes to cover the
frame it declares, and accept it only when `probe_candidate()` passes:
the declared frame ends on `0x1D` *and* strict structural validation
(base address, directory bounds, field terminators) succeeds.

* *False-positive risk: low.* A corrupt region can contain a digit run, but
  a full, self-consistent directory whose every entry addresses in-range,
  terminator-delimited fields and whose declared length ends exactly on
  `0x1D` is extremely unlikely by chance. The terminator alone never
  qualifies; neither does the five-digit header alone.
* *Read latency / buffering: moderate.* Candidates may declare large
  lengths, so the window can grow to cover one candidate frame. Reads stay
  bounded to `READ_CHUNK_SIZE` (64 KiB) per call, and once a candidate is
  rejected scanning resumes byte-by-byte. The buffered window is bounded by
  the largest single candidate frame encountered, not by total input size.
* *Re-parse cost: structural validation is text-free and linear in frame
  size; only digit positions are probed, and each position is probed once.

### Conservative handling of ambiguity

* The **first** position that yields a fully valid frame anchors recovery;
  bytes before it are reported as exactly one damaged region.
* A candidate whose frame is not fully buffered and EOF has been reached is
  skipped; later, complete candidates are still considered.
* If no valid frame start appears within `recovery_limit` bytes of the lost
  boundary, `RecoveryLimitExceeded` is raised and iteration ends, so a
  corrupt stream can never be consumed without bound.
* When multiple interpretations remain possible and none validates, the
  reader never guesses — it treats the bytes as damaged and keeps scanning
  rather than emitting a speculative record.

## Streaming and short reads

The input is wrapped in an internal byte buffer with an absolute stream
offset. `_fill(n)` loops on `read()` until `n` bytes are buffered or the
stream returns `b""`; a short read is simply followed by another read, so a
valid record never becomes a truncation error merely because the stream
hands over 1–3 bytes at a time. No `seek()`/`tell()` is used. Each
`read()` request is capped at 64 KiB regardless of record or damage size,
which is asserted by the tests (`RecoveryLimitTest.test_long_damage_keeps_
read_requests_bounded`).

## Diagnostics

* `current_exception` — the failure; framing failures are `FramingError`
  subclasses, content failures (e.g. `UnicodeDecodeError`) are not.
* `current_chunk` — the bytes of the failed record or damaged region.
* `current_range` — the half-open `(start, end)` byte range relative to the
  start of the reader's input; framing exceptions expose the same range via
  `byte_range`.

A contiguous damaged region produces exactly one `None`. Good records are
delivered in original order, each once, and no byte of a good record is
included in a damaged region. At true EOF a remaining bad tail is reported
once; subsequent `next()` calls raise `StopIteration` without further reads.

## Limitations

* Recovery trusts structural consistency, not semantic cataloging rules; a
  syntactically valid but bibliographically wrong frame will be accepted.
* The recovery window is measured from the first byte of the damaged
  region; a valid record start just past `recovery_limit` is not found until
  a new framing attempt begins (and by then the limit exception has ended
  iteration, as specified).
* The optional strict field-terminator rule is only used for recovery
  anchoring; direct `Record` construction retains the historical tolerance
  for in-range mis-sized entries to avoid breaking existing fixtures.
* Only synchronous binary streams are supported; non-blocking streams that
  signal "no data yet" without EOF are out of scope.

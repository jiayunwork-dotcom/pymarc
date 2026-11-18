# This file is part of pymarc. It is subject to the license terms in the
# LICENSE file found in the top-level directory of this distribution and at
# https://opensource.org/licenses/BSD-2-Clause. pymarc may be copied, modified,
# propagated, or distributed according to the terms contained in the LICENSE
# file.

"""Subfield 6 linkage groups and group migration between records.

A *linkage group* is a regular field together with the 880 Alternate Graphic
Representation fields linked to it through subfield 6: the regular field's
subfield 6 points to 880 (e.g. "880-01"), each 880's subfield 6 points back
to the regular field's tag (e.g. "245-01"), and every member of the group
carries the same non-zero two digit occurrence number. One regular field may
head a group of several 880s (different scripts). An 880 whose occurrence
number is 00 has no associated regular field: it stays where it is and does
not occupy an allocatable number. In records handled here every non-zero
number belongs to exactly one group.

:class:`LinkageIndex` scans a record once, validates those rules and raises
:class:`InvalidLinkage <pymarc.exceptions.InvalidLinkage>` listing every
problem (duplicate ownership, mismatched ends, duplicate or malformed
subfield 6) with the record's label and the field positions, so a human can
look the offenders up. The same index backs ``Record.get_linked_fields(...,
strict=True)``, the migration plan below and the ``strict_linkage=True``
writers, so all of them read a subfield 6 the same way.

Migration works in two phases::

    plan = preview_migration(source, target, [
        ImportGroup("01"),                       # add source group 01
        ReplaceGroup(target="03", source="07"),  # replace target group 03
    ])
    print(plan)   # the cataloger reviews fields, numbers and replacements
    plan.apply()  # re-validates, then mutates the target record

preview_migration() never mutates either record. The plan stores, per
operation: isolated (already renumbered) copies of the fields to import, a
content snapshot of the source unit and of any replaced target unit, and the
occurrence number assigned in the target. apply() rescans both records once
and compares those facts before touching the target:

* edits to a unit that is imported or replaced, occupation of an assigned
  number, or newly invalid linkage raise MigrationConflict / InvalidLinkage
  and nothing is written;
* unrelated edits (say an added note field) do not conflict; the plan is
  applied as previewed and the edit is preserved.

Consequences of storing exactly these facts: mid-flight changes are caught
where they matter and ignored where they do not; repeated previews are cheap
and deterministic (same inputs, same assigned numbers) and applying one plan
makes a sibling plan conflict rather than double-apply; temporary space is
proportional to the imported fields, since no whole-record copies are taken.
"""

from typing import TYPE_CHECKING, Any, NamedTuple, Optional, Union

from pymarc.exceptions import (
    InvalidLinkage,
    LinkageNumbersExhausted,
    LinkageProblem,
    MigrationConflict,
)
from pymarc.field import Field, Indicators, Subfield, parse_linkage

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pymarc.record import Record

__all__ = [
    "FieldGroup",
    "ImportGroup",
    "LinkageIndex",
    "MigrationPlan",
    "ReplaceGroup",
    "preview_migration",
]

#: Occurrence number marking an 880 with no associated regular field.
STANDALONE_NUMBER = "00"

#: How a unit to migrate is selected: an occurrence number ("07"), a Field
#: of the record, or a FieldGroup from a LinkageIndex of the record.
Selector = Union[str, Field, "FieldGroup"]


class FieldGroup:
    """One selectable unit in a record: a field plus its linked 880 alternates.

    `number` is the two digit occurrence number shared by the group's
    subfield 6 values, or None for an unlinked singleton field. `anchor` is
    the regular (non-880) field, `alternates` the linked 880 fields and
    `fields` every member in the order the members appear in the record.
    """

    __slots__ = ("number", "anchor", "alternates", "fields")

    def __init__(
        self,
        number: Optional[str],
        anchor: Field,
        alternates: "Sequence[Field]",
        fields: "Sequence[Field]",
    ) -> None:
        """Initialize a FieldGroup; use LinkageIndex rather than building these."""
        self.number = number
        self.anchor = anchor
        self.alternates = tuple(alternates)
        self.fields = tuple(fields)

    @property
    def linked(self) -> bool:
        """True when the group is held together by subfield 6 linkage."""
        return self.number is not None

    def __str__(self) -> str:
        head = f"group {self.number}" if self.linked else "unlinked field"
        return head + ": " + " | ".join(str(f) for f in self.fields)


class LinkageIndex:
    """A validated view of a record's subfield 6 linkage, built in one pass.

    `source` may be a Record or any sequence of Field objects; `label`
    identifies the record in error messages. Raises InvalidLinkage listing
    every problem found. After a clean scan `groups` maps each non-zero
    occurrence number to its FieldGroup and `standalone` lists the 880
    fields whose occurrence number is 00.
    """

    def __init__(self, source: Any, label: str = "record") -> None:
        """Scan and validate `source`; raise InvalidLinkage on any problem."""
        fields = getattr(source, "fields", source)
        self.label = label
        self.groups: dict[str, FieldGroup] = {}
        self.standalone: list[Field] = []
        self._field_ids: set[int] = set()
        self._group_by_field_id: dict[int, FieldGroup] = {}
        problems: list[LinkageProblem] = []
        anchors: dict[str, list] = {}
        alternates: dict[str, list] = {}
        for position, field in enumerate(fields):
            self._field_ids.add(id(field))
            if field.control_field:
                continue
            sixes = [s.value for s in field.subfields if s.code == "6"]
            if len(sixes) > 1:
                problems.append(
                    LinkageProblem(label, position, field.tag, "duplicate subfield 6")
                )
                continue
            if not sixes:
                if field.tag == "880":
                    problems.append(
                        LinkageProblem(
                            label, position, "880", "880 field without subfield 6"
                        )
                    )
                continue
            try:
                link = parse_linkage(sixes[0])
            except ValueError as err:
                problems.append(LinkageProblem(label, position, field.tag, str(err)))
                continue
            if field.tag == "880":
                if link.number == STANDALONE_NUMBER:
                    self.standalone.append(field)
                elif link.tag == "880":
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            "880",
                            f"880 field links to 880 ({sixes[0]!r})",
                        )
                    )
                else:
                    alternates.setdefault(link.number, []).append(
                        (position, field, link)
                    )
            else:
                if link.tag != "880":
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            field.tag,
                            f"non-880 field links to {link.tag} instead of 880 ({sixes[0]!r})",
                        )
                    )
                elif link.number == STANDALONE_NUMBER:
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            field.tag,
                            "non-880 field uses occurrence number 00",
                        )
                    )
                else:
                    anchors.setdefault(link.number, []).append((position, field, link))
        for number in sorted(set(anchors) | set(alternates)):
            claims = anchors.get(number, [])
            alts = alternates.get(number, [])
            if len(claims) > 1:
                for position, field, _link in claims:
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            field.tag,
                            f"occurrence number {number} claimed by more than one field",
                        )
                    )
                continue
            if not claims:
                for position, _field, link in alts:
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            "880",
                            f"880 field links to {link.tag}-{number} but no such field exists",
                        )
                    )
                continue
            ((anchor_position, anchor, _link),) = claims
            good = [(p, f) for p, f, link in alts if link.tag == anchor.tag]
            for position, _field, link in alts:
                if link.tag != anchor.tag:
                    problems.append(
                        LinkageProblem(
                            label,
                            position,
                            "880",
                            f"880 field links back to {link.tag} but the group anchor"
                            f" is {anchor.tag} (field #{anchor_position})",
                        )
                    )
            if not good:
                problems.append(
                    LinkageProblem(
                        label,
                        anchor_position,
                        anchor.tag,
                        f"no 880 field links back to {anchor.tag}-{number}",
                    )
                )
                continue
            ordered = [f for _p, f in sorted([(anchor_position, anchor)] + good)]
            group = FieldGroup(
                number,
                anchor,
                [f for f in ordered if f.tag == "880"],
                ordered,
            )
            self.groups[number] = group
            for field in group.fields:
                self._group_by_field_id[id(field)] = group
        if problems:
            raise InvalidLinkage(problems)

    def group_for_number(self, number: str) -> Optional[FieldGroup]:
        """Return the linked group with occurrence `number`, or None."""
        return self.groups.get(number)

    def unit_for_field(self, field: Field) -> Optional[FieldGroup]:
        """Return the FieldGroup `field` belongs to, or None.

        A member of a linked group returns that group; an unlinked non-880
        field returns a singleton group; a standalone 880 (occurrence number
        00) or a field of another record returns None.
        """
        if id(field) not in self._field_ids:
            return None
        group = self._group_by_field_id.get(id(field))
        if group is not None:
            return group
        if field.tag == "880":
            return None
        return FieldGroup(None, field, (), (field,))

    def free_numbers(self) -> "Iterator[str]":
        """Yield the allocatable occurrence numbers 01-99 not used by any group."""
        for i in range(1, 100):
            number = f"{i:02d}"
            if number not in self.groups:
                yield number


class ImportGroup(NamedTuple):
    """Plan operation: import a unit from the source record as a new group.

    `source` selects the unit (see module docs for selector forms). A linked
    group is assigned the lowest free occurrence number in the target; an
    unlinked field is copied as is.
    """

    source: Selector


class ReplaceGroup(NamedTuple):
    """Plan operation: replace a unit of the target record with one from the source.

    A linked incoming group reuses the replaced group's occurrence number;
    when the replaced unit had no number, the lowest free number is assigned.
    """

    target: Selector
    source: Selector


def _field_facts(field: Field) -> tuple:
    """Return a hashable snapshot of a field's content."""
    if field.control_field:
        return (field.tag, None, field.data)
    return (field.tag, tuple(field.indicators or ()), tuple(field.subfields))


def _group_facts(group: FieldGroup) -> tuple:
    """Return a hashable snapshot of a group's content in record order."""
    return tuple(_field_facts(f) for f in group.fields)


def _copy_field(field: Field) -> Field:
    """Copy a field so the copy shares no mutable state with the original."""
    if field.control_field:
        return type(field)(tag=field.tag, data=field.data)
    return type(field)(
        tag=field.tag,
        indicators=Indicators(field.indicator1, field.indicator2),
        subfields=list(field.subfields),
    )


def _payload(group: FieldGroup, number: Optional[str]) -> tuple:
    """Copy a group's fields, rewriting each subfield 6 to `number` if given.

    The tag and any suffix (e.g. a script code) of each subfield 6 are
    preserved, as are indicators, the other subfields and the field order.
    """
    payload = []
    for field in group.fields:
        clone = _copy_field(field)
        if number is not None:
            for i, subfield in enumerate(clone.subfields):
                if subfield.code == "6":
                    link = parse_linkage(subfield.value)
                    clone.subfields[i] = Subfield(
                        "6", f"{link.tag}-{number}{link.suffix}"
                    )
        payload.append(clone)
    return tuple(payload)


def _take_number(occupied: set) -> str:
    """Take the lowest free occurrence number, or raise LinkageNumbersExhausted."""
    for i in range(1, 100):
        number = f"{i:02d}"
        if number not in occupied:
            occupied.add(number)
            return number
    raise LinkageNumbersExhausted


def _resolve(index: LinkageIndex, selector: Selector, label: str) -> FieldGroup:
    """Resolve a selector against `index` into a current FieldGroup."""
    if isinstance(selector, FieldGroup):
        unit = index.unit_for_field(selector.anchor)
        if unit is None:
            raise ValueError(f"group does not belong to the {label} record: {selector}")
        return unit
    if isinstance(selector, Field):
        unit = index.unit_for_field(selector)
        if unit is None:
            raise ValueError(
                f"field is not selectable in the {label} record: {selector}"
            )
        return unit
    if isinstance(selector, str):
        unit = index.group_for_number(selector)
        if unit is None:
            raise ValueError(
                f"no group with occurrence number {selector} in the {label} record"
            )
        return unit
    raise TypeError(f"cannot use {selector!r} to select a field group")


def _describe(group: FieldGroup, label: str) -> str:
    if group.linked:
        return f"{label} group {group.number} ({group.anchor.tag})"
    return f"{label} unlinked {group.anchor.tag} field"


class _Step:
    """One planned operation with every fact apply() needs to verify it."""

    __slots__ = (
        "kind",
        "number",
        "payload",
        "source_facts",
        "source_number",
        "source_field",
        "source_desc",
        "target_facts",
        "target_number",
        "target_field",
        "target_desc",
    )

    def __init__(
        self,
        kind: str,
        number: Optional[str],
        payload: tuple,
        source_group: FieldGroup,
        target_group: Optional[FieldGroup],
        source_label: str,
        target_label: str,
    ) -> None:
        """Initialize a _Step."""
        self.kind = kind
        self.number = number
        self.payload = payload
        self.source_facts = _group_facts(source_group)
        self.source_number = source_group.number
        self.source_field = None if source_group.linked else source_group.anchor
        self.source_desc = _describe(source_group, source_label)
        self.target_facts = _group_facts(target_group) if target_group else None
        self.target_number = target_group.number if target_group else None
        self.target_field = (
            None
            if (target_group is None or target_group.linked)
            else target_group.anchor
        )
        self.target_desc = (
            _describe(target_group, target_label) if target_group else None
        )


class MigrationPlan:
    """A previewed, not yet applied migration of field groups between records.

    Created by :func:`preview_migration`, which also documents which facts
    the plan stores and why. `steps` holds one _Step per operation; `apply`
    re-validates the plan against the current records and only then mutates
    the target.
    """

    def __init__(
        self,
        source: "Record",
        target: "Record",
        steps: list,
        source_label: str,
        target_label: str,
        target_numbers: frozenset,
    ) -> None:
        """Initialize a MigrationPlan; use preview_migration()."""
        self.source = source
        self.target = target
        self.steps = steps
        self.source_label = source_label
        self.target_label = target_label
        self.target_numbers = target_numbers

    def __str__(self) -> str:
        lines = [f"Migration plan {self.source_label} -> {self.target_label}:"]
        for step in self.steps:
            if step.kind == "import":
                head = f"  import {step.source_desc}"
            else:
                head = f"  replace {step.target_desc} with {step.source_desc}"
            if step.number is not None:
                head += f" as {self.target_label} group {step.number}"
            lines.append(head)
            lines.extend(f"    {field}" for field in step.payload)
        return "\n".join(lines)

    def apply(self) -> None:
        """Verify the plan against the current records and mutate the target.

        Raises InvalidLinkage if either record's linkage no longer validates
        and MigrationConflict listing every planned fact that no longer
        holds; in both cases the target record is left untouched. On success
        fields the plan does not touch keep their values and relative order,
        and the source record is not modified.
        """
        src_index = LinkageIndex(self.source, self.source_label)
        tgt_index = LinkageIndex(self.target, self.target_label)
        # numbers the plan itself takes over or frees through replacements
        plan_target_numbers = {
            s.target_number for s in self.steps if s.target_number is not None
        }
        conflicts: list[str] = []
        replacements = []
        for step in self.steps:
            self._check_source(step, src_index, conflicts)
            if step.kind == "replace":
                unit = self._check_target(step, tgt_index, conflicts)
                if unit is not None:
                    replacements.append((step, unit))
            if step.number is not None and step.number != step.target_number:
                owner = tgt_index.group_for_number(step.number)
                if owner is not None and step.number not in plan_target_numbers:
                    conflicts.append(
                        f"occurrence number {step.number} assigned at preview is now"
                        f" used by {owner.anchor.tag} in the {self.target_label} record"
                    )
        if conflicts:
            raise MigrationConflict(conflicts)
        insert_before: dict[int, tuple] = {}
        remove_ids: set[int] = set()
        for step, unit in replacements:
            insert_before[id(unit.fields[0])] = step.payload
            for field in unit.fields:
                remove_ids.add(id(field))
        new_fields: list = []
        for field in self.target.fields:
            payload = insert_before.get(id(field))
            if payload is not None:
                new_fields.extend(payload)
            if id(field) not in remove_ids:
                new_fields.append(field)
        for step in self.steps:
            if step.kind == "import":
                new_fields.extend(step.payload)
        # defensive: the result must itself validate before it lands
        LinkageIndex(new_fields, self.target_label)
        self.target.fields[:] = new_fields

    def _check_source(
        self, step: _Step, src_index: LinkageIndex, conflicts: list
    ) -> None:
        """Verify the source unit of `step` is still as previewed."""
        if step.source_number is not None:
            unit = src_index.group_for_number(step.source_number)
        elif step.source_field is not None:
            unit = src_index.unit_for_field(step.source_field)
        else:
            unit = None
        if unit is None:
            conflicts.append(
                f"{step.source_desc} no longer exists in the {self.source_label} record"
            )
        elif _group_facts(unit) != step.source_facts:
            conflicts.append(f"{step.source_desc} changed since the plan was previewed")

    def _check_target(self, step: _Step, tgt_index: LinkageIndex, conflicts: list):
        """Verify the replaced target unit of `step` is still as previewed."""
        if step.target_number is not None:
            unit = tgt_index.group_for_number(step.target_number)
        elif step.target_field is not None:
            unit = tgt_index.unit_for_field(step.target_field)
        else:
            unit = None
        if unit is None:
            conflicts.append(
                f"{step.target_desc} no longer exists in the {self.target_label} record"
            )
            return None
        if _group_facts(unit) != step.target_facts:
            conflicts.append(f"{step.target_desc} changed since the plan was previewed")
            return None
        return unit


def preview_migration(
    source: "Record",
    target: "Record",
    ops: "Sequence[Union[ImportGroup, ReplaceGroup]]",
    source_label: str = "source",
    target_label: str = "target",
) -> MigrationPlan:
    """Preview a migration of field groups from `source` into `target`.

    `ops` is a list of ImportGroup and ReplaceGroup operations, processed in
    order. Both records are scanned once; any linkage problem raises
    InvalidLinkage identifying the record and fields. If the operations need
    more occurrence numbers than the target has free, LinkageNumbersExhausted
    is raised and no plan is created, so a transaction that cannot be fully
    numbered is never applied in part. Neither record is modified.
    """
    if source is target:
        raise ValueError("source and target must be different records")
    src_index = LinkageIndex(source, source_label)
    tgt_index = LinkageIndex(target, target_label)
    occupied = set(tgt_index.groups)
    steps = []
    replaced_anchor_ids: set[int] = set()
    for op in ops:
        if isinstance(op, ImportGroup):
            src_unit = _resolve(src_index, op.source, source_label)
            number = _take_number(occupied) if src_unit.linked else None
            steps.append(
                _Step(
                    "import",
                    number,
                    _payload(src_unit, number),
                    src_unit,
                    None,
                    source_label,
                    target_label,
                )
            )
        elif isinstance(op, ReplaceGroup):
            tgt_unit = _resolve(tgt_index, op.target, target_label)
            if id(tgt_unit.anchor) in replaced_anchor_ids:
                raise ValueError(
                    f"target unit replaced more than once: {_describe(tgt_unit, target_label)}"
                )
            replaced_anchor_ids.add(id(tgt_unit.anchor))
            src_unit = _resolve(src_index, op.source, source_label)
            if src_unit.linked:
                number = tgt_unit.number if tgt_unit.linked else _take_number(occupied)
            else:
                if tgt_unit.number is not None:
                    occupied.discard(tgt_unit.number)
                number = None
            steps.append(
                _Step(
                    "replace",
                    number,
                    _payload(src_unit, number),
                    src_unit,
                    tgt_unit,
                    source_label,
                    target_label,
                )
            )
        else:
            raise TypeError(f"unknown migration operation {op!r}")
    return MigrationPlan(
        source, target, steps, source_label, target_label, frozenset(tgt_index.groups)
    )

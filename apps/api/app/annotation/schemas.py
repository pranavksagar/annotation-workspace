"""Pydantic request/response schemas. Mutation operations accept strictly listed fields only."""
from __future__ import annotations

from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

VersionString = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]
DecimalString = Annotated[str, Field(pattern=r"^[+-]?[0-9]+(\.[0-9]+)?$", max_length=40)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Point = Annotated[list[Finite], Field(min_length=2, max_length=2)]
Polygon = Annotated[list[Point], Field(min_length=4, max_length=4)]

RegionKind = Literal["TEXT", "SYMBOL", "IGNORE"]
Legibility = Literal["READABLE", "UNREADABLE"]
Disposition = Literal["UNCLASSIFIED", "NON_REQUIREMENT", "SUPPORTING_CONTEXT"]
RequirementType = Literal["DIMENSION", "GEOMETRIC_TOLERANCE", "SURFACE_FINISH", "THREAD", "MATERIAL",
                          "HEAT_TREATMENT", "COATING", "GEAR_SPLINE", "MANUFACTURING_NOTE", "OTHER"]
Role = Literal["PRIMARY", "NOMINAL", "UPPER_TOLERANCE", "LOWER_TOLERANCE", "UNIT", "DATUM", "SYMBOL", "CONTEXT"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StructuredValue(Strict):
    """Optional typed interpretation. Decimal strings, explicit units, null when unknown."""
    nominal: DecimalString | None = None
    upper_tolerance: DecimalString | None = None
    lower_tolerance: DecimalString | None = None
    unit: Annotated[str, Field(max_length=32)] | None = None
    note: Annotated[str, Field(max_length=500)] | None = None


class RegionCreate(Strict):
    id: UUID
    page_id: UUID
    kind: RegionKind
    polygon: Polygon
    transcription: Annotated[str, Field(max_length=4000)] = ""
    symbol_code: Annotated[str, Field(max_length=64)] | None = None
    legibility: Legibility = "READABLE"
    reading_angle_degrees: Finite = 0.0
    semantic_disposition: Disposition = "UNCLASSIFIED"


class RegionChanges(Strict):
    kind: RegionKind | None = None
    polygon: Polygon | None = None
    transcription: Annotated[str, Field(max_length=4000)] | None = None
    symbol_code: Annotated[str, Field(max_length=64)] | None = None
    legibility: Legibility | None = None
    reading_angle_degrees: Finite | None = None
    semantic_disposition: Disposition | None = None


class GroupCreate(Strict):
    id: UUID
    primary_page_id: UUID
    requirement_type: RequirementType
    description: Annotated[str, Field(max_length=2000)] | None = None
    structured_value: StructuredValue | None = None


class GroupChanges(Strict):
    primary_page_id: UUID | None = None
    requirement_type: RequirementType | None = None
    description: Annotated[str, Field(max_length=2000)] | None = None
    structured_value: StructuredValue | None = None


class MemberChanges(Strict):
    order_index: Annotated[int, Field(ge=0, le=100_000)]


class RegionCreateOp(Strict):
    type: Literal["region.create"]
    region: RegionCreate


class RegionUpdateOp(Strict):
    type: Literal["region.update"]
    region_id: UUID
    changes: RegionChanges


class RegionDeleteOp(Strict):
    type: Literal["region.delete"]
    region_id: UUID


class GroupCreateOp(Strict):
    type: Literal["group.create"]
    group: GroupCreate


class GroupUpdateOp(Strict):
    type: Literal["group.update"]
    group_id: UUID
    changes: GroupChanges


class GroupDeleteOp(Strict):
    type: Literal["group.delete"]
    group_id: UUID


class MemberAddOp(Strict):
    type: Literal["member.add"]
    group_id: UUID
    region_id: UUID
    role: Role
    order_index: Annotated[int, Field(ge=0, le=100_000)] = 0


class MemberUpdateOp(Strict):
    type: Literal["member.update"]
    group_id: UUID
    region_id: UUID
    role: Role
    changes: MemberChanges


class MemberRemoveOp(Strict):
    type: Literal["member.remove"]
    group_id: UUID
    region_id: UUID
    role: Role


Operation = Annotated[Union[RegionCreateOp, RegionUpdateOp, RegionDeleteOp, GroupCreateOp, GroupUpdateOp,
                            GroupDeleteOp, MemberAddOp, MemberUpdateOp, MemberRemoveOp], Field(discriminator="type")]


class OcrRequest(Strict):
    page_id: UUID
    polygon: Polygon


class LeaseCredentials(Strict):
    lease_token: Annotated[str, Field(min_length=16, max_length=200)]
    lease_generation: VersionString


class MutationEnvelope(Strict):
    mutation_id: UUID
    expected_annotation_version: VersionString
    browser_id: UUID
    tab_id: UUID
    lease_token: Annotated[str, Field(min_length=16, max_length=200)]
    lease_generation: VersionString
    operations: Annotated[list[Operation], Field(min_length=1)]


class CoverageEntry(Strict):
    page_id: UUID
    status: Literal["ANNOTATED", "NO_LABELABLE_CONTENT"]


class CompleteRequest(Strict):
    mutation_id: UUID
    expected_annotation_version: VersionString
    browser_id: UUID
    tab_id: UUID
    lease_token: Annotated[str, Field(min_length=16, max_length=200)]
    lease_generation: VersionString
    labeling_policy_version: str
    coverage: list[CoverageEntry]


class ReopenRequest(Strict):
    mutation_id: UUID
    expected_annotation_version: VersionString
    browser_id: UUID
    tab_id: UUID


class EditSessionRequest(Strict):
    browser_id: UUID
    tab_id: UUID


class BrowserProgressIn(Strict):
    browser_id: UUID
    page_id: UUID
    center_x: Finite
    center_y: Finite
    zoom_relative_to_fit: Annotated[float, Field(gt=0, le=1000, allow_inf_nan=False)]
    selected_region_id: UUID | None = None
    selected_group_id: UUID | None = None

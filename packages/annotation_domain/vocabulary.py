"""Versioned annotation vocabulary shared by the API, the editor and training readers."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

VOCABULARY_PATH = Path(__file__).with_name("vocabulary.json")

REGION_KINDS = ("TEXT", "SYMBOL", "IGNORE")
LEGIBILITY = ("READABLE", "UNREADABLE")
SEMANTIC_DISPOSITIONS = ("UNCLASSIFIED", "NON_REQUIREMENT", "SUPPORTING_CONTEXT")
REGION_ORIGINS = ("MANUAL", "OCR_ACCEPTED", "LEGACY_IMPORT")
ANNOTATION_STATUSES = ("TO_DO", "IN_PROGRESS", "COMPLETED")
PREPARATION_STATES = ("QUEUED", "RUNNING", "READY", "FAILED")
OCR_STATES = ("NOT_REQUESTED", "QUEUED", "RUNNING", "READY", "FAILED")
COVERAGE_STATUSES = ("ANNOTATED", "NO_LABELABLE_CONTENT")


@lru_cache(maxsize=1)
def vocabulary() -> dict:
    return json.loads(VOCABULARY_PATH.read_text(encoding="utf-8"))


def requirement_types() -> tuple[str, ...]:
    return tuple(vocabulary()["requirement_types"])


def member_roles() -> tuple[str, ...]:
    return tuple(vocabulary()["member_roles"])


def symbol_codes() -> dict[str, dict]:
    return {item["code"]: item for item in vocabulary()["symbol_codes"]}


LABELING_POLICY_VERSION = vocabulary()["labeling_policy_version"]
SNAPSHOT_SCHEMA_VERSION = vocabulary()["snapshot_schema_version"]
VOCABULARY_VERSION = vocabulary()["vocabulary_version"]

"""Transport bounds, format regexes, and supported version sets for scalar dependency evidence."""

from __future__ import annotations

import re


MAX_SOURCE_LENGTH = 1_000_000
MAX_TOKENS = 512
MAX_EDGES = 1_024
MAX_MARKERS = 128
MAX_CUES = 64
MAX_CHECK_NAMES = 64
MAX_VERSION_LENGTH = 64
MAX_PARSER_ID_LENGTH = 64
MAX_LABEL_LENGTH = 64
MAX_POS_TAG_LENGTH = 32
MAX_CATEGORY_LENGTH = 64
MAX_OUTCOME_LENGTH = 32
MAX_REASON_LENGTH = 256
MAX_RULE_VERSION_LENGTH = 64
MAX_COMPOSER_VERSION_LENGTH = 64
MAX_CHECK_NAME_LENGTH = 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_TOKEN_RE = re.compile(r"^[a-z0-9._-]{1,64}$")
SUPPORTED_SCHEMA_VERSIONS = frozenset({"scalar-dependency-v1"})
SUPPORTED_EVIDENCE_VERSIONS = frozenset({"parser-evidence-v1"})

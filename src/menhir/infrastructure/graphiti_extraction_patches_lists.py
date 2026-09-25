"""Titled-list recognition data: membership relation, line regexes, and the verb/pronoun guard.

Pure data for ``parse_titled_list`` (which stays on ``graphiti_extraction_patches``, pinned there
by the CF-193 source drift guard). Extracted verbatim; that module re-exports everything here.
"""

from __future__ import annotations

import re

#: Relation emitted for a titled list. The list SYNTAX states membership -- "agents names below:"
#: followed by seven names asserts those are the agents -- so parsing it is reading the turn, not
#: inferring from it. Kept as one explicit relation rather than a guessed verb.
_MEMBERSHIP_RELATION = "MEMBER_OF"

#: A title line is `<title>:` optionally followed by the FIRST item on the same line. Real turns are
#: typed without care: the roster that motivated this is written `agents names below:Admon\nMagdy...`
#: with no newline after the colon, so requiring the colon to END the line refused the very case this
#: exists for. The colon itself is the load-bearing marker -- an explicit author-written "a list
#: follows" -- and the per-item guards below are what keep prose out, not the line break.
_LIST_TITLE_RE = re.compile(r"^(?P<title>[^:]{2,60}?)\s*:\s*(?P<first>.*)$")

#: Leading bullet/number decoration stripped from an item before it becomes an entity name.
_LIST_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")

#: Verbs that disqualify an item (and therefore the whole block) under the "items are NAMES, not
#: clauses" rule. A closed, conservative allowlist matched at word boundaries; includes the
#: inflections observed on the CF-193 probes (`buy`, `walk`, `call`, `fixed`, `shipped`, `ate`).
_LIST_VERBS: tuple[str, ...] = (
    "buy", "bought", "buying", "purchase", "purchased", "purchasing",
    "walk", "walked", "walking",
    "call", "called", "calling",
    "fix", "fixed", "fixing",
    "ship", "shipped", "shipping",
    "eat", "ate", "eating",
    "get", "got", "getting",
    "make", "made", "making",
    "go", "went", "going",
    "run", "ran", "running",
    "do", "did", "doing",
    "take", "took", "taking",
    "see", "saw", "seen", "seeing",
    "say", "said", "saying",
    "have", "had", "having",
    "finish", "finished", "finishing",
    "complete", "completed", "completing",
    "work", "worked", "working",
    "read", "reading",
    "write", "wrote", "written", "writing",
)

#: Personal pronouns. A NAME does not begin with one; a clause does ("we are working today",
#: "i bought milk"). Same leading-token rule as the verbs, so this stays one concept.
_LIST_CLAUSE_PRONOUNS: tuple[str, ...] = (
    "i", "we", "you", "he", "she", "they", "it", "my", "our", "your", "their",
)

#: An item that BEGINS with a verb or a pronoun and continues is a clause, not a name.
#: Anchored at the start on purpose -- see the note above `_LIST_ITEM_MAX_WORDS` on the facade.
_LIST_VERB_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(w) for w in (*_LIST_VERBS, *_LIST_CLAUSE_PRONOUNS))
    + r")\s+\S",
    re.IGNORECASE,
)

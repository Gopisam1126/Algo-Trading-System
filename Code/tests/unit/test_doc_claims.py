"""The documents say what the tree is (audit W8, 10 Nov 2026).

The audit found the entry documents describing a build months out of date
(ENG-009, ENG-038, ENG-049), seven contradictory hand-written test counts
(ENG-037), the Algo-ID question closed while the exchanges' standard says
otherwise (LEG-001) and Indian hosting cited as a SEBI rule no circular contains
(HAL-001). Each was corrected in prose; these checks keep the corrections from
drifting back, which is how every one of them happened.

Two scopes, one rule set. QA promotion strips the design documents, and nothing
that runs on QA may read a stripped path (tests/security/test_promotion_hygiene.py),
so this suite reads the files every branch carries - both READMEs, Code/CLAUDE.md
and Code/SETUP.md. The design documents, PRE_LIVE_CHECKLIST among them, are checked
by the same tests run from the DEV-side pre-commit hook ``scripts/hooks/doc_claims.py``,
which names them in ``DOC_CLAIMS_EXTRA`` (paths relative to the repository root,
separated by ``os.pathsep``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[2]
ROOT = CODE.parent
PACKAGES = CODE / "src" / "algotrader"

#: The documents every branch carries.
CODE_SIDE = [ROOT / "README.md", CODE / "README.md", CODE / "CLAUDE.md", CODE / "SETUP.md"]
EXTRA = [ROOT / p for p in os.environ.get("DOC_CLAIMS_EXTRA", "").split(os.pathsep) if p]
DOCS = CODE_SIDE + EXTRA
IDS = [str(p.relative_to(ROOT)) for p in DOCS]


def paragraphs(doc: Path) -> list[str]:
    """Blank-line separated prose blocks; a table row is a block of its own.
    Fenced code (commands, diagrams, screen mock-ups) is not prose and is dropped."""
    prose = re.sub(
        r"^[ \t]*```.*?^[ \t]*```", "", doc.read_text(encoding="utf-8"), flags=re.M | re.S
    )
    blocks: list[str] = []
    for block in re.split(r"\n\s*\n", prose):
        rows = [r for r in block.splitlines() if r.lstrip().startswith("|")]
        blocks.extend(rows if rows else [block])
    return blocks


def offending(doc: Path, claim: str, unless: str = r"(?!x)x") -> list[str]:
    """Blocks that make ``claim`` and do not also say ``unless``."""
    return [
        " ".join(b.split())[:160]
        for b in paragraphs(doc)
        if re.search(claim, b, re.I | re.S) and not re.search(unless, b, re.I | re.S)
    ]


def test_every_document_named_exists() -> None:
    """Control: a misspelt or moved document would otherwise be checked by nothing
    (and a missing code-side one means this suite is checking less than it says)."""
    missing = [str(p) for p in DOCS if not p.is_file()]
    assert not missing, f"named but absent: {missing}"


#: A count of tests or a coverage figure written as prose (ENG-037). "100%
#: coverage" as a TARGET is a requirement, not a measurement, and is allowed.
TEST_COUNT = (
    r"\b\d{1,3}(?:,\d{3})+ tests\b|\b\d{3,} tests\b|\btests passing\b"
    r"|\b\d{3,} tests pass\b|\b\d{2}(?:\.\d+)?% coverage\b"
)


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_hand_written_test_count(doc: Path) -> None:
    """The latest CI run is the count; a number in prose is true for one commit."""
    assert not offending(doc, TEST_COUNT), "drop the count; cite the CI run instead"


#: Statements the tree has falsified: an order path that "does not exist", a
#: design document "not yet implemented" over 10k lines of execution code.
DENIES_BUILT_CODE = (
    r"no order placement|pre-implementation|no code (has been )?written"
    r"|order management does not|nothing attaches a protective stop"
)


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_document_denies_code_that_exists(doc: Path) -> None:
    assert not offending(doc, DENIES_BUILT_CODE)


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_document_closes_the_algo_id_question(doc: Path) -> None:
    """B1 was closed as "not applicable" on 9 Sep 2026 and reopened by LEG-001;
    live mode refuses to load without ``broker.algo_tagging_confirmation``. A
    block calling it closed must say it was reopened."""
    closed = r"\b(closed|resolved|not applicable|N/A|withdrawn)\b"
    # A table row is one claim however wide: PRE_LIVE_CHECKLIST's B1 row puts its
    # status cell 130 characters from "B1". Prose: within a sentence's reach.
    claim = rf"^\s*\|[^\n]*\b(B1|Algo-ID)\b[^\n]*{closed}|\b(B1|Algo-ID)\b[\s\S]{{0,80}}?{closed}"
    assert not offending(doc, claim, unless=r"reopen")


@pytest.mark.parametrize("doc", DOCS, ids=IDS)
def test_no_document_cites_a_rule_for_indian_hosting(doc: Path) -> None:
    """No circular read requires Indian servers (HAL-001): the India-region check
    is this system's own rule. A block that ties India hosting to SEBI or calls
    it mandatory must say so."""
    india = r"(india[- ]hosted|indian servers|hosted (in|on) india|india region)"
    rule = r"\b(SEBI|mandatory|requires?|required|regulat\w*|constraints? follow)\b"
    # 300: "SEBI's framework is mandatory ... [a sentence of the lane] ... hosted
    # in India" is one claim across a long sentence (the root README's, until
    # 21 Nov 2026).
    claim = rf"{india}[\s\S]{{0,300}}?{rule}|{rule}[\s\S]{{0,300}}?{india}"
    assert not offending(doc, claim, unless=r"own rule|self-imposed|HAL-001|B10")


def empty_packages() -> set[str]:
    """Packages with no module but a one-line ``__init__.py``. Only Python counts:
    git keeps no empty directory, so ``api/static/`` exists on one machine only."""
    return {
        p.name
        for p in PACKAGES.iterdir()
        if (p / "__init__.py").exists()
        and not [f for f in p.rglob("*.py") if f.name != "__init__.py"]
        and len((p / "__init__.py").read_text(encoding="utf-8").strip().splitlines()) <= 1
    }


#: A status table's row of empty packages: "| `ai/` `macro/` ... | **Empty** |"
#: (MASTER_REFERENCE §2.2, the one authoritative status).
EMPTY_ROW = re.compile(r"^\|((?: `\w+/`)+) \| \*\*Empty\*\* \|", re.M)


def named_empty(doc: Path) -> set[str]:
    text = doc.read_text(encoding="utf-8")
    if doc == CODE / "README.md":  # its layout block marks each package
        return set(re.findall(r"^  (\w+)/ .*\[empty - planned\]\s*$", text, re.M))
    row = EMPTY_ROW.search(text)
    assert row
    return set(re.findall(r"`(\w+)/`", row.group(1)))


STATUS_DOCS = [CODE / "README.md"] + [d for d in EXTRA if EMPTY_ROW.search(d.read_text("utf-8"))]


@pytest.mark.parametrize("doc", STATUS_DOCS, ids=[str(p.relative_to(ROOT)) for p in STATUS_DOCS])
def test_the_empty_packages_named_are_the_empty_packages(doc: Path) -> None:
    """ENG-009/ENG-038: the package states are checked against the tree, both ways
    - a built package called empty, and an empty one presented as built."""
    actual = empty_packages()
    assert actual, "control: five packages are still empty (E23-S03, E10, ...)"
    assert named_empty(doc) == actual

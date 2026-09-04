"""One definition of "text that is safe to put in a log line or an audit row".

**This is the fourth time the same defect has been found**, and the reason the
rule lives in `common/` rather than beside any one of its callers:

* QA-SEC-16 — a newline in ``OrderRequest.symbol`` forged a log line.
* QA-SEC-28 — the same untrusted symbol reaching ``Trigger`` and
  ``Recommendation``, which the risk engine logs on every rejection.
* QA-SEC-30 — surveillance-restriction labels, from NSE data by way of E04.
  That fix moved the escaping to where details are *constructed*
  (``CheckOutcome``) rather than patching a third source.
* QA-SEC-38 — found in E14-S07: ``RiskDecision`` is a **second door** into the
  same ``detail`` field, and the sizer writes through it carrying a
  ``binding_constraint`` that ``CheckOutcome`` never sees.

Moving it to `common/` is the same lesson one level up. A rule that lives on
one of two constructors is a rule with a hole in it, and the hole is invisible
because the other constructor looks equally reasonable.
"""

from __future__ import annotations

#: A detail is one line, and no longer than this.
#:
#: Enforced where the value is BUILT rather than at the log call, because the
#: log is not the only consumer — the same text reaches ``decision_log.payload``
#: — and because a rule applied at one call site has to be remembered at the
#: next one.
MAX_DETAIL = 512

#: Control characters that must never survive into a detail. C0 plus DEL.
_CONTROL = {c: f"\\x{c:02x}" for c in [*range(0x20), 0x7F]}
_CONTROL.update({0x0A: "\\n", 0x0D: "\\r", 0x09: "\\t"})
_CONTROL_TABLE = str.maketrans({chr(k): v for k, v in _CONTROL.items()})


def one_safe_line(detail: str) -> str:
    """Escape control characters and bound the length.

    Escaping rather than rejecting: a detail is diagnostic text, not a security
    decision. Refusing to build the object would turn an accurate rejection
    reason into a generic fault and lose what an operator needs, which is a
    worse outcome than a visible ``\\n``.

    The escape is visible on purpose — an investigator should be able to see
    that a newline was attempted, not merely that the text looks odd.
    """
    escaped = detail.translate(_CONTROL_TABLE)
    if len(escaped) > MAX_DETAIL:
        keep = MAX_DETAIL - 24
        escaped = f"{escaped[:keep]}... [{len(escaped) - keep} more chars]"
    return escaped

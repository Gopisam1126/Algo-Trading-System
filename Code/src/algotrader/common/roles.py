"""The database roles the code relies on, named once.

Neutral ground: the persistence layer grants to these roles and the strategy
registry checks who wrote an approval, and neither should import the other.
The migrations freeze their own copies (a migration is a snapshot); the
integration tests hold the two together.
"""

from __future__ import annotations

from typing import Final

#: The application's role: every trading service connects as it.
APP_ROLE: Final = "algotrader_app"

#: The role that writes strategy approvals - and that the application lacks.
#: An approval written by anything else does not count (audit RSK-007).
APPROVER_ROLE: Final = "algotrader_approver"

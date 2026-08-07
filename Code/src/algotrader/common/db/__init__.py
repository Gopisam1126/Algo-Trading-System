"""Database access layer.

Scope boundary, enforced by an acceptance test in E01-S02: **SQLAlchemy is
imported here and nowhere else.**  Services talk to repository protocols, not
to the ORM, so that swapping the persistence layer or faking it in a unit test
does not require touching business logic.

Contents as of Sprint 1:

- ``base``  — the declarative base and naming convention (this scaffold)
- ``models``       — table definitions              (E01-S01 task 2)
- ``repositories`` — protocols and implementations  (E01-S02)
"""

from algotrader.common.db.base import Base, metadata

__all__ = ["Base", "metadata"]

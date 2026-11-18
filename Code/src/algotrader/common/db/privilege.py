"""A service's database login must not be able to rewrite the record (audit SEC-002).

**The decision.** Every service connects as ``algotrader_svc``: a LOGIN role
that is a member of ``algotrader_app`` and owns nothing, created on first
database init (``ops/postgres/initdb/10-app-login.sh``). Before a service
uses a connection, :func:`require_least_privilege` asks the database what that
login may do, and refuses it if it may do anything the append-only rules
forbid. :func:`~algotrader.common.db.engine.open_service_engine` is the
constructor services use, and it runs this before returning.

**Why it is checked, not assumed.** The grants that made ``decision_log`` and
``strategy_trial`` append-only (BR-3, BR-4) and a strategy's document
immutable were written to ``algotrader_app`` - and the services connected as
the bootstrap user that OWNS the tables. An owner holds every privilege on
what it owns, so every one of those grants was in force for nobody; the hash
chain still caught an edit in place, but deleting the newest rows left a
valid chain (audit SEC-002, 10 Nov 2026). A grant that applies to a role
nothing connects as is a comment.

**The alternative rejected:** ``SET ROLE algotrader_app`` after connecting as
the owner. It works until any code runs ``RESET ROLE`` - which the session
then permits, because the session still IS the owner. The login itself must
lack the rights.

**What would make this wrong:** a table added to the append-only set (or a
column to the immutable document) without a line here; the check lists what
it knows. ``tests/integration/test_service_login.py`` holds the list to
``models.APPEND_ONLY_TABLES``.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection


class LeastPrivilegeError(RuntimeError):
    """The connection may do something a service's login must not."""


#: Each column must be FALSE for a service's login. A fixed statement: no
#: part of it comes from data, so there is nothing to inject.
_CHECK: Final = text(
    """
    SELECT
        current_user AS login,
        r.rolsuper AS superuser,
        r.rolcreaterole AS create_role,
        r.rolbypassrls AS bypass_rls,
        EXISTS (
            SELECT 1 FROM pg_tables t
            WHERE t.schemaname = 'public' AND t.tableowner = current_user
        ) AS owns_tables,
        has_table_privilege('decision_log', 'UPDATE') AS update_decision_log,
        has_table_privilege('decision_log', 'DELETE') AS delete_decision_log,
        has_table_privilege('decision_log', 'TRUNCATE') AS truncate_decision_log,
        has_table_privilege('strategy_trial', 'UPDATE') AS update_strategy_trial,
        has_table_privilege('strategy_trial', 'DELETE') AS delete_strategy_trial,
        has_table_privilege('strategy_trial', 'TRUNCATE') AS truncate_strategy_trial,
        has_column_privilege('strategy', 'dsl', 'UPDATE') AS update_strategy_dsl,
        has_column_privilege('strategy', 'dsl_hash', 'UPDATE') AS update_strategy_hash,
        has_table_privilege('strategy', 'DELETE') AS delete_strategy,
        has_table_privilege('strategy_approval', 'INSERT') AS write_approval
    FROM pg_roles r
    WHERE r.rolname = current_user
    """
)

#: What each column means, in the refusal.
FORBIDDEN: Final[dict[str, str]] = {
    "superuser": "is a superuser",
    "create_role": "can create roles",
    "bypass_rls": "bypasses row-level security",
    "owns_tables": "owns tables in the schema",
    "update_decision_log": "can UPDATE decision_log",
    "delete_decision_log": "can DELETE from decision_log",
    "truncate_decision_log": "can TRUNCATE decision_log",
    "update_strategy_trial": "can UPDATE strategy_trial",
    "delete_strategy_trial": "can DELETE from strategy_trial",
    "truncate_strategy_trial": "can TRUNCATE strategy_trial",
    "update_strategy_dsl": "can rewrite a strategy's DSL",
    "update_strategy_hash": "can rewrite a strategy's dsl_hash",
    "delete_strategy": "can DELETE a strategy",
    "write_approval": "can write a strategy approval",
}


async def require_least_privilege(conn: AsyncConnection) -> None:
    """Refuse a connection whose login may rewrite the record. Fail closed.

    A database that cannot answer - the schema not migrated, a role that
    cannot read the catalogue - is a refusal too: "could not check" is not
    "checked".
    """
    try:
        row = (await conn.execute(_CHECK)).mappings().one()
    except DBAPIError as exc:
        raise LeastPrivilegeError(
            f"could not verify the services' database login ({type(exc.orig).__name__}); "
            f"refusing to run unchecked"
        ) from None
    held = [meaning for column, meaning in FORBIDDEN.items() if row[column]]
    if held:
        raise LeastPrivilegeError(
            f"the services' database login {row['login']!r} {'; '.join(held)}. Services "
            f"connect as algotrader_svc, a member of algotrader_app that owns nothing - "
            f"never as the owner that runs the migrations (audit SEC-002)."
        )

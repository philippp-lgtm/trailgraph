"""The ``os`` identity provider: the verified principal (contracts B2.2, B2.3).

Together with the admin wiring, this is the only module that reads the
process environment, the uid, the user name and the pid (A test).
"""

from __future__ import annotations

import os
import pwd

from graph_cli.catalog.identity.facets import IdentityInputs
from graph_cli.kernel.actor import Assurance, Facet


def system_inputs() -> IdentityInputs:
    uid = os.getuid()
    try:
        user_name: str | None = pwd.getpwuid(uid).pw_name
    except KeyError:
        user_name = None
    return IdentityInputs(env=dict(os.environ), uid=uid, user_name=user_name, pid=os.getpid())


def principal(inputs: IdentityInputs) -> Facet:
    return Facet(
        Assurance.VERIFIED, id=f"os:{inputs.uid}", name=inputs.user_name or None, source="os"
    )

"""Identity providers and the one ``ActorContext`` per invocation (contracts B2).

``resolve_actor`` is pure; ``os.system_inputs`` is the only reader of the
process. Asserted facets follow the provider precedence ``generic_env``,
then ``claude_code`` (B2.6). A facet comes whole from the first provider
that supplies it; a malformed value makes it absent with warning
``actor.facet_malformed`` and does not fall back to the next provider
(B2.4). The warning names the variable, never the value. The entrypoint is
a constant of the binary (B2.7).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Final

from graph_cli.catalog.identity import claude_code, generic_env
from graph_cli.catalog.identity import os as os_provider
from graph_cli.catalog.identity.facets import IdentityInputs, Supplied
from graph_cli.kernel.actor import (
    ABSENT,
    ActorContext,
    Assurance,
    ClientInfo,
    Facet,
    is_valid_asserted_value,
)
from graph_cli.kernel.errors import GraphCliError, WarningRecord

Provider = Callable[[Mapping[str, str]], dict[str, Supplied | None]]

# Precedence order (B2.6). The os provider is always on and not listed.
PROVIDERS: Final[Mapping[str, Provider]] = {
    generic_env.NAME: generic_env.facets,
    claude_code.NAME: claude_code.facets,
}
DEFAULT_PROVIDERS: Final = tuple(PROVIDERS)

system_inputs = os_provider.system_inputs


def resolve_actor(
    inputs: IdentityInputs,
    *,
    client: ClientInfo,
    device_id: str | None = None,
    project: str | None = None,
    providers: Sequence[str] = DEFAULT_PROVIDERS,
) -> tuple[ActorContext, tuple[WarningRecord, ...]]:
    """Build the invocation's actor. ``device_id`` comes from ``config.toml``, ``project``
    from the pointer file, ``providers`` from config ``[identity] providers``."""
    unknown = sorted(set(providers) - set(PROVIDERS))
    if unknown:
        raise GraphCliError("config.invalid", f"unknown identity providers: {', '.join(unknown)}")
    warnings: list[WarningRecord] = []
    answers = [PROVIDERS[name](inputs.env) for name in PROVIDERS if name in providers]
    asserted: dict[str, Facet] = {}
    for name in ("agent", "session"):
        supplied = next((a[name] for a in answers if a[name] is not None), None)
        asserted[name] = _facet(name, supplied, warnings)
    return (
        ActorContext(
            principal=os_provider.principal(inputs),
            agent=asserted["agent"],
            session=asserted["session"],
            device=_configured("device", device_id, "config", warnings),
            project=_configured("project", project, "pointer", warnings),
            client=client,
            pid=inputs.pid,
        ),
        tuple(warnings),
    )


def _facet(name: str, supplied: Supplied | None, warnings: list[WarningRecord]) -> Facet:
    if supplied is None:
        return ABSENT
    if supplied.facet is None:
        warnings.append(_malformed(name, supplied.malformed))
        return ABSENT
    return supplied.facet


def _configured(name: str, value: str | None, source: str, warnings: list[WarningRecord]) -> Facet:
    if not value:
        return ABSENT
    if not is_valid_asserted_value(value):
        warnings.append(_malformed(name, source))
        return ABSENT
    return Facet(Assurance.ASSERTED, id=value, source=source)


def _malformed(facet: str, origin: str | None) -> WarningRecord:
    return WarningRecord(
        "actor.facet_malformed",
        f"ignored a malformed {facet} identity value from {origin}",
        {"facet": facet, "source": origin},
    )


__all__ = ["DEFAULT_PROVIDERS", "IdentityInputs", "resolve_actor", "system_inputs"]

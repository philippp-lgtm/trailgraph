"""Shared pieces of the read limit tests: installing a policy, and comparing what a caller sees
of a hidden entity, type or relation with what it sees of one that does not exist (type read
limits plan V5)."""

import hashlib
import json
from pathlib import Path
from typing import Any

from harness import CliRunner

from support.admin import Outcome, admin
from support.policies import write_policy

NOT_THERE = "0192f3c0-11aa-7e01-b2c3-d4e5f6a7b8c9"


def install(run_cli: CliRunner, work: Path, text: str) -> None:
    """Apply ``text`` as the installation policy."""
    name = f"policy_{hashlib.sha256(text.encode()).hexdigest()[:12]}.yaml"
    path = write_policy(work, name, text)
    applied = admin(run_cli, "policy", "apply", str(path), "--reason", "test policy")
    assert applied.code == 0, applied.envelope


def shown(outcome: Outcome) -> dict[str, Any]:
    """What a caller sees of an answer: the exit code and its data or its error."""
    return {"code": outcome.code, "data": outcome.data, "error": outcome.envelope.get("error")}


def without_suggestions(answer: dict[str, Any]) -> dict[str, Any]:
    """``answer`` without ``did_you_mean`` (it depends on the spelling of the name asked, so two
    different names get different ones); the suggestions are checked on their own."""
    text = json.loads(json.dumps(answer))
    text["error"]["details"].pop("did_you_mean", None)
    return text  # type: ignore[no-any-return]


def the_same_but_for(
    hidden: dict[str, Any], missing: dict[str, Any], *swaps: tuple[str, str]
) -> None:
    """``hidden`` equals ``missing`` once the text of the first is rewritten to the second."""
    text = json.dumps(hidden)
    for old, new in swaps:
        text = text.replace(old, new)
    assert json.loads(text) == missing, json.dumps([json.loads(text), missing], indent=1)

"""The parts of ``graph call`` that need no graph (tool manifest plan TM7): which catalog a tool
name asks for, what is read to find it, the kind of a call from its arguments, and the digest of
its input."""

from typing import Any

from graph_cli.app_cli.commands import call as call_module
from graph_cli.kernel.canonical import hash_value
from graph_cli.kernel.changeset import load_document


class Reader:
    """Records which catalogs a call reads, and shows what it sees of each."""

    def __init__(self) -> None:
        self.read: list[str] = []

    def classified_functions(self, actor: Any, entry: Any) -> tuple[Any, ...]:
        self.read.append("functions")
        return ()

    def classified_views(self, actor: Any, entry: Any) -> tuple[Any, ...]:
        self.read.append("views")
        return ()

    def published_actions(self, entry: Any) -> None:
        self.read.append("actions")
        return None

    def may(self, actor: Any, entry: Any, action: str) -> bool:
        self.read.append(action)
        return False  # no lookup tool, so no model to read


def test_the_prefix_of_a_name_says_which_catalog_it_asks_for() -> None:
    assert call_module._kind_of("run_items") == "function"
    assert call_module._kind_of("do_move") == "action"
    assert call_module._kind_of("view_card") == "view"
    for name in ("items", "run", "Run_items", "lookup", "undo_it", ""):
        assert call_module._kind_of(name) is None, name


def test_a_call_reads_only_the_catalog_of_its_tool() -> None:
    for kind, catalog in (("function", "functions"), ("action", "actions"), ("view", "views")):
        reader = Reader()
        call_module._classified(reader, None, None, kind)  # type: ignore[arg-type]
        assert reader.read == [catalog], kind
    reader = Reader()
    call_module._usable_names(reader, None, None)  # type: ignore[arg-type]
    # all of them, on a miss only, and whether the lookup tool is one of the names
    assert sorted(reader.read) == ["actions", "functions", "read.find", "views"]


def test_a_call_is_a_write_for_an_action_that_is_not_a_dry_run() -> None:
    kind = call_module.call_kind
    assert kind({"tool": "do_x"}) == "write"
    assert kind({"tool": "do_x", "--input": ["_dry_run=false"]}) == "write"
    assert kind({"tool": "do_x", "--input": ["a=1", "_dry_run=true"]}) == "read"
    assert kind({"tool": "do_x", "--input": {"_dry_run": True}}) == "read"
    assert kind({"tool": "do_x", "--input": ["_dry_run=nope"]}) == "write"
    for tool in ("run_x", "view_x", "other"):
        assert kind({"tool": tool}) == "read"
    assert kind({}) == "read"


def test_a_number_with_a_fraction_and_its_text_have_two_digests() -> None:
    number, text = load_document("5.0"), load_document('"5.0"')
    digest = call_module._digestible
    assert digest({"n": number}) == {"n": {"number": "5.0"}}
    assert digest({"n": text}) == {"n": "5.0"}
    assert hash_value(digest({"n": number})) != hash_value(digest({"n": text}))
    assert digest({"n": [number, 2, None, "x"]}) == {"n": [{"number": "5.0"}, 2, None, "x"]}

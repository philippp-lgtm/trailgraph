"""Language-tagged values through the write service on both backends (Q18 stage 3).

An untagged value takes the graph language and is stored with it; without a
graph language it is refused. Tags get canonical case; ``remove`` of an
untagged text drops it in every language.
"""

from pathlib import Path
from typing import Any

import pytest
from support.services import ServiceEnv

from graph_cli.kernel.canonical import JsonValue
from graph_cli.kernel.ids import parse_id

GLOSSARY = "nas:GlossaryEntry"


def de(text: str) -> dict[str, JsonValue]:
    return {"lang": "de", "value": text}


def en(text: str) -> dict[str, JsonValue]:
    return {"lang": "en", "value": text}


def svc_for(backend: str, root: Path, language: str | None) -> ServiceEnv:
    return ServiceEnv(backend, "notes_and_sources", root, version="1.2.0", language=language)


@pytest.fixture
def svc(backend: str, tmp_path: Path) -> ServiceEnv:
    return svc_for(backend, tmp_path / "root", "de")


def create(svc: ServiceEnv, **parts: Any) -> dict[str, JsonValue]:
    result = svc.apply({"op": "create", "type": GLOSSARY, "local": "g", **parts})
    (entity,) = result.entities
    return attrs_of(svc, str(entity.id))


def attrs_of(svc: ServiceEnv, entity: str) -> dict[str, JsonValue]:
    current = svc.snapshot().get(parse_id(entity))
    assert current is not None
    return dict(current.attrs)


def test_untagged_values_take_the_graph_language(svc: ServiceEnv) -> None:
    attrs = create(
        svc,
        attrs={"heading": ["Steuerung und Kontrolle", en("Control and Monitoring")]},
        attrs_text={"variant_names": "SuK", "explanation": '"A subject."@en-GB'},
    )
    assert attrs == {
        "heading": [de("Steuerung und Kontrolle"), en("Control and Monitoring")],
        "variant_names": [de("SuK")],
        "explanation": [{"lang": "en-GB", "value": "A subject."}],
        "usage_hint": en("in notes"),  # the default keeps its own tag
    }


def test_tags_get_canonical_case_and_the_text_form_is_read(svc: ServiceEnv) -> None:
    attrs = create(
        svc,
        attrs={"heading": [{"lang": "DE-ch", "value": "Kontrolle"}]},
        attrs_text={"usage_hint": "Notizen@DE"},
    )
    assert attrs["heading"] == [{"lang": "de-CH", "value": "Kontrolle"}]
    assert attrs["usage_hint"] == de("Notizen")


def test_list_changes_tag_added_values_and_remove_by_text(svc: ServiceEnv) -> None:
    result = svc.apply(
        {
            "op": "create",
            "type": GLOSSARY,
            "local": "g",
            "attrs": {"heading": ["Kontrolle"], "variant_names": [de("SuK"), en("SuK"), en("K")]},
        }
    )
    entity = str(result.entities[0].id)
    svc.apply({"op": "update", "id": entity, "add_text": {"variant_names": ["S", "C@en"]}})
    assert attrs_of(svc, entity)["variant_names"] == [
        de("SuK"),
        en("SuK"),
        en("K"),
        de("S"),
        en("C"),
    ]
    svc.apply({"op": "update", "id": entity, "remove": {"variant_names": ["SuK", en("K")]}})
    assert attrs_of(svc, entity)["variant_names"] == [de("S"), en("C")]
    svc.apply({"op": "update", "id": entity, "set": {"usage_hint": "selten"}})
    assert attrs_of(svc, entity)["usage_hint"] == de("selten")


def test_without_a_graph_language_untagged_values_are_refused(backend: str, tmp_path: Path) -> None:
    svc = svc_for(backend, tmp_path / "root", None)
    error = svc.refuse(
        {"op": "create", "type": GLOSSARY, "local": "g", "attrs_text": {"heading": "Kontrolle"}}
    )
    (finding,) = error.details["findings"]  # type: ignore[index, misc]
    assert finding["code"] == "data.structural.datatype_mismatch"
    assert "no default language" in finding["message"]
    attrs = create(svc, attrs_text={"heading": '"Kontrolle"@de'})
    assert attrs["heading"] == [de("Kontrolle")]


def test_one_main_name_per_language(svc: ServiceEnv) -> None:
    error = svc.refuse(
        {"op": "create", "type": GLOSSARY, "local": "g", "attrs": {"heading": ["a", "b"]}}
    )
    (finding,) = error.details["findings"]  # type: ignore[index, misc]
    assert finding["code"] == "data.structural.language_repeated"

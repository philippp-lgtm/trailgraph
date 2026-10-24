"""S: language-tagged values through ``graph`` (Q18 stage 3).

A graph created with ``--language de`` stores untagged text as German;
``"text"@tag`` and ``text@tag`` name another language; ``find``, ``lookup``
and ``search`` compare the text, narrow by language and show the label in
the preferred language. A graph without a language refuses untagged text.
"""

import json

import pytest
from harness import CliRunner
from support.admin import init
from support.app import ENV, app, create_graph

pytestmark = pytest.mark.slow

GLOSSARY = "nas:GlossaryEntry"


@pytest.fixture
def entry(run_cli: CliRunner, isolated_env: dict[str, str]) -> str:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", language="de", version="1.2.0")
    names = [
        "Steuerung und Kontrolle",
        {"lang": "en", "value": "Control and Monitoring"},
    ]
    outcome = app(
        run_cli,
        "create",
        GLOSSARY,
        "--set-json",
        f"heading={json.dumps(names)}",
        "--set",
        "variant_names=SuK",
        "--set",
        'usage_hint="for exams"@en-GB',
        env=ENV,
    )
    assert outcome.code == 0, outcome.envelope
    entity: str = outcome.data["entities"][0]["id"]
    return entity


def test_untagged_text_takes_the_graph_language(run_cli: CliRunner, entry: str) -> None:
    attrs = app(run_cli, "get", entry, env=ENV).data["entity"]["attrs"]
    assert attrs == {
        "heading": [
            {"lang": "de", "value": "Steuerung und Kontrolle"},
            {"lang": "en", "value": "Control and Monitoring"},
        ],
        "variant_names": [{"lang": "de", "value": "SuK"}],
        "usage_hint": {"lang": "en-GB", "value": "for exams"},
    }


def test_query_sees_a_map_and_tables_show_the_tag(run_cli: CliRunner, entry: str) -> None:
    cypher = "MATCH (g:GlossaryEntry) RETURN g.usage_hint AS hint, g.usage_hint.lang AS lang"
    (row,) = app(run_cli, "query", cypher, env=ENV).data["items"]
    assert row == {"hint": {"lang": "en-GB", "value": "for exams"}, "lang": "en-GB"}
    table = run_cli("graph", "query", cypher, env=ENV).stdout
    assert '"for exams"@en-GB' in table


def test_find_compares_the_text_and_narrows_by_language(run_cli: CliRunner, entry: str) -> None:
    def found(*where: str) -> list[str]:
        args = [arg for w in where for arg in ("--where", w)]
        items = app(run_cli, "find", "--type", GLOSSARY, *args, env=ENV).data["items"]
        return [item["id"] for item in items]

    assert found("variant_names=SuK") == [entry]
    assert found("variant_names=SuK@de") == [entry]
    assert found("variant_names=SuK@en") == []
    assert found("heading:lang=en") == [entry]
    assert found("usage_hint:lang=en") == [entry]  # en matches en-GB
    assert found("usage_hint:lang=de") == []
    assert found("usage_hint:prefix=for") == [entry]


def test_lookup_names_the_language_and_picks_the_label(run_cli: CliRunner, entry: str) -> None:
    (item,) = app(run_cli, "lookup", "suk", env=ENV).data["items"]
    assert item["match"]["lang"] == "de"
    assert item["label"] == "Steuerung und Kontrolle"
    (english,) = app(run_cli, "lookup", "Control and Monitoring", env=ENV).data["items"]
    assert english["label"] == "Steuerung und Kontrolle"  # the graph language comes first
    (chosen,) = app(run_cli, "lookup", "suk", "--lang", "*", env=ENV).data["items"]
    assert chosen["id"] == entry
    assert app(run_cli, "lookup", "suk", "--lang", "en", env=ENV).data["items"] == []
    (hit,) = app(run_cli, "search", "monitoring", "--lang", "en", env=ENV).data["items"]
    assert hit["match"]["lang"] == "en"
    refused = app(run_cli, "lookup", "suk", "--lang", "de_DE", env=ENV)
    assert refused.error["code"] == "usage.invalid_argument"


def test_a_graph_without_a_language_refuses_untagged_text(
    run_cli: CliRunner, isolated_env: dict[str, str]
) -> None:
    init(run_cli, isolated_env)
    create_graph(run_cli, "example_graph", "notes_and_sources", version="1.2.0")
    refused = app(run_cli, "create", GLOSSARY, "--set", "heading=Kontrolle", env=ENV)
    assert refused.code != 0
    (finding,) = refused.error["details"]["findings"]
    assert finding["code"] == "data.structural.datatype_mismatch"
    assert "no default language" in finding["message"]
    tagged = app(run_cli, "create", GLOSSARY, "--set", "heading=Kontrolle@de", env=ENV)
    assert tagged.code == 0, tagged.envelope

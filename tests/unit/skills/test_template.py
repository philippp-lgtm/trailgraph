"""Graph skill templates: block markers, kind detection and the template rules (Q2, Q10)."""

import pytest

from graph_cli.skills.template import (
    BLOCKS,
    GENERATED,
    MODEL_FILE,
    STATIC,
    kind_of,
    parse_template,
)

FRONT = '---\nname: graph-example-graph\ndescription: "Example."\n---\n\n'


def files(body: str, extra: dict[str, str] | None = None) -> dict[str, bytes]:
    found = {"SKILL.md": (FRONT + body).encode()}
    found.update({name: text.encode() for name, text in (extra or {}).items()})
    return found


def problems(found: dict[str, bytes]) -> list[str]:
    template, broken = parse_template(found)
    assert (template is None) == bool(broken)
    return [p.message for p in broken]


def test_markers_are_found_with_their_lines() -> None:
    template, broken = parse_template(
        files(
            "# Graph\n\n<!-- graph_cli:block start -->\n\nText.\n<!-- graph_cli:block types -->\n"
        )
    )
    assert broken == [] and template is not None
    assert [name for _, name in template.blocks] == ["start", "types"]
    assert [template.lines[index] for index, _ in template.blocks] == [
        "<!-- graph_cli:block start -->",
        "<!-- graph_cli:block types -->",
    ]


def test_every_block_name_is_known() -> None:
    body = "\n".join(f"<!-- graph_cli:block {name} -->" for name in BLOCKS) + "\n"
    assert problems(files(body)) == []


def test_kind_follows_the_markers() -> None:
    assert kind_of(files("# Graph\n\n<!-- graph_cli:block names -->\n")) == GENERATED
    assert kind_of(files("# Graph\n\nNo markers.\n")) == STATIC
    assert kind_of(files("# Graph\n\n<!-- graph_cli:block bogus -->\n")) == GENERATED
    assert kind_of({"references/x.md": b"<!-- graph_cli:block types -->\n"}) == STATIC


def test_a_marker_inside_a_line_is_plain_text() -> None:
    found = files("See `<!-- graph_cli:block types -->` for the marker syntax.\n")
    assert kind_of(found) == STATIC
    template, broken = parse_template(found)
    assert broken == [] and template is not None and template.blocks == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("<!-- graph_cli:block bogus -->\n", "unknown block 'bogus'"),
        ("<!-- graph_cli:block types -->\n<!-- graph_cli:block types -->\n", "used twice"),
        ("<!-- graph_cli:block -->\n", "not a block marker"),
        ("<!-- graph_cli:blocks types -->\n", "not a block marker"),
    ],
)
def test_template_rules(body: str, message: str) -> None:
    (found,) = problems(files("<!-- graph_cli:block start -->\n" + body))
    assert message in found


def test_the_model_reference_is_generated_only() -> None:
    (found,) = problems(files("<!-- graph_cli:block model -->\n", {MODEL_FILE: "# Mine\n"}))
    assert MODEL_FILE == "references/model.md" and "generated from the model" in found
    assert problems(files("<!-- graph_cli:block model -->\n", {"references/x.md": "# x\n"})) == []


def test_markers_outside_skill_md_are_refused() -> None:
    extra = {"references/extra.md": "<!-- graph_cli:block types -->\n"}
    (found,) = problems(files("<!-- graph_cli:block start -->\n", extra))
    assert "SKILL.md only" in found


def test_curated_text_blanks_the_markers_and_keeps_line_numbers() -> None:
    template, _ = parse_template(
        files(
            "Intro.\n<!-- graph_cli:block types -->\nOutro.\n", {"references/extra.md": "Extra.\n"}
        )
    )
    assert template is not None
    curated = template.curated()
    assert curated["SKILL.md"].split("\n")[5:8] == ["Intro.", "", "Outro."]
    assert curated["references/extra.md"] == "Extra.\n"

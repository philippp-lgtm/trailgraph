"""Small authored ontologies for tests, compiled from inline module texts."""

from collections.abc import Mapping

from graph_cli.ontology_layer.bundle import LayeredSource, MappingSource
from graph_cli.ontology_layer.compiler import compile_bundle
from graph_cli.ontology_layer.model import CompiledModel


def header(module_id: str, prefix: str, version: str, imports: tuple[str, ...] = ()) -> str:
    """The format line and module block of an authored module (imports as ``^1.0`` ranges)."""
    lines = [
        "format: graph_cli.ir/1",
        "module:",
        f"  id: {module_id}",
        f"  prefix: {prefix}",
        f"  version: {version}",
        "  summary: Test module.",
    ]
    if imports:
        lines.append("  imports:")
        lines += [f'    - {{module: {name}, version: "^1.0"}}' for name in imports]
    return "\n".join(lines) + "\n"


def compile_modules(main: str, files: Mapping[str, str]) -> CompiledModel:
    """Compile ``{module id: text}`` with ``main`` as the main module."""
    source = LayeredSource(MappingSource({k: v.encode() for k, v in files.items()}))
    outcome = compile_bundle(main, source)
    assert outcome.model is not None, [f.to_json() for f in outcome.findings]
    return outcome.model


def single(
    version: str, body: str, *, module_id: str = "example", prefix: str = "ex"
) -> CompiledModel:
    """A one-module ontology: ``body`` is the text after the module block."""
    return compile_modules(module_id, {module_id: header(module_id, prefix, version) + body})

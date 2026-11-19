"""The inventory series with one class moved to another module, for the release tests of
policies and catalogs that follow a rename (migration plan M8).

``moved_source`` is 2.0.0: ``inv:Supplier`` is now ``sup:Supplier`` (``renamed_from`` names the
old CURIE), everything else is as in 1.1.0. Published against 1.1.0 (``module: inventory``: with
several modules a function names its module), a function with the bare
name ``Supplier`` follows the move; one with the CURIE ``inv:Supplier`` does not (an action
names its classes by CURIE, so it never follows a move of one it names). A view follows
the same way: ``BARE_VIEW`` lists ``Supplier`` by its bare name and runs ``BARE_FUNCTION``,
``CURIE_VIEW`` names the old CURIE.
"""

from pathlib import Path

from support.fixtures import ONTOLOGIES

INVENTORY = ONTOLOGIES / "inventory"

SUPPLIERS_MODULE = """\
format: graph_cli.ir/1
module: {id: suppliers, prefix: sup, version: 1.0.0, summary: Suppliers.}
classes:
  Supplier:
    summary: A company that delivers items.
    display_prefix: SUP
    renamed_from: inv:Supplier
    attributes:
      name: {summary: Name of the supplier., type: string, required: true}
"""

MIGRATION = """\
format: graph_cli.migration/1
from: 1.1.0
summary: Suppliers moved to their own module.
steps: []
"""

BARE_FUNCTION = """\
schema: graph_cli.function/1
name: suppliers_by_name
module: inventory
summary: The suppliers, by name.
use_when: >-
  Which suppliers exist; a list of suppliers.
query: |
  MATCH (s:Supplier)
  RETURN s.name AS supplier
  ORDER BY supplier
returns:
  - column: supplier
    summary: Name of the supplier.
examples:
  - question: Which suppliers deliver to us?
"""

CURIE_FUNCTION = """\
schema: graph_cli.function/1
name: suppliers_exact
module: inventory
summary: The suppliers, named by their CURIE.
use_when: >-
  Which suppliers exist, asked with the full type name.
query: |
  MATCH (s:`inv:Supplier`)
  RETURN s.name AS supplier
  ORDER BY supplier
returns:
  - column: supplier
    summary: Name of the supplier.
examples:
  - question: Which suppliers are there?
"""

BARE_VIEW = """\
schema: graph_cli.view/1
name: supplier_catalog
module: inventory
summary: Every supplier, for renderers.
use_when: >-
  Export or render the suppliers; a list of suppliers.
sections:
  - kind: list
    id: suppliers
    type: Supplier
    fields: [name]
  - kind: rows
    id: names
    function: suppliers_by_name
"""

CURIE_VIEW = """\
schema: graph_cli.view/1
name: supplier_exact
module: inventory
summary: Every supplier, named by its CURIE.
use_when: >-
  Export or render the suppliers, asked with the full type name.
sections:
  - kind: list
    id: suppliers
    type: inv:Supplier
    fields: [name]
"""

ACTION = """\
schema: graph_cli.action/1
name: rename_supplier
module: inventory
summary: Give a supplier another name.
use_when: >-
  A supplier changes its name.
params:
  supplier:
    type: ref
    class: inv:Supplier
    summary: The supplier.
  name:
    type: string
    summary: The new name.
ops:
  - {op: update, id: $supplier, set: {name: $name}}
examples:
  - request: The supplier is now called Example Trading.
"""


def moved_source(parent: Path, name: str = "moved", *, migration: bool = False) -> Path:
    """The 2.0.0 version directory (main module ``inventory``), with its migration file if
    asked for."""
    text = (INVENTORY / "1.1.0" / "inventory.yaml").read_text()
    text = text.replace("  version: 1.1.0\n", "  version: 2.0.0\n", 1)
    text = text.replace(
        "  summary: Physical items",
        '  imports:\n    - {module: suppliers, version: "^1.0"}\n  summary: Physical items',
        1,
    )
    start = text.index("  Supplier:\n")
    end = text.index("  Category:\n")
    text = text[:start] + text[end:]
    text = text.replace("range: Supplier", "range: sup:Supplier", 1)
    source = parent / name
    source.mkdir()
    (source / "inventory.yaml").write_text(text)
    (source / "suppliers.yaml").write_text(SUPPLIERS_MODULE)
    if migration:
        (source / "migration.yaml").write_text(MIGRATION)
    return source


def write_folder(parent: Path, name: str, files: dict[str, str]) -> Path:
    """A ``functions/`` or ``actions/`` folder with one ``<name>.yaml`` per entry."""
    folder = parent / name
    folder.mkdir()
    for stem, text in files.items():
        (folder / f"{stem}.yaml").write_text(text)
    return folder

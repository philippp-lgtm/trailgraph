"""Take the screenshots and the terminal transcript of the documentation.

    uv run --with playwright==1.65.2 python scripts/docs_screenshots.py

Builds the example graph `examples/company` in a throwaway installation (its `setup.sh`,
in a temporary directory), starts `graph-admin view` on it and takes the pictures with
Playwright's Chromium, which it installs on first use. The `graph` and `graph-admin` on
`PATH` are used: `uv run` puts those of the checkout there. Writes:

- `docs/images/live_view_model.png`: the type graph of the example graph;
- `docs/images/live_view_graph.png`: the graph itself, while the transcript below runs;
- `docs/images/live_view_activity.png`: the details panel with the activity list;
- the `terminal_session` transcript block of `docs/overview.md`: the commands of
  `TRANSCRIPT` and their real output.

Nothing outside the temporary directory, the view's state directory and those files is
touched; both directories are removed at the end. Review the diff before committing.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import FloatRect, Page, ViewportSize

REPO = Path(__file__).resolve().parent.parent
IMAGES = REPO / "docs" / "images"
OVERVIEW = REPO / "docs" / "overview.md"
SETUP = REPO / "examples" / "company" / "setup.sh"
VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
# The agent the transcript and the activity name, instead of the OS user.
AGENT = "assistant"
# An agent session: start, find a person, ask a function, record a fact, ask again.
PERSON = "EMP-0011"
TRANSCRIPT: tuple[tuple[str, ...], ...] = (
    ("graph", "start"),
    ("graph", "lookup", "Noah Becker"),
    ("graph", "run", "required_trainings", "--param", f"person={PERSON}"),
    (
        "graph", "link", PERSON, "ops:completed", "TRN-0001", "--qualifier", "on=2026-11-20",
        "--reason", "Completion confirmed in the training records",
        "--idempotency-key", "noah-security-awareness",
    ),
    ("graph", "run", "required_trainings", "--param", f"person={PERSON}"),
)  # fmt: skip
TRANSCRIPT_BLOCK = re.compile(
    r"(<!-- transcript: terminal_session -->\n)```text\n.*?```\n(<!-- /transcript -->)", re.DOTALL
)
ADDRESS = re.compile(r"https?://\S+")


def main() -> None:
    for tool in ("graph", "graph-admin"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on PATH; run this script with `uv run` from the checkout")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    with installation() as (env, work):
        with view(env, work) as address:
            shots, transcript = capture(address, env, work)
        check_leaks(transcript, work)
    IMAGES.mkdir(exist_ok=True)
    for name, png in shots.items():
        (IMAGES / f"{name}.png").write_bytes(png)
    text = OVERVIEW.read_text()
    block = f"```text\n{transcript}```\n"
    new, count = TRANSCRIPT_BLOCK.subn(lambda m: m.group(1) + block + m.group(2), text)
    if count != 1:
        sys.exit("docs/overview.md has no terminal_session transcript block")
    OVERVIEW.write_text(new)
    print("Wrote", ", ".join(sorted(shots)), "and the transcript; review the diff.")


@contextmanager
def installation() -> Iterator[tuple[dict[str, str], Path]]:
    """The example graph in a throwaway installation: its environment and working directory."""
    with tempfile.TemporaryDirectory(prefix="graph_docs_") as tmp:
        base = Path(tmp) / "example"
        caller = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_")}
        subprocess.run([str(SETUP)], env={**caller, "BASE": str(base)}, check=True,
                       stdout=subprocess.DEVNULL)  # fmt: skip
        dump = subprocess.run(
            ["bash", "-c", 'source "$1" >/dev/null && env -0', "-", str(base / "env.sh")],
            env=caller, check=True, capture_output=True, text=True,
        ).stdout  # fmt: skip
        env = dict(item.split("=", 1) for item in dump.split("\0") if "=" in item)
        env |= {"GRAPH_CLI_AGENT_ID": AGENT, "GRAPH_CLI_AGENT_TYPE": AGENT}
        config = Path(env["XDG_CONFIG_HOME"]) / "graph_cli" / "config.toml"
        with config.open("a") as f:
            f.write("\n[activity]\nsignal_reads = true\n")
        try:
            yield env, base / "work"
        finally:
            shutil.rmtree(env["XDG_STATE_HOME"], ignore_errors=True)


@contextmanager
def view(env: dict[str, str], work: Path) -> Iterator[str]:
    """`graph-admin view` on the installation, stopped by its own PID: the page's address."""
    proc = subprocess.Popen(
        ["graph-admin", "view", "--no-open", "--port", "0"],
        env=env, cwd=work, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            if match := ADDRESS.search(line):
                yield match.group(0)
                return
        sys.exit(f"graph-admin view ended without an address (exit {proc.wait()})")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def capture(address: str, env: dict[str, str], work: Path) -> tuple[dict[str, bytes], str]:
    from playwright.sync_api import sync_playwright

    shots: dict[str, bytes] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # The page's CSP forbids eval, which the wait conditions below need.
        page = browser.new_page(viewport=VIEWPORT, color_scheme="light", bypass_csp=True)
        page.goto(f"{address}&graph=company")
        page.wait_for_function("tg !== null && tg.alpha < 0.004", timeout=60_000)
        page.wait_for_timeout(300)
        shots["live_view_model"] = page.screenshot()
        page.goto(f"{address}&graph=company&tab=graph")
        page.wait_for_function("statusLine.textContent === 'live' && alpha < 0.005", timeout=60_000)
        transcript = "".join(run(command, env, work) for command in TRANSCRIPT)
        page.wait_for_selector("#events li")
        page.wait_for_timeout(250)
        shots["live_view_graph"] = page.screenshot()
        shots["live_view_activity"] = page.screenshot(clip=activity_box(page))
        browser.close()
    return shots, transcript


def activity_box(page: Page) -> FloatRect:
    """The activity list of the details panel, with its heading."""
    panel, heading, last = (
        page.locator(selector).bounding_box()
        for selector in ("#panel", "#panel > h2", "#events li >> nth=-1")
    )
    assert panel and heading and last
    top = heading["y"] - 12
    bottom = last["y"] + last["height"] + 12
    return {"x": panel["x"], "y": top, "width": panel["width"], "height": bottom - top}


def run(command: tuple[str, ...], env: dict[str, str], work: Path) -> str:
    done = subprocess.run(command, env=env, cwd=work, capture_output=True, text=True, check=True)
    time.sleep(0.4)  # let the view draw each step
    return f"$ {shlex.join(command)}\n{done.stdout}"


def check_leaks(transcript: str, work: Path) -> None:
    """Refuse output that names this machine: the OS user or the temporary path."""
    user = getpass.getuser()
    leaks = [str(work.parent.parent), *([user] if len(user) > 2 else [])]
    found = [leak for leak in leaks if re.search(rf"\b{re.escape(leak)}\b", transcript)]
    if found:
        sys.exit(f"the transcript names {found}; nothing was written")


if __name__ == "__main__":
    main()

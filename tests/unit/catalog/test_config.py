"""``config.toml`` (contracts B12.7, V.3, V.4; plan M5)."""

from pathlib import Path

import pytest

from graph_cli.catalog.config import (
    Config,
    S3Table,
    format_new_config,
    load_config,
    parse_config,
    remote_config,
)
from graph_cli.kernel.errors import GraphCliError

PATH = Path("/cfg/graph_cli/config.toml")
FULL = b"""format_version = 1
[device]
id = "0192e000-0000-7000-8000-00000000d001"
name = "workstation"
[runtime]
mode = "remote"
endpoint = "https://graphs.example.org/api/"
[paths]
data_root = "~/graphs"
[identity]
providers = ["generic_env"]
[storage]
lock_timeout_s = 2.5
durability = "fsync"
[audit]
record_data_root = false
access_log = true
[output]
default = "json"
spill_bytes = 0
spill_dir = "~/spill"
[roots]
notes = "~/synced/notes"
archive_2 = "/srv/archive"
[files]
max_bytes = 2048
store = "s3"
[files.s3]
bucket = "example-bucket"
prefix = "graph_cli"
endpoint_url = "https://objects.example.org"
region = "example-region"
addressing = "virtual"
allow_plain_http = true
[extract]
timeout_seconds = 12.5
max_memory_mib = 256
[activity]
signal_reads = true
[server]
cors_origins = ["https://app.example.org", "http://localhost:5173"]
client_builds = "warn"
public_url = "https://graph.example.org/api/"
[remote]
token_file = "~/.config/graph_cli/token"
allow_plain_http = false
timeout_s = 30
"""


def test_missing_file_is_the_default(tmp_path: Path) -> None:
    config, warnings = load_config(tmp_path / "config.toml")
    assert config == Config(tmp_path / "config.toml")
    assert (config.lock_timeout_s, config.durability, config.output) == (10.0, "full", "text")
    assert warnings == ()


def test_every_key() -> None:
    config, warnings = parse_config(FULL, path=PATH)
    assert warnings == ()
    assert config == Config(
        path=PATH,
        exists=True,
        device_id="0192e000-0000-7000-8000-00000000d001",
        device_name="workstation",
        data_root="~/graphs",
        identity_providers=("generic_env",),
        lock_timeout_s=2.5,
        durability="fsync",
        record_data_root=False,
        access_log=True,
        output="json",
        spill_bytes=0,
        spill_dir="~/spill",
        file_roots={"archive_2": "/srv/archive", "notes": "~/synced/notes"},
        max_file_bytes=2048,
        files_store="s3",
        files_s3=S3Table(
            bucket="example-bucket",
            prefix="graph_cli",
            endpoint_url="https://objects.example.org",
            region="example-region",
            addressing="virtual",
            allow_plain_http=True,
        ),
        extract_timeout_s=12.5,
        extract_max_memory_mib=256,
        signal_reads=True,
        mode="remote",
        endpoint="https://graphs.example.org/api/",
        cors_origins=("https://app.example.org", "http://localhost:5173"),
        client_builds="warn",
        public_url="https://graph.example.org/api",
        token_file="~/.config/graph_cli/token",
        remote_timeout_s=30.0,
    )


def test_plain_http_beyond_loopback_needs_the_switch() -> None:
    """B14.10: the token would travel in clear."""
    loopback = (
        b'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://127.0.0.1:8470"\n'
    )
    assert parse_config(loopback, path=PATH)[0].mode == "remote"
    allowed = (
        b'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://x.example"\n'
        b"[remote]\nallow_plain_http = true\n"
    )
    assert parse_config(allowed, path=PATH)[0].allow_plain_http is True


def test_extract_defaults() -> None:
    config, _ = parse_config(b"format_version = 1\n", path=PATH)
    assert (config.extract_timeout_s, config.extract_max_memory_mib) == (30.0, 512)


def test_file_defaults() -> None:
    config, _ = parse_config(b"format_version = 1\n", path=PATH)
    assert (config.file_roots, config.max_file_bytes) == ({}, 100 * 1024 * 1024)
    assert (config.files_store, config.files_s3) == ("local", S3Table())
    assert config.signal_reads is False
    assert config.access_log is False
    assert config.client_builds == "compatible"
    for mode in ("compatible", "exact", "warn"):
        body = f'format_version = 1\n[server]\nclient_builds = "{mode}"\n'.encode()
        assert parse_config(body, path=PATH)[0].client_builds == mode


def test_integer_timeout() -> None:
    config, _ = parse_config(b"format_version = 1\n[storage]\nlock_timeout_s = 3\n", path=PATH)
    assert config.lock_timeout_s == 3.0


def test_unknown_keys_warn() -> None:
    data = b'format_version = 1\ncolour = "x"\n[device]\nid = "d"\nserial = 3\n[extra]\n'
    config, warnings = parse_config(data, path=PATH)
    assert config.device_id == "d"
    assert [(w.code, w.details["key"]) for w in warnings] == [
        ("config.unknown_key", "colour"),
        ("config.unknown_key", "device.serial"),
        ("config.unknown_key", "extra"),
    ]
    _, warnings = parse_config(b"format_version = 1\n[files]\nkeep = 1\n", path=PATH)
    assert [w.details["key"] for w in warnings] == ["files.keep"]
    _, warnings = parse_config(b"format_version = 1\n[activity]\nwrites = true\n", path=PATH)
    assert [w.details["key"] for w in warnings] == ["activity.writes"]


def test_unknown_major() -> None:
    with pytest.raises(GraphCliError) as caught:
        parse_config(b"format_version = 2\n", path=PATH)
    assert caught.value.code == "format.unsupported_version"


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("", "format_version"),
        ('format_version = 1\n[runtime]\nmode = "remote"\n', "runtime.endpoint"),
        (
            'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "ftp://x.example"\n',
            "runtime.endpoint",
        ),
        (
            'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "http://x.example"\n',
            "runtime.endpoint",
        ),
        (
            'format_version = 1\n[runtime]\nmode = "remote"\nendpoint = "https://u:p@x.example"\n',
            "runtime.endpoint",
        ),
        ('format_version = 1\n[remote]\ntoken_file = "token"\n', "remote.token_file"),
        ("format_version = 1\n[remote]\ntimeout_s = 0\n", "remote.timeout_s"),
        (
            'format_version = 1\n[server]\ncors_origins = ["https://x.example/path"]\n',
            "server.cors_origins",
        ),
        ('format_version = 1\n[runtime]\nmode = "cloud"\n', "runtime.mode"),
        ('format_version = 1\n[server]\nclient_builds = "any"\n', "server.client_builds"),
        ('format_version = 1\n[server]\npublic_url = "graph.example.org"\n', "server.public_url"),
        (
            'format_version = 1\n[server]\npublic_url = "https://x.example/?a=1"\n',
            "server.public_url",
        ),
        (
            'format_version = 1\n[server]\npublic_url = "https://user:pw@x.example/"\n',
            "server.public_url",
        ),
        ("format_version = 1\n[storage]\nlock_timeout_s = true\n", "storage.lock_timeout_s"),
        ("format_version = 1\n[storage]\nlock_timeout_s = 0\n", "storage.lock_timeout_s"),
        ('format_version = 1\n[storage]\ndurability = "none"\n', "storage.durability"),
        ('format_version = 1\n[paths]\ndata_root = "relative"\n', "paths.data_root"),
        ('format_version = 1\n[identity]\nproviders = "os"\n', "identity.providers"),
        ("format_version = 1\n[identity]\nproviders = [1]\n", "identity.providers"),
        ('format_version = 1\n[audit]\nrecord_data_root = "no"\n', "audit.record_data_root"),
        ('format_version = 1\n[output]\ndefault = "yaml"\n', "output.default"),
        ("format_version = 1\n[device]\nid = 7\n", "device.id"),
        ("format_version = 1\ndevice = 3\n", "device"),
        ("format_version = ", ""),
        ('format_version = 1\n[roots]\nNotes = "/n"\n', "roots.Notes"),
        ('format_version = 1\n[roots]\n"my-notes" = "/n"\n', "roots.my-notes"),
        ('format_version = 1\n[roots]\nnotes = "relative/n"\n', "roots.notes"),
        ("format_version = 1\n[roots]\nnotes = 3\n", "roots.notes"),
        ("format_version = 1\nroots = 3\n", "roots"),
        ("format_version = 1\n[files]\nmax_bytes = 0\n", "files.max_bytes"),
        ("format_version = 1\n[files]\nmax_bytes = 1073741825\n", "files.max_bytes"),
        ('format_version = 1\n[files]\nmax_bytes = "1M"\n', "files.max_bytes"),
        ("format_version = 1\n[extract]\ntimeout_seconds = 0\n", "extract.timeout_seconds"),
        ("format_version = 1\n[extract]\ntimeout_seconds = 3601\n", "extract.timeout_seconds"),
        ('format_version = 1\n[extract]\ntimeout_seconds = "30"\n', "extract.timeout_seconds"),
        ("format_version = 1\n[extract]\nmax_memory_mib = 63\n", "extract.max_memory_mib"),
        ("format_version = 1\n[extract]\nmax_memory_mib = 1.5\n", "extract.max_memory_mib"),
        ("format_version = 1\n[output]\nspill_bytes = -1\n", "output.spill_bytes"),
        ('format_version = 1\n[output]\nspill_dir = "spill"\n', "output.spill_dir"),
        ("format_version = 1\n[activity]\nsignal_reads = 1\n", "activity.signal_reads"),
        ('format_version = 1\n[audit]\naccess_log = "yes"\n', "audit.access_log"),
    ],
)
def test_defects_are_config_invalid(body: str, key: str) -> None:
    with pytest.raises(GraphCliError) as caught:
        parse_config(body.encode(), path=PATH)
    assert caught.value.code == "config.invalid"
    assert caught.value.details["key"] == key


def test_new_config_round_trips() -> None:
    data = format_new_config(device_id="dev-1", device_name="box", data_root="/srv/graphs")
    config, warnings = parse_config(data, path=PATH)
    assert (config.device_id, config.device_name, config.data_root) == (
        "dev-1",
        "box",
        "/srv/graphs",
    )
    assert warnings == ()
    minimal, _ = parse_config(format_new_config(device_id="dev-1"), path=PATH)
    assert (minimal.device_name, minimal.data_root) == (None, None)


# ---- graph connect: the remote mode written into the file (B14.21) ----------------------

ENDPOINT = "https://graphs.example.org"


def test_a_new_config_gets_the_remote_mode_and_a_device() -> None:
    data = remote_config(None, path=PATH, endpoint=ENDPOINT, device_id="dev-1")
    config, warnings = parse_config(data, path=PATH)
    assert (config.mode, config.endpoint, config.device_id) == ("remote", ENDPOINT, "dev-1")
    assert warnings == ()


@pytest.mark.parametrize(
    "text",
    [
        'format_version = 1\n# mine\n[device]\nid = "d"\n',
        'format_version = 1\n[runtime]\nmode = "local" # was\n\n[paths]\ndata_root = "/d"\n',
        'format_version = 1\n[runtime]\nendpoint = "https://old.example.org"\nmode = "remote"',
    ],
)
def test_an_existing_config_keeps_everything_else(text: str) -> None:
    import tomllib

    edited = remote_config(text.encode(), path=PATH, endpoint=ENDPOINT, device_id="unused")
    before, after = tomllib.loads(text), tomllib.loads(edited.decode())
    assert after.pop("runtime") == {"mode": "remote", "endpoint": ENDPOINT}
    before.pop("runtime", None)
    assert after == before
    for line in text.splitlines():
        if line.startswith("#") or line.startswith("["):
            assert line in edited.decode()


def test_a_config_that_cannot_be_edited_safely_is_refused() -> None:
    for text in ('format_version = 1\nruntime.mode = "local"\n', "runtime = 1\n", "x = [\n"):
        with pytest.raises(GraphCliError) as caught:
            remote_config(text.encode(), path=PATH, endpoint=ENDPOINT, device_id="d")
        assert caught.value.code == "config.invalid"

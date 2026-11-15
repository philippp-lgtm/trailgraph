"""Object store settings: ``[files] store``, ``[files.s3]`` and the environment.

The file's shape is checked on every read (``parse_config``); the full validation with
the environment (``object_store``) stops a command only with ``store = "s3"``, otherwise
the error is kept for a graph whose manifest names an object store. No message, detail
or ``repr`` carries a secret.
"""

from pathlib import Path

import pytest

from graph_cli.catalog.config import Config, parse_config
from graph_cli.catalog.object_store import object_store, object_store_settings
from graph_cli.kernel.errors import GraphCliError
from graph_cli.storage.s3.settings import ObjectStoreCredentials, ObjectStoreSettings

PATH = Path("/cfg/graph_cli/config.toml")
KEY_ID = "EXAMPLEKEYID0000"
SECRET = "example/secret+value"
TOKEN = "example-session-token"
SECRETS = (KEY_ID, SECRET, TOKEN, "pw-in-url")
BASE = {"GRAPH_CLI_S3_BUCKET": "example-bucket", "AWS_REGION": "example-region"}


def config(s3: str = "", *, store: str | None = None) -> Config:
    body = "format_version = 1\n[files]\n"
    if store is not None:
        body += f'store = "{store}"\n'
    if s3:
        body += "[files.s3]\n" + s3
    return parse_config(body.encode(), path=PATH)[0]


def refused(cfg: Config, env: dict[str, str]) -> GraphCliError:
    with pytest.raises(GraphCliError) as caught:
        object_store_settings(cfg, env)
    error = caught.value
    assert error.code == "config.invalid"
    shown = error.message + repr(error.details)
    for secret in SECRETS:
        assert secret not in shown
    return error


def where(error: GraphCliError) -> str:
    return str(error.details.get("key") or error.details.get("variable"))


# ---- defaults and shape ------------------------------------------------------


def test_nothing_configured_is_no_object_store() -> None:
    cfg = config()
    assert (cfg.files_store, cfg.files_s3.bucket) == ("local", None)
    choice = object_store(cfg, {})
    assert choice.settings is None
    assert choice.problem is not None
    assert (choice.problem.code, where(choice.problem)) == ("config.invalid", "files.s3.bucket")


def test_store_s3_without_a_bucket_stops_the_command() -> None:
    with pytest.raises(GraphCliError) as caught:
        object_store(config(store="s3"), {})
    assert (caught.value.code, where(caught.value)) == ("config.invalid", "files.s3.bucket")
    assert "GRAPH_CLI_S3_BUCKET" in caught.value.message


def test_file_values() -> None:
    cfg = config(
        'bucket = "example-bucket"\nprefix = "graph_cli"\n'
        'endpoint_url = "https://objects.example.org:9000/"\nregion = "example-region"\n'
        'addressing = "virtual"\n',
        store="s3",
    )
    choice = object_store(cfg, {})
    assert choice == type(choice)(
        ObjectStoreSettings(
            bucket="example-bucket",
            region="example-region",
            prefix="graph_cli/",
            endpoint_url="https://objects.example.org:9000",
            addressing="virtual",
        )
    )


def test_defaults_of_a_minimal_store() -> None:
    settings = object_store_settings(config(store="s3"), BASE)
    assert settings == ObjectStoreSettings(bucket="example-bucket", region="example-region")
    assert (settings.prefix, settings.endpoint_url, settings.addressing) == ("", None, "path")
    assert (settings.allow_plain_http, settings.credentials, settings.ca_bundle) == (
        False,
        None,
        None,
    )


def test_environment_wins_over_the_file() -> None:
    cfg = config(
        'bucket = "file-bucket"\nendpoint_url = "https://file.example.org"\n'
        'region = "file-region"\n'
    )
    assert object_store_settings(cfg, {}).bucket == "file-bucket"
    env = {
        "GRAPH_CLI_S3_BUCKET": "env-bucket",
        "AWS_ENDPOINT_URL": "https://generic.example.org",
        "AWS_DEFAULT_REGION": "default-region",
    }
    settings = object_store_settings(cfg, env)
    assert (settings.bucket, settings.endpoint_url, settings.region) == (
        "env-bucket",
        "https://generic.example.org",
        "default-region",
    )
    env |= {"AWS_ENDPOINT_URL_S3": "https://s3.example.org", "AWS_REGION": "env-region"}
    settings = object_store_settings(cfg, env)
    assert (settings.endpoint_url, settings.region) == ("https://s3.example.org", "env-region")


def test_an_empty_variable_counts_as_unset() -> None:
    cfg = config('bucket = "file-bucket"\nregion = "file-region"\n')
    env = {"GRAPH_CLI_S3_BUCKET": "", "AWS_REGION": "", "AWS_ENDPOINT_URL_S3": ""}
    settings = object_store_settings(cfg, env)
    assert (settings.bucket, settings.region, settings.endpoint_url) == (
        "file-bucket",
        "file-region",
        None,
    )


@pytest.mark.parametrize("store", ["local", "s3"])
@pytest.mark.parametrize(
    "key", ["access_key_id", "secret_access_key", "session_token", "Bucket", "credentials"]
)
def test_any_other_key_in_files_s3_is_refused(store: str, key: str) -> None:
    """Stricter than the unknown-key warning: a pasted secret stops the command."""
    body = f'format_version = 1\n[files]\nstore = "{store}"\n[files.s3]\n{key} = "{SECRET}"\n'
    with pytest.raises(GraphCliError) as caught:
        parse_config(body.encode(), path=PATH)
    assert (caught.value.code, caught.value.details["key"]) == ("config.invalid", f"files.s3.{key}")
    assert SECRET not in caught.value.message + repr(caught.value.details)
    assert "environment" in caught.value.message


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ('[files]\nstore = "remote"\n', "files.store"),
        ("[files]\nstore = 1\n", "files.store"),
        ("[files]\ns3 = 1\n", "files.s3"),
        ("[files.s3]\nbucket = 1\n", "files.s3.bucket"),
        ("[files.s3]\nprefix = true\n", "files.s3.prefix"),
        ("[files.s3]\nendpoint_url = []\n", "files.s3.endpoint_url"),
        ("[files.s3]\nregion = 3\n", "files.s3.region"),
        ('[files.s3]\naddressing = "dns"\n', "files.s3.addressing"),
        ('[files.s3]\nallow_plain_http = "yes"\n', "files.s3.allow_plain_http"),
        ("[files.s3.nested]\nx = 1\n", "files.s3.nested"),
    ],
)
def test_the_shape_is_checked_on_every_read(body: str, key: str) -> None:
    with pytest.raises(GraphCliError) as caught:
        parse_config(f"format_version = 1\n{body}".encode(), path=PATH)
    assert (caught.value.code, caught.value.details["key"]) == ("config.invalid", key)


def test_a_local_store_never_meets_an_invalid_files_s3() -> None:
    """Shape only when ``store = "local"``: the full check waits for an object store graph."""
    s3 = 'bucket = "Not_A_Bucket"\nendpoint_url = "http://objects.example.org"\n'
    choice = object_store(config(s3), {})
    assert choice.settings is None
    assert choice.problem is not None and where(choice.problem) == "files.s3.bucket"
    with pytest.raises(GraphCliError):
        object_store(config(s3, store="s3"), {})


# ---- full validation ---------------------------------------------------------


@pytest.mark.parametrize(
    "bucket", ["ab", "Example", "example_bucket", "-example", "example-", "a" * 64]
)
def test_bucket_names(bucket: str) -> None:
    error = refused(config(f'bucket = "{bucket}"\nregion = "r"\n'), {})
    assert where(error) == "files.s3.bucket"
    error = refused(config(), {**BASE, "GRAPH_CLI_S3_BUCKET": bucket})
    assert where(error) == "GRAPH_CLI_S3_BUCKET"


def test_region_is_required_and_named() -> None:
    error = refused(config('bucket = "example-bucket"\n'), {})
    assert where(error) == "files.s3.region"
    assert "AWS_REGION" in error.message
    assert where(refused(config(), {**BASE, "AWS_REGION": "eu/west"})) == "AWS_REGION"


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("ftp://objects.example.org", "http:// or https://"),
        ("objects.example.org", "http:// or https://"),
        ("https://", "http:// or https://"),
        ("https://user:pw-in-url@objects.example.org", "user info"),
        ("https://objects.example.org/?pw-in-url", "query"),
        ("https://objects.example.org/#pw-in-url", "fragment"),
        ("https://objects.example.org/bucket", "path"),
        ("https://objects.example.org:pw-in-url", "not a URL"),
    ],
)
def test_endpoint_shapes(endpoint: str, message: str) -> None:
    error = refused(config(f'endpoint_url = "{endpoint}"\n'), BASE)
    assert where(error) == "files.s3.endpoint_url"
    assert message in error.message
    error = refused(config(), {**BASE, "AWS_ENDPOINT_URL_S3": endpoint})
    assert error.details == {"variable": "AWS_ENDPOINT_URL_S3"}
    assert message in error.message


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1:3900", "http://localhost:9000", "http://[::1]:3900"]
)
def test_plain_http_to_loopback_is_allowed(endpoint: str) -> None:
    settings = object_store_settings(config(f'endpoint_url = "{endpoint}"\n'), BASE)
    assert settings.endpoint_url == endpoint
    assert settings.allow_plain_http is False


def test_plain_http_beyond_loopback_needs_the_switch() -> None:
    endpoint = "http://objects.internal:3900"
    error = refused(config(f'endpoint_url = "{endpoint}"\n'), BASE)
    assert "allow_plain_http" in error.message
    error = refused(config(), {**BASE, "AWS_ENDPOINT_URL": endpoint})
    assert error.details == {"variable": "AWS_ENDPOINT_URL"}
    allowed = config(f'endpoint_url = "{endpoint}"\nallow_plain_http = true\n')
    settings = object_store_settings(allowed, BASE)
    assert (settings.endpoint_url, settings.allow_plain_http) == (endpoint, True)
    assert object_store_settings(allowed, {**BASE, "AWS_ENDPOINT_URL": endpoint}).allow_plain_http


@pytest.mark.parametrize(
    ("prefix", "normalized"),
    [
        ("", ""),
        ("graph_cli", "graph_cli/"),
        ("graph_cli/", "graph_cli/"),
        ("team-a/graph_cli.v1", "team-a/graph_cli.v1/"),
    ],
)
def test_prefix_is_normalized(prefix: str, normalized: str) -> None:
    assert object_store_settings(config(f'prefix = "{prefix}"\n'), BASE).prefix == normalized


@pytest.mark.parametrize(
    "prefix", ["/graph_cli", "a//b", "a/../b", "./a", "a b", "a\\\\b", "a" * 300, "/"]
)
def test_bad_prefixes(prefix: str) -> None:
    assert where(refused(config(f'prefix = "{prefix}"\n'), BASE)) == "files.s3.prefix"


def test_credentials_come_only_from_the_environment() -> None:
    env = {**BASE, "AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    settings = object_store_settings(config(), env)
    assert settings.credentials == ObjectStoreCredentials(KEY_ID, SECRET)
    with_token = object_store_settings(config(), {**env, "AWS_SESSION_TOKEN": TOKEN})
    assert with_token.credentials == ObjectStoreCredentials(KEY_ID, SECRET, TOKEN)
    for secret in SECRETS:
        assert secret not in repr(with_token)
        assert secret not in repr(object_store(config(), {**env, "AWS_SESSION_TOKEN": TOKEN}))


@pytest.mark.parametrize(
    ("given", "variable"),
    [
        ({"AWS_ACCESS_KEY_ID": KEY_ID}, "AWS_SECRET_ACCESS_KEY"),
        ({"AWS_SECRET_ACCESS_KEY": SECRET}, "AWS_ACCESS_KEY_ID"),
        ({"AWS_SESSION_TOKEN": TOKEN}, "AWS_SESSION_TOKEN"),
        ({"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SESSION_TOKEN": TOKEN}, "AWS_SECRET_ACCESS_KEY"),
    ],
)
def test_incomplete_credentials(given: dict[str, str], variable: str) -> None:
    assert refused(config(), {**BASE, **given}).details == {"variable": variable}


def test_ca_bundle() -> None:
    settings = object_store_settings(config(), {**BASE, "AWS_CA_BUNDLE": "/etc/ssl/store.pem"})
    assert settings.ca_bundle == Path("/etc/ssl/store.pem")
    error = refused(config(), {**BASE, "AWS_CA_BUNDLE": "certs/store.pem"})
    assert error.details == {"variable": "AWS_CA_BUNDLE"}


def test_a_local_store_keeps_the_problem_without_secrets() -> None:
    env = {**BASE, "AWS_ENDPOINT_URL": "https://user:pw-in-url@objects.example.org"}
    env |= {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    choice = object_store(config(), env)
    assert choice.settings is None and choice.problem is not None
    assert choice.problem.details == {"variable": "AWS_ENDPOINT_URL"}
    for secret in SECRETS:
        assert secret not in repr(choice)

"""Static integrity tests for the bundled source-provider plugins.

The provider catalogues in ``srcmgr/bundled/providers`` are data, not code, so the
usual unit tests cannot catch a typo in them.  This module is the safety net: it loads
every provider through the real :class:`~srcmgr.core.plugins.PluginLoader` with
temporary ``SRCMGR_*`` directories and checks the invariants the project promises in
``docs/source-providers.md``:

* every provider loads (a single bad entry removes the whole provider),
* every entry has a key, a URL (or ``url_template``), a valid ``kind`` and a health URL,
* keys are unique per manager inside a provider and match ``[a-z0-9-]+``,
* ``url_template`` only uses the placeholders the manager plugins supply,
* health URLs are concrete (no unexpanded ``${...}``) because they are probed straight
  from the registry listing,
* ``scope`` matches the ecosystem's configuration location,
* China entries are recognised by the CLI's ``--china`` filter,
* example hosts under ``.invalid`` are marked private + ``allow_missing``,
* ``SourceManagerRuntime._expand_templates`` turns a template into a real URL with a
  fake ``template_values()`` and refuses to guess when a placeholder is unknown.

Nothing here touches the network: no provider URL is ever requested.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow `pytest tests/...` without an installed package
    sys.path.insert(0, str(ROOT))

from srcmgr.core.api import Scope, Source, SourceKind  # noqa: E402
from srcmgr.core.errors import SourceNotFound  # noqa: E402
from srcmgr.core.plugins import PluginLoader, source_from_dict  # noqa: E402
from srcmgr.core.registry import SourceRegistry  # noqa: E402
from srcmgr.core.runtime import SourceManagerRuntime  # noqa: E402

PROVIDERS_ROOT = ROOT / "srcmgr" / "bundled" / "providers"

#: Placeholders the manager plugins actually supply (see ``ManagerPlugin``
#: implementations' ``template_values()``; apt's is the reference).
KNOWN_PLACEHOLDERS = {"codename", "distro", "id", "id_like", "version_id", "arch", "suite"}

#: Managers whose configuration lives in /etc (or a system-wide installation).
SYSTEM_MANAGERS = {"apt", "docker", "flatpak", "snap"}

#: Every manager the provider registry is expected to cover.
EXPECTED_MANAGERS = (
    "apt",
    "flatpak",
    "docker",
    "npm",
    "yarn",
    "pnpm",
    "pip",
    "cargo",
    "go",
    "git",
    "maven",
    "rubygems",
    "conda",
)

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PLACEHOLDER_RE = re.compile(r"\$\{([a-z_]+)\}")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every SRCMGR_* root (and $HOME) at a temporary directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CONFIG", "DATA", "CACHE"):
        monkeypatch.setenv(f"SRCMGR_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.delenv("SRCMGR_PLUGIN_PATH", raising=False)
    return home


@pytest.fixture(scope="module")
def loaded() -> dict[str, Any]:
    """Every bundled provider, loaded through the real loader.

    The loader needs no writable state to *read* plugins, but ``user_plugins_dir()``
    is still consulted, so the environment is patched for the duration.
    """
    saved = {name: os.environ.get(name) for name in ("HOME", "SRCMGR_CONFIG_HOME", "SRCMGR_DATA_HOME", "SRCMGR_CACHE_HOME")}
    os.environ["HOME"] = "/nonexistent-srcmgr-home"
    os.environ["SRCMGR_CONFIG_HOME"] = "/nonexistent-srcmgr-config"
    os.environ["SRCMGR_DATA_HOME"] = "/nonexistent-srcmgr-data"
    os.environ["SRCMGR_CACHE_HOME"] = "/nonexistent-srcmgr-cache"
    try:
        yield PluginLoader().load_providers()
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _sources_by_provider(loaded: dict[str, Any]) -> dict[str, list[Source]]:
    out: dict[str, list[Source]] = {}
    for name, item in loaded.items():
        if item.provider is not None:
            out[name] = item.provider.list_sources()
    return out


def _templates(sources: list[Source]) -> list[tuple[Source, str]]:
    found = []
    for source in sources:
        template = str(source.extra.get("url_template") or "")
        if template:
            found.append((source, template))
    return found


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_provider_directories_are_discovered() -> None:
    directories = sorted(p.name for p in PROVIDERS_ROOT.iterdir() if p.is_dir())
    assert directories, "no provider directories found"
    for name in directories:
        assert (PROVIDERS_ROOT / name / "plugin.yaml").is_file(), f"{name}: missing plugin.yaml"
        assert (PROVIDERS_ROOT / name / "sources").is_dir(), f"{name}: missing sources/"


def test_every_provider_loads(loaded: dict[str, Any]) -> None:
    assert loaded, "the provider loader returned nothing"
    broken = {name: item.error for name, item in loaded.items() if not item.ok}
    assert broken == {}, f"providers failed to load: {broken}"
    for name, item in loaded.items():
        assert item.manifest.type.value == "provider"
        assert item.manifest.managers, f"{name}: providers must declare managers"
        assert item.provider.list_sources(), f"{name}: provider declares no sources"


def test_every_declared_manager_has_sources(loaded: dict[str, Any]) -> None:
    for name, item in loaded.items():
        served = {source.manager for source in item.provider.list_sources()}
        missing = set(item.manifest.managers) - served
        assert not missing, f"{name}: declares {sorted(missing)} but ships no sources for them"


def test_registry_covers_every_supported_manager(loaded: dict[str, Any]) -> None:
    registry = SourceRegistry(loaded)
    for manager in EXPECTED_MANAGERS:
        assert registry.sources_for(manager), f"{manager}: no registry sources"
        assert registry.groups_for(manager), f"{manager}: no provider groups"


# ---------------------------------------------------------------------------
# per-entry schema
# ---------------------------------------------------------------------------
def test_entries_have_key_url_kind_and_health(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            where = f"{provider}/{source.manager}/{source.provider_key}"
            assert source.provider_key, f"{where}: missing key"
            assert KEY_RE.match(source.provider_key), f"{where}: key must match [a-z0-9-]"
            assert source.name, f"{where}: missing name"
            assert source.url or source.extra.get("url_template"), f"{where}: missing url"
            if source.url:
                assert source.url.startswith(("http://", "https://", "sparse+http", "ppa:")), (
                    f"{where}: url {source.url!r} is not a usable scheme"
                )
            assert isinstance(source.kind, SourceKind), f"{where}: kind must be a SourceKind"
            assert source.kind is not SourceKind.UNKNOWN, f"{where}: kind must be explicit"
            health = source.health or {}
            assert health.get("url") or health.get("urls"), f"{where}: missing health url"
            assert source.description, f"{where}: missing description"


def test_keys_are_unique_per_manager_inside_a_provider(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        seen: set[tuple[str, str]] = set()
        for source in sources:
            key = (source.manager, source.provider_key)
            assert key not in seen, f"{provider}: duplicate entry {key}"
            seen.add(key)


def test_scope_matches_the_ecosystem(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            where = f"{provider}/{source.manager}/{source.provider_key}"
            if source.manager in SYSTEM_MANAGERS:
                assert source.scope is Scope.SYSTEM, f"{where}: must be scope system"
            else:
                assert source.scope is Scope.USER, f"{where}: must be scope user"


def test_priority_is_within_the_scoring_range(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            if source.priority is not None:
                assert 0 <= source.priority <= 100, f"{provider}/{source.provider_key}: priority"


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------
def test_url_templates_only_use_known_placeholders(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source, template in _templates(sources):
            used = set(PLACEHOLDER_RE.findall(template))
            unknown = used - KNOWN_PLACEHOLDERS
            assert not unknown, f"{provider}/{source.provider_key}: unknown placeholders {unknown}"
            assert used, f"{provider}/{source.provider_key}: template without placeholders"


def test_suite_placeholders_are_known(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            used = set(PLACEHOLDER_RE.findall(source.suite or ""))
            assert not (used - KNOWN_PLACEHOLDERS), (
                f"{provider}/{source.provider_key}: unknown suite placeholders {used}"
            )


def test_health_urls_are_concrete(loaded: dict[str, Any]) -> None:
    """Health URLs are probed unexpanded, so a ${...} there would always fail."""
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            health = source.health or {}
            urls = list(health.get("urls") or []) + ([health["url"]] if health.get("url") else [])
            for url in urls:
                assert "${" not in str(url), f"{provider}/{source.provider_key}: templated health url"
                assert str(url).startswith(("http://", "https://")), (
                    f"{provider}/{source.provider_key}: health url {url!r} is not HTTP(S)"
                )


def test_templated_entries_carry_a_concrete_fallback_url(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source, _template in _templates(sources):
            assert source.url.startswith("https://"), (
                f"{provider}/{source.provider_key}: needs a concrete url fallback"
            )


def test_apt_templated_fallback_is_the_ubuntu_tree(loaded: dict[str, Any]) -> None:
    """China/Europe/self-hosted APT entries fall back to /ubuntu/ (see the YAML notes)."""
    for provider, sources in _sources_by_provider(loaded).items():
        for source, template in _templates(sources):
            if source.manager != "apt":
                continue
            if "/${distro}/" not in template:
                continue
            assert source.url.endswith("/ubuntu/"), (
                f"{provider}/{source.provider_key}: fallback should be the Ubuntu tree, got {source.url}"
            )
            assert "debian-security" not in source.url


# ---------------------------------------------------------------------------
# source_from_dict integration
# ---------------------------------------------------------------------------
def test_top_level_url_template_reaches_extra() -> None:
    """The YAML relies on core folding unknown top-level keys into ``extra``."""
    source = source_from_dict(
        {
            "key": "tuna",
            "name": "TUNA",
            "url": "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/",
            "url_template": "https://mirrors.tuna.tsinghua.edu.cn/${distro}/",
            "countries": ["cn"],
        },
        manager="apt",
        provider="china-mirrors",
    )
    assert source.extra["url_template"] == "https://mirrors.tuna.tsinghua.edu.cn/${distro}/"
    assert source.extra["countries"] == ["cn"]


class _FakeAptPlugin:
    """Minimal stand-in for the apt manager plugin's ``template_values()``."""

    def __init__(self, **values: str) -> None:
        self._values = values

    def template_values(self) -> dict[str, str]:
        return dict(self._values)


class _NoTemplatePlugin:
    """A manager that does not implement templating at all."""


DEBIAN_VALUES = {
    "codename": "trixie",
    "distro": "debian",
    "id": "debian",
    "id_like": "",
    "version_id": "13",
    "arch": "amd64",
    "suite": "trixie",
}
UBUNTU_VALUES = {
    "codename": "resolute",
    "distro": "ubuntu",
    "id": "ubuntu",
    "id_like": "debian",
    "version_id": "26.04",
    "arch": "amd64",
    "suite": "resolute",
}


def _runtime(hermetic: Path) -> SourceManagerRuntime:
    return SourceManagerRuntime(command="pytest")


def _tuna_source(loaded: dict[str, Any], manager: str = "apt") -> Source:
    """A fresh, mutable copy of the ``tuna`` entry (the fixtures are module-scoped)."""
    source = loaded["china-mirrors"].provider.get_source("tuna")
    assert source is not None and source.manager == manager
    return dataclasses.replace(source, extra=dict(source.extra), health=dict(source.health))


def test_expand_templates_resolves_apt_templates(hermetic: Path, loaded: dict[str, Any]) -> None:
    runtime = _runtime(hermetic)
    for values, expected in (
        (DEBIAN_VALUES, "https://mirrors.tuna.tsinghua.edu.cn/debian/"),
        (UBUNTU_VALUES, "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/"),
    ):
        source = _tuna_source(loaded)
        expanded = runtime._expand_templates(_FakeAptPlugin(**values), source)
        assert expanded.url == expected
        assert "url_template" not in expanded.extra
        assert expanded.extra["value"] == expected  # single-value managers read this
        assert expanded.health["urls"], "health hints must survive expansion"


def test_expand_templates_resolves_the_suite_placeholder(
    hermetic: Path, loaded: dict[str, Any]
) -> None:
    security = loaded["china-mirrors"].provider.get_source("tuna-debian-security")
    assert security is not None
    assert security.suite == "${codename}-security"
    # The apt plugin expands `suite` itself (helpers.expand_template in _prepare);
    # emulate the documented substitution to prove the value is derivable.
    from srcmgr.core import helpers

    assert helpers.expand_template(security.suite, DEBIAN_VALUES) == "trixie-security"
    assert security.url == "https://mirrors.tuna.tsinghua.edu.cn/debian-security/"


def test_expand_templates_refuses_unknown_placeholders(hermetic: Path) -> None:
    runtime = _runtime(hermetic)
    source = source_from_dict(
        {
            "key": "broken",
            "name": "Broken",
            "url": "https://example.org/debian/",
            "url_template": "https://example.org/${nonsense}/",
        },
        manager="apt",
        provider="test",
    )
    with pytest.raises(SourceNotFound):
        runtime._expand_templates(_FakeAptPlugin(**DEBIAN_VALUES), source)


def test_expand_templates_without_manager_support_is_refused(
    hermetic: Path, loaded: dict[str, Any]
) -> None:
    """A manager that cannot supply values must not silently keep a literal ${...}."""
    runtime = _runtime(hermetic)
    source = _tuna_source(loaded)
    with pytest.raises(SourceNotFound):
        runtime._expand_templates(_NoTemplatePlugin(), source)


def test_static_urls_are_untouched_by_expansion(hermetic: Path, loaded: dict[str, Any]) -> None:
    runtime = _runtime(hermetic)
    security = loaded["china-mirrors"].provider.get_source("tuna-debian-security")
    assert security is not None
    expanded = runtime._expand_templates(_FakeAptPlugin(**DEBIAN_VALUES), security)
    assert expanded.url == "https://mirrors.tuna.tsinghua.edu.cn/debian-security/"


# ---------------------------------------------------------------------------
# China filtering and the example provider
# ---------------------------------------------------------------------------
def test_china_entries_are_recognised_by_the_cli_filter(loaded: dict[str, Any]) -> None:
    from srcmgr.cli.commands import _is_china

    for source in loaded["china-mirrors"].provider.list_sources():
        assert _is_china(source), f"china-mirrors/{source.provider_key} is not seen as China"
    # Official upstreams must not be mis-tagged as Chinese...
    for name in ("debian-official", "cloud-vendor"):
        for source in loaded[name].provider.list_sources():
            assert not _is_china(source), f"{name}/{source.provider_key} is wrongly tagged as China"
    # ... with Canonical's China CDN as the single deliberate exception.
    tagged = [
        source.provider_key
        for source in loaded["ubuntu-official"].provider.list_sources()
        if _is_china(source)
    ]
    assert tagged == ["ubuntu-cn"]


def test_invalid_hosts_are_private_and_allowed_to_be_missing(loaded: dict[str, Any]) -> None:
    for provider, sources in _sources_by_provider(loaded).items():
        for source in sources:
            host = source.url.split("//", 1)[-1].split("/", 1)[0]
            if host.endswith(".invalid"):
                where = f"{provider}/{source.provider_key}"
                assert source.kind is SourceKind.PRIVATE, f"{where}: .invalid hosts must be private"
                assert source.health.get("allow_missing") is True, (
                    f"{where}: .invalid hosts must set health.allow_missing"
                )


def test_corporate_template_is_fully_marked_as_example(loaded: dict[str, Any]) -> None:
    sources = loaded["corporate-template"].provider.list_sources()
    assert sources
    for source in sources:
        assert source.kind is SourceKind.PRIVATE
        assert source.health.get("allow_missing") is True
        assert ".invalid" in source.url

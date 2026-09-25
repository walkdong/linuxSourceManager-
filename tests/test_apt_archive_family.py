"""Derivative mapping for APT provider templates.

Mirrors host ``/ubuntu/`` and ``/debian/`` and nothing else, so ``${distro}`` in a
provider ``url_template`` must resolve to the *archive family* rather than to the local
``ID``.  Getting this wrong produces a plausible-looking URL that 404s on Mint, Pop!_OS
and friends; getting it *too* clever silently points Kali at Debian, which is worse.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from srcmgr.core.plugins import PluginLoader

CASES = {
    # id: (expected archive family, codename)
    "ubuntu": ("ubuntu", "resolute"),
    "debian": ("debian", "trixie"),
    "linuxmint": ("ubuntu", "wilma"),
    "pop": ("ubuntu", "jammy"),
    "elementary": ("ubuntu", "noble"),
    "lmde": ("debian", "faye"),
    "devuan": ("debian", "daedalus"),
    "pureos": ("debian", "crimson"),
    "kali": ("kali", "kali-rolling"),
    "raspbian": ("raspbian", "bookworm"),
    "parrot": ("parrot", "lorikeet"),
}

OS_RELEASE = {
    "ubuntu": "ID=ubuntu\nID_LIKE=debian\n",
    "debian": "ID=debian\n",
    "linuxmint": "ID=linuxmint\nID_LIKE=ubuntu debian\n",
    "pop": "ID=pop\nID_LIKE=ubuntu debian\n",
    "elementary": "ID=elementary\nID_LIKE=ubuntu\n",
    "lmde": "ID=lmde\nID_LIKE=debian\n",
    "devuan": "ID=devuan\nID_LIKE=debian\n",
    "pureos": "ID=pureos\nID_LIKE=debian\n",
    "kali": "ID=kali\nID_LIKE=debian\n",
    "raspbian": "ID=raspbian\nID_LIKE=debian\n",
    "parrot": "ID=parrot\nID_LIKE=debian\n",
}

DISTROS_WITH_OWN_ARCHIVE = {"kali", "raspbian", "parrot"}


def load_apt(monkeypatch, tmp_path: Path, distro: str, codename: str):
    apt_root = tmp_path / f"etc-apt-{distro}"
    (apt_root / "sources.list.d").mkdir(parents=True)
    release = apt_root / "os-release"
    release.write_text(
        textwrap.dedent(
            f"""\
            {OS_RELEASE[distro]}VERSION_ID="1"
            VERSION_CODENAME={codename}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SRCMGR_APT_ROOT", str(apt_root))
    monkeypatch.setenv("SRCMGR_APT_OS_RELEASE", str(release))
    loaded = PluginLoader().load_managers(only=["apt"])["apt"]
    assert loaded.ok, loaded.error
    return loaded.plugin


@pytest.mark.parametrize("distro", sorted(CASES))
def test_archive_family_matches_the_archive_that_exists(monkeypatch, tmp_path, distro):
    expected, codename = CASES[distro]
    plugin = load_apt(monkeypatch, tmp_path, distro, codename)
    values = plugin.template_values()
    assert values["codename"] == codename
    assert values["distro"] == expected
    # the raw identity stays available for providers that need it
    assert values["id"] == distro


@pytest.mark.parametrize("distro", sorted(DISTROS_WITH_OWN_ARCHIVE))
def test_systems_with_their_own_archive_are_not_remapped(monkeypatch, tmp_path, distro):
    expected, codename = CASES[distro]
    plugin = load_apt(monkeypatch, tmp_path, distro, codename)
    values = plugin.template_values()
    assert values["distro"] == distro, (
        f"{distro} runs its own archive; mapping it to {expected} would produce a wrong "
        "but plausible source URL"
    )


def test_template_expansion_produces_a_real_mirror_path(monkeypatch, tmp_path):
    """The provider templates end in /${distro}/ and must resolve to /ubuntu/ here."""
    from srcmgr.core.config import Config
    from srcmgr.core.runtime import SourceManagerRuntime

    plugin = load_apt(monkeypatch, tmp_path, "linuxmint", "wilma")
    rt = SourceManagerRuntime(config=Config.defaults())
    source = rt.registry.get("apt", "tuna")
    assert source is not None, "the China mirror provider should be loaded"
    expanded = rt._expand_templates(plugin, source)
    assert expanded.url.endswith("/ubuntu/"), expanded.url
    assert "${" not in expanded.url


def test_unknown_placeholder_is_refused(monkeypatch, tmp_path):
    from srcmgr.core.api import Source
    from srcmgr.core.config import Config
    from srcmgr.core.errors import SourceNotFound
    from srcmgr.core.runtime import SourceManagerRuntime

    plugin = load_apt(monkeypatch, tmp_path, "ubuntu", "resolute")
    rt = SourceManagerRuntime(config=Config.defaults())
    source = Source(
        manager="apt",
        name="Weird",
        url="https://mirror.example.org/${mystery}/",
        extra={"url_template": "https://mirror.example.org/${mystery}/"},
    )
    with pytest.raises(SourceNotFound) as excinfo:
        rt._expand_templates(plugin, source)
    assert "mystery" in str(excinfo.value)

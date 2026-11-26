#!/usr/bin/env python3
"""Builds the APT index this source serves, from one project's release plus
every stanza the live index already carries for everything else.

## Why a parser and not line editing

`Packages` is RFC822. The fields that matter here — `Description` above all —
carry continuation lines whose leading space is significant, and anything that
rewrites a stanza by matching lines eventually strips one. A description that
becomes a bare line makes apt reject the entire index, which reads as "the source
is broken" with no hint of which package caused it. `python-debian`'s `Deb822`
parses and re-emits a stanza without losing anything, so a package this run did
not build comes back byte-identical.

## Why the carry-forward exists

This branch is shared. `jzksnsjswkw/repo` serves more than one project, and each
project's workflow publishes the whole directory with `keep_files: false`. An
index built from only the current release's debs therefore deletes every other
project's rows — which is exactly what happened the first time this was a shell
script that knew nothing about anything it was not handed.

## What is per-project and what is not

Everything below the "project" comment is one project's declarations: what it
paints where, what it no longer publishes, what its debs are addressed as. Adding
a project means adding an entry to `PROJECTS`; nothing else in this file changes.

Usage:
    make_index.py --project pusher --tag v3.0.1 --out repo \\
                  [--previous live/Packages] [--debs a.deb b.deb]

Layout produced in --out:
    Packages, Packages.gz, Packages.bz2, Packages.xz, Packages.zst
    Release            (checksums of the above)
    index.html         (the landing page, rendered here)
    CydiaIcon*.png     (the source's own icon)
    <per-package icons>
    debs/              (mirrored dependencies)
"""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import lzma
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from debian.deb822 import Deb822
except ImportError:  # pragma: no cover - deployment-time dependency
    sys.exit("python-debian is required: pip install python-debian")


PAGES_URL = os.environ.get("PAGES_URL", "https://jzksnsjswkw.github.io/repo")

# Where this script lives. Icons, the depiction directory and the landing-page
# template are read from beside it, because the workflow checks out this
# repository's index branch and runs the script from there.
HERE = Path(__file__).resolve().parent

# --- project declarations --------------------------------------------------
#
# One entry per project that publishes into this source.
#
#   releases   the release download prefix the debs live under
#   icons      package name -> filename in `HERE`. Empty means "take whatever the
#              deb's own `Icon:` field names", which is the normal case: a project
#              that keeps its icon name stable needs no entry here.
#   obsolete   package names this project used to publish. A carried stanza with
#              one of these is a leftover that cannot be installed beside its
#              replacement, so it is dropped rather than forwarded.
#   depiction  package name -> depiction JSON, published under `depiction/`.
#
# `obsolete` is per project on purpose. A shared list would have one project's
# cleanup delete another project's current package.
PROJECTS = {
    "pusher": {
        "releases": "https://github.com/jzksnsjswkw/Pusher-rootless"
                    "/releases/download",
        "icons": {},
        "obsolete": {
            "com.jzksnsjswkw.pusher",
            "com.jzksnsjswkw.pusher-roothide",
        },
        "depiction": {
            "com.jzksnsjswkw.pusher-rootless": "pusher.json",
        },
    },
    "relocate": {
        "releases": "https://github.com/jzksnsjswkw/Relocate-rootless"
                    "/releases/download",
        "icons": {},
        "obsolete": {
            "com.jzksnsjswkw.relocate",
            "com.jzksnsjswkw.relocate-roothide",
        },
        "depiction": {
            "com.jzksnsjswkw.relocate-rootless": "relocate.json",
        },
    },
}

# The source's own description, shown by clients in the source list.
SOURCE_ICON = "CydiaIcon"
SOURCE_DESCRIPTION = "Notification forwarding and location overrides for iOS"

# Files that must exist before anything is published. A source that lists without
# its icon, or whose package pages render as empty tabs, looks fine in the JSON
# and broken in the client — so a missing one is an error, not a warning.
REQUIRED_SOURCE_FILES = [
    "CydiaIcon.png", "CydiaIcon@2x.png", "CydiaIcon@3x.png",
]


def control(deb: Path) -> Deb822:
    """The deb's own control stanza."""
    raw = subprocess.run(
        ["dpkg-deb", "-f", str(deb)], capture_output=True, check=True
    ).stdout
    return Deb822(raw.decode("utf-8"))


def field(stanza: Deb822, name: str, fallback: str = "") -> str:
    return stanza.get(name) or fallback


def served_name(deb: Path) -> str:
    """The name the file is served under.

    GitHub rewrites `~` to `.` in an uploaded asset's name. The `~` is dpkg's
    prerelease marker and must stay in `Version:`, where it is what sorts a beta
    before its release — but a filename cannot carry it, so the URL and the local
    path disagree for any prerelease and the index has to name the one the server
    uses.
    """
    return deb.name.replace("~", ".")


def collect_icon(icon: str, into: dict[str, Path]) -> None:
    """Records the icon file a stanza names, so it is copied into the tree.

    Only icons this source serves: a `file://` path resolves to something that
    only exists after installation, and an absolute URL belongs to whoever hosts
    it. A named file that is not beside this script is an error rather than a
    warning — the row would render with a placeholder and look like a mistake in
    the package.
    """
    if not icon or not icon.startswith(PAGES_URL):
        return
    name = icon.rsplit("/", 1)[-1]
    source = HERE / name
    if not source.is_file():
        sys.exit(f"error: {source} is missing — a package would list a broken icon")
    into[name] = source


def digest(data: bytes) -> tuple[str, str, str]:
    return (
        hashlib.md5(data).hexdigest(),
        hashlib.sha1(data).hexdigest(),
        hashlib.sha256(data).hexdigest(),
    )


def build(args: argparse.Namespace) -> int:
    project = PROJECTS[args.project]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    mirrored_dir = Path(args.mirrored)
    mirrored = sorted(mirrored_dir.glob("*.deb")) if mirrored_dir.is_dir() else []

    published: list[Deb822] = []
    icons: dict[str, Path] = {}
    seen: set[tuple[str, str]] = set()

    # --- this release's packages -------------------------------------------

    for path in [Path(p) for p in args.debs]:
        if not path.is_file():
            continue
        src = control(path)
        name = field(src, "Package")
        version = field(src, "Version")
        arch = field(src, "Architecture")
        if not name:
            print(f"skipping {path}: no Package field", file=sys.stderr)
            continue

        key = (name, arch)
        if key in seen:
            continue
        seen.add(key)

        stanza = Deb822()
        # Straight from the deb. Nothing is invented here: a project's two scheme
        # builds carry the same `Package:` and differ only in `Architecture`, and
        # a client keys a package on the pair — so Sileo lists one row and an
        # upgrade crosses schemes. A suffix added on the way out would disagree
        # with what dpkg recorded as installed.
        stanza["Package"] = name
        stanza["Name"] = field(src, "Name", name)
        stanza["Version"] = version
        if field(src, "Provides"):
            stanza["Provides"] = src["Provides"]
        stanza["Architecture"] = arch
        stanza["Section"] = field(src, "Section", "Tweaks")
        stanza["Priority"] = "optional"
        stanza["Maintainer"] = field(src, "Maintainer", "jzksnsjswkw")
        stanza["Installed-Size"] = field(src, "Installed-Size", "0")
        stanza["Depends"] = field(
            src, "Depends", "firmware (>= 15.0), mobilesubstrate"
        )

        icon = field(src, "Icon")
        if icon:
            stanza["Icon"] = icon
            collect_icon(icon, icons)

        if field(src, "Depiction"):
            stanza["Depiction"] = src["Depiction"]
        if field(src, "Author"):
            stanza["Author"] = src["Author"]
        if name in project["depiction"]:
            stanza["Sileodepiction"] = (
                f"{PAGES_URL}/depiction/{project['depiction'][name]}"
            )

        data = path.read_bytes()
        stanza["Filename"] = f"{project['releases']}/{args.tag}/{served_name(path)}"
        stanza["Size"] = str(len(data))
        md5, _, sha256 = digest(data)
        stanza["MD5sum"] = md5
        stanza["SHA256"] = sha256
        stanza["Description"] = field(
            src, "Description", "Push notifications to other devices"
        )
        published.append(stanza)

    # --- mirrored dependencies ---------------------------------------------

    for path in mirrored:
        if not path.is_file():
            continue
        src = control(path)
        name = field(src, "Package")
        arch = field(src, "Architecture")
        if not name or (name, arch) in seen:
            continue
        seen.add((name, arch))

        stanza = Deb822()
        for key in (
            "Package", "Name", "Version", "Architecture", "Maintainer", "Author",
            "Section", "Description", "Depends", "Installed-Size", "Provides",
        ):
            if field(src, key):
                stanza[key] = src[key]
        # Served from this source rather than from the upstream release: a client
        # installing the package that depends on it has added only this source,
        # and an unresolved dependency here stops the install half way.
        stanza["Filename"] = f"{PAGES_URL}/debs/{served_name(path)}"
        data = path.read_bytes()
        stanza["Size"] = str(len(data))
        md5, _, sha256 = digest(data)
        stanza["MD5sum"] = md5
        stanza["SHA256"] = sha256
        published.append(stanza)

    # --- stanzas this run did not produce ----------------------------------

    carried: list[Deb822] = []
    previous = Path(args.previous) if args.previous else None
    if previous and previous.is_file():
        obsolete = set().union(*(p["obsolete"] for p in PROJECTS.values()))
        for block in previous.read_text(encoding="utf-8").split("\n\n"):
            if not block.strip():
                continue
            stanza = Deb822(block)
            name = field(stanza, "Package")
            if not name:
                continue
            if name in obsolete:
                print(f"dropped obsolete {name} from the previous index")
                continue
            # Keyed on the pair, not the name. A release that builds only one
            # architecture must not delete the other one's row: that row is a real
            # package, still downloadable, that this run simply did not rebuild.
            if (name, field(stanza, "Architecture")) in seen:
                continue
            carried.append(stanza)

            # A carried stanza names its own icon, and the file has to travel with
            # it — a package this run did not build still renders a row, and a row
            # whose icon 404s is a placeholder. Collected here rather than only
            # from this release's debs, which is what left another project's icon
            # out of the tree the first time this ran.
            collect_icon(field(stanza, "Icon"), icons)

    # --- write --------------------------------------------------------------

    with open(out / "Packages", "wb") as handle:
        for stanza in published + carried:
            stanza.dump(handle, encoding="utf-8")
            handle.write(b"\n")

    index = (out / "Packages").read_bytes()
    artefacts: dict[str, bytes] = {"Packages": index}
    artefacts["Packages.gz"] = gzip.compress(index, 9)
    artefacts["Packages.bz2"] = bz2.compress(index, 9)
    artefacts["Packages.xz"] = lzma.compress(index, preset=9)
    for name, data in artefacts.items():
        if name != "Packages":
            (out / name).write_bytes(data)

    # zst is what a recent apt asks for first. Written only where zstd exists, and
    # listed in `Release` only when it was written: a checksum for a file that is
    # not there makes the client fail to fetch the index at all.
    if shutil.which("zstd"):
        target = out / "Packages.zst"
        result = subprocess.run(
            ["zstd", "-19", "-q", "-f", "-o", str(target)],
            input=index, capture_output=True,
        )
        if result.returncode == 0:
            artefacts["Packages.zst"] = target.read_bytes()
            target.unlink()
        else:
            print("warning: zstd failed; Packages.zst not written", file=sys.stderr)
    else:
        print("note: zstd not installed; Packages.zst not written", file=sys.stderr)

    release = [
        "Origin: jzksnsjswkw",
        "Label: jzksnsjswkw",
        "Suite: stable",
        "Version: 1.0",
        "Codename: ios",
        "Architectures: iphoneos-arm64 iphoneos-arm64e",
        "Components: main",
        f"Description: {SOURCE_DESCRIPTION}",
        f"Icon: file://{SOURCE_ICON}@2x.png",
    ]
    for label, fn in (
        ("MD5Sum", hashlib.md5),
        ("SHA1", hashlib.sha1),
        ("SHA256", hashlib.sha256),
    ):
        release.append(f"{label}:")
        for name in sorted(artefacts):
            data = artefacts[name]
            release.append(f" {fn(data).hexdigest()} {len(data)} {name}")
    (out / "Release").write_text("\n".join(release) + "\n", encoding="utf-8")

    # --- assets -------------------------------------------------------------

    for name in REQUIRED_SOURCE_FILES:
        source = HERE / name
        if not source.is_file():
            sys.exit(f"error: {source} is missing — the source will list without "
                     f"an icon")
        shutil.copy2(source, out / name)

    for name, source in icons.items():
        shutil.copy2(source, out / name)

    # Every project's depiction directory, copied in rather than linked to an
    # outside host, for the same reason the source icon is.
    depiction_target = out / "depiction"
    if depiction_target.exists():
        shutil.rmtree(depiction_target)
    for project in PROJECTS.values():
        for filename in project["depiction"].values():
            source = HERE / "depiction" / filename
            if not source.is_file():
                sys.exit(f"error: {source} is missing — a package page would "
                         f"render as an empty tab")
            shots = source.parent / "shots"
            if shots.is_dir():
                for shot in sorted(shots.glob("*.png")):
                    if not shot.is_file():
                        sys.exit(f"error: {shot} is missing — the package page "
                                 f"would show an empty frame")
    if (HERE / "depiction").is_dir():
        shutil.copytree(
            HERE / "depiction", depiction_target,
            ignore=shutil.ignore_patterns("banner.png"),
        )

    if mirrored:
        (out / "debs").mkdir(exist_ok=True)
        for path in mirrored:
            shutil.copy2(path, out / "debs" / served_name(path))

    # --- landing page -------------------------------------------------------

    template = HERE / "index.html"
    if template.is_file():
        rows = []
        listed: set[str] = set()
        for stanza in published + carried:
            name = stanza.get("Package", "")
            # One row per package: the two architectures of one build differ only
            # in where they install, and listing both reads as two plugins.
            if name in listed:
                continue
            listed.add(name)
            rows.append(
                '<li><p class="name">{}</p>'
                '<p class="desc">{}</p>'
                '<p class="meta">{} · {}</p></li>'.format(
                    stanza.get("Name", name),
                    stanza.get("Description", "").split("\n")[0],
                    stanza.get("Version", ""),
                    "roothide" if "arm64e" in stanza.get("Architecture", "")
                    else "rootless",
                )
            )
        html = template.read_text(encoding="utf-8")
        html = html.replace("<!--PACKAGE_COUNT-->", str(len(rows)))
        html = html.replace("<!--PACKAGE_ROWS-->", "\n".join(rows))
        (out / "index.html").write_text(html, encoding="utf-8")
    else:
        print("warning: index.html missing; the source URL will list files",
              file=sys.stderr)

    total = len(published) + len(carried)
    print(f"indexed {total} stanza(s) into {out}/")
    print(f"  {len(published)} from this release, {len(carried)} carried forward")
    for stanza in published:
        print(f"  + {stanza['Package']} {stanza['Version']} {stanza['Architecture']}")
    for stanza in carried:
        print(f"  · {stanza.get('Package')} {stanza.get('Version')} "
              f"{stanza.get('Architecture')} (carried)")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, choices=sorted(PROJECTS),
                    help="which project's release these debs belong to")
    ap.add_argument("--tag", required=True, help="release tag the debs belong to")
    ap.add_argument("--out", default="repo", help="directory to write the index into")
    ap.add_argument("--previous",
                    help="the live Packages file; other projects are carried from it")
    ap.add_argument("--debs", nargs="*", default=[], help="this release's debs")
    ap.add_argument("--mirrored", default="packages/mirrored",
                    help="debs of other projects this source serves (dependencies)")
    sys.exit(build(ap.parse_args()))


if __name__ == "__main__":
    main()

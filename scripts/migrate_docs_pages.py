"""Carry a project's prose across the template's documentation moves (#716, #717).

Run by copier's ``_migrations`` after an update.  When the template stops
rendering a page at one path and renders it at another, the update deletes
the old file and writes the new one; the project's ``DOMAIN-*`` block
content would go with the old file.  Copier requires a clean tree, so
``HEAD`` still holds every deleted page, and this script carries each block
from there into the same-named block of the new page.

Template script parks, implementation agent sorts:

- A moved page (``MOVES``): each ``DOMAIN-*`` block body is carried into the
  new page's block of the same name.  A block with no home parks the old
  page in place and says so.
- ``README.md``: the update conflicts for every project, because the
  generated table the old frame carried differs from the pristine render;
  the conflict is resolved to the template's side (the new frame), then the
  five positional ``DOMAIN-START``/``DOMAIN-END`` blocks of ``HEAD``'s README
  map by position to the named ``DOMAIN-README-*`` blocks (``README_BLOCKS``).
  A block still holding the scaffold's placeholder text is not carried.
- ``docs/tools/index.md`` and ``docs/prompts.md`` (#716): when they carry
  project content, the page is restored where it was, as a parked page.
  The agent applying the update moves each example into its tool's or
  prompt's ``DOMAIN-EXAMPLE-<name>`` slot and each piece of task guidance
  into a Use page, then deletes the parked page; the structure check
  reports E2 on it until then.  The update is not finished while a parked
  page exists.
- Links on the project's own pages that point at a moved page are rewritten
  to the new path, anchor kept, so the structure check does not report them;
  release notes, decision records and fenced code keep their old links, which
  the redirects serve.
- The ``GENERATED-NAV-TOOLS`` region of ``mkdocs.yml``: a conflict inside it
  is resolved to the template's side, since ``gen_reference.py`` rewrites it.

Idempotent: a block already carried, a page already restored, is left alone;
a project whose HEAD has no old page is a no-op.
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

# (old path, new path): the template renders the page at the new path now.
MOVES = (
    ("docs/configuration.md", "docs/reference/configuration.md"),
    ("docs/guides/security-model.md", "docs/security-model.md"),
    ("docs/installation.md", "docs/get-started/installation.md"),
    ("docs/deployment/claude-desktop.md", "docs/get-started/claude-desktop.md"),
    ("docs/deployment/docker.md", "docs/deploy/docker.md"),
    ("docs/guides/authentication.md", "docs/deploy/authentication.md"),
    ("docs/deployment/oidc.md", "docs/deploy/oidc.md"),
    ("docs/guides/authorization.md", "docs/deploy/authorization.md"),
)
PARKED = ("docs/tools/index.md", "docs/prompts.md")
README = "README.md"
# The old README's positional blocks, top to bottom, and their new names.
README_BLOCKS = (
    "DOMAIN-README-BADGES",
    "DOMAIN-README-PITCH",
    "DOMAIN-README-FIT",
    "DOMAIN-README-EXTRAS",
    "DOMAIN-README-DESIGN",
)
NEXT_STEPS = (
    "run `uv run python scripts/gen_reference.py` to write docs/reference/ from the code",
    "run `uv run python scripts/check_docs_structure.py`; an E2 on a parked page means "
    "its content still has to move into DOMAIN-EXAMPLE slots or Use pages, after "
    "which the parked page is deleted",
    "read README.md once: its blocks were carried by position, so check each sits "
    "under the heading it belongs to",
)

NAV_START = "GENERATED-NAV-TOOLS-START"
NAV_END = "GENERATED-NAV-TOOLS-END"
_CONFLICT = re.compile(
    r"^<<<<<<< before updating\n(?P<before>.*?)"
    r"(?:^\|\|\|\|\|\|\| last update\n(?P<base>.*?))?"
    r"^=======\n(?P<after>.*?)^>>>>>>> after updating\n",
    re.DOTALL | re.MULTILINE,
)
_BARE_BLOCK = re.compile(r"<!-- DOMAIN-START -->\n(.*?)<!-- DOMAIN-END -->", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_LINK = re.compile(r"\]\(([^)\s]+)\)")
# What the deleted pages' blocks held on a fresh render, comments stripped:
# a block still saying this carries nothing the project wrote.
_PLACEHOLDERS = frozenset(
    {
        "",
        '## ping\n\nHealth-check tool that returns `"pong"` if the service is alive.',
        "## Built-in prompts\n\n_None yet._",
    }
)
# Phrases only the old README frame's placeholder blocks contain.
_README_PLACEHOLDER_MARKS = ("[Capability 1]", "[Task 1]", "_Replace this placeholder")


def _head(root: Path, rel: str) -> str | None:
    """Return *rel* as committed at HEAD, or None when HEAD has no such file."""
    try:
        return subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def blocks(text: str) -> dict[str, str]:
    """Return ``DOMAIN-*`` block name -> body for *text* (named blocks only).

    A block opens on a line ``<!-- <NAME>-START ... -->`` and closes on the
    first later line that is exactly ``<!-- <NAME>-END -->``; the body is
    everything between, newline included.
    """
    found: dict[str, str] = {}
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not (
            line.startswith("<!-- DOMAIN-")
            and "-START" in line
            and line.rstrip().endswith("-->")
        ):
            continue
        name = line[len("<!-- ") : line.index("-START")]
        if name == "DOMAIN":
            continue  # a bare positional block; see positional_blocks
        end = f"<!-- {name}-END -->"
        body: list[str] = []
        while i < len(lines) and lines[i].rstrip("\n") != end:
            body.append(lines[i])
            i += 1
        if i < len(lines):
            found[name] = "".join(body)
            i += 1
    return found


def positional_blocks(text: str) -> list[str]:
    """Return the bodies of the bare ``DOMAIN-START``/``DOMAIN-END`` blocks, in order."""
    return _BARE_BLOCK.findall(text)


def _written(body: str) -> bool:
    bare = _COMMENT.sub("", body).strip()
    if bare in _PLACEHOLDERS:
        return False
    return not any(mark in bare for mark in _README_PLACEHOLDER_MARKS)


def has_project_content(text: str) -> bool:
    """True when a page carries text the project wrote.

    Text inside a ``DOMAIN-*`` block other than a placeholder counts; so does
    a page with no blocks at all, which the project rewrote wholesale.
    """
    found = blocks(text)
    if not found:
        return True
    return any(_written(body) for body in found.values())


def _replace_block(text: str, name: str, body: str) -> str:
    start = f"<!-- {name}-START"
    i = text.index(start)
    i = text.index("-->\n", i) + len("-->\n")
    j = text.index(f"<!-- {name}-END -->", i)
    return text[:i] + body + text[j:]


def transplant(old: str, new: str) -> tuple[str, list[str]]:
    """Copy each written ``DOMAIN-*`` block body of *old* into *new*.

    Returns the updated *new* and the names of blocks *old* has that *new*
    lacks, which cannot be carried automatically.
    """
    targets = blocks(new)
    missing = []
    for name, body in blocks(old).items():
        if not _written(body):
            continue
        if name not in targets:
            missing.append(name)
            continue
        new = _replace_block(new, name, body)
    return new, missing


def transplant_readme(old: str, new: str) -> tuple[str, list[str], list[str]]:
    """Carry the old README's positional blocks into the new named ones.

    Returns the updated README, the positions (1-based) that had content but
    no named block to land in, and the positions skipped as the scaffold's
    placeholder text.
    """
    targets = blocks(new)
    missing: list[str] = []
    skipped: list[str] = []
    for index, body in enumerate(positional_blocks(old)):
        if not _written(body):
            if _COMMENT.sub("", body).strip():
                skipped.append(str(index + 1))
            continue
        if index >= len(README_BLOCKS) or README_BLOCKS[index] not in targets:
            missing.append(str(index + 1))
            continue
        new = _replace_block(new, README_BLOCKS[index], body)
    return new, missing, skipped


def take_template_side(text: str) -> str:
    """Resolve every conflict in *text* to the "after updating" side."""
    return _CONFLICT.sub(lambda m: m.group("after"), text)


def resolve_nav_region(text: str) -> str:
    """Take the template's side of any conflict inside the nav tools region.

    A conflict counts as inside the region when the text before it sits
    between the region's markers, or when the after side carries the
    markers itself (the template moved or reworded the region).
    """

    def choose(match: re.Match[str]) -> str:
        prefix = text[: match.start()]
        inside = prefix.rfind(NAV_START) > prefix.rfind(NAV_END)
        after = match.group("after")
        if inside or NAV_START in after:
            return after
        return match.group(0)

    return _CONFLICT.sub(choose, text)


def _carry(root: Path, old_rel: str, new_rel: str, notes: list[str]) -> None:
    old = _head(root, old_rel)
    new_path = root / new_rel
    if old is None or not new_path.exists() or (root / old_rel).exists():
        return
    current = new_path.read_text(encoding="utf-8")
    updated, missing = transplant(old, current)
    if updated != current:
        new_path.write_text(updated, encoding="utf-8")
        notes.append(f"carried the DOMAIN blocks of {old_rel} into {new_rel}")
    if missing:
        (root / old_rel).parent.mkdir(parents=True, exist_ok=True)
        (root / old_rel).write_text(old, encoding="utf-8")
        notes.append(
            f"parked {old_rel}: its block(s) {', '.join(missing)} have no home in "
            f"{new_rel}; move the content, then delete the parked page"
        )


def _carry_readme(root: Path, notes: list[str]) -> None:
    old = _head(root, README)
    path = root / README
    if old is None or not path.exists() or not positional_blocks(old):
        return
    current = path.read_text(encoding="utf-8")
    resolved = take_template_side(current)
    if resolved != current:
        notes.append(
            "resolved README.md's update conflict to the template's new frame; "
            "text written outside the old DOMAIN blocks is dropped, so compare "
            "with `git show HEAD:README.md`"
        )
    if positional_blocks(resolved):
        if resolved != current:
            path.write_text(resolved, encoding="utf-8")
        return  # the README still has the old frame; nothing to map into
    updated, missing, skipped = transplant_readme(old, resolved)
    if updated == current:
        return  # already carried on an earlier run
    path.write_text(updated, encoding="utf-8")
    if updated != resolved:
        notes.append(
            "carried README.md's positional DOMAIN blocks into the named blocks"
        )
    if skipped:
        notes.append(
            f"README.md block(s) {', '.join(skipped)} (counted from the top of the old "
            "file) still held the scaffold's placeholder and were not carried"
        )
    if missing:
        notes.append(
            f"README.md block(s) {', '.join(missing)} (counted from the top of the old "
            "file) had content but no named block to land in; recover the text with "
            "`git show HEAD:README.md` and place it"
        )


def _park(root: Path, rel: str, notes: list[str]) -> None:
    old = _head(root, rel)
    if old is None or (root / rel).exists() or not has_project_content(old):
        return
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(old, encoding="utf-8")
    notes.append(
        f"parked {rel}: move its examples into the DOMAIN-EXAMPLE slots under "
        "docs/reference/ and its task guidance into docs/use/, then delete it"
    )


# Pages whose links are a historical record: the redirect serves them.
_HISTORY = ("releases/", "decisions/", "design/", "superpowers/")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)


def rewrite_links(text: str, page_rel: str) -> str:
    """Point links on *page_rel* (docs-relative) at the new path of a moved page.

    Fenced code is left as it is.
    """
    moved = {old.removeprefix("docs/"): new.removeprefix("docs/") for old, new in MOVES}
    here = posixpath.dirname(page_rel)

    def fix(match: re.Match[str]) -> str:
        target = match.group(1)
        path, sep, rest = target.partition("#")
        if not sep:
            path, sep, rest = target.partition("?")
        if not path.endswith(".md") or "://" in path:
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(here, path))
        if resolved not in moved:
            return match.group(0)
        new_target = posixpath.relpath(moved[resolved], here or ".")
        return f"]({new_target}{sep}{rest})"

    out: list[str] = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        out.append(line if fenced else _LINK.sub(fix, line))
    return "".join(out)


def _rewrite_project_links(root: Path, notes: list[str]) -> None:
    docs = (root / "docs").resolve()
    if not docs.is_dir():
        return
    changed = []
    for rel in sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*.md")):
        if rel.startswith(_HISTORY):
            continue
        page = (docs / rel).resolve()
        if not str(page).startswith(str(docs) + os.sep):
            continue  # a symlink pointing outside docs/ is not ours to rewrite
        with page.open(encoding="utf-8") as handle:
            text = handle.read()
        updated = rewrite_links(text, rel)
        if updated != text:
            with page.open("w", encoding="utf-8") as handle:
                handle.write(updated)
            changed.append(f"docs/{rel}")
    if changed:
        notes.append(f"pointed links at moved pages on: {', '.join(changed)}")


def migrate(root: Path) -> list[str]:
    """Apply the migration under *root*; return the lines to print."""
    notes: list[str] = []
    for old_rel, new_rel in MOVES:
        _carry(root, old_rel, new_rel, notes)
    _carry_readme(root, notes)
    mkdocs = root / "mkdocs.yml"
    if mkdocs.exists():
        before = mkdocs.read_text(encoding="utf-8")
        after = resolve_nav_region(before)
        if after != before:
            mkdocs.write_text(after, encoding="utf-8")
            notes.append(
                "resolved the conflict in mkdocs.yml's GENERATED-NAV-TOOLS region to "
                "the template's side; gen_reference.py rewrites it"
            )
    for rel in PARKED:
        _park(root, rel, notes)
    _rewrite_project_links(root, notes)
    return notes


def main() -> int:
    root = Path.cwd()
    notes = migrate(root)
    for note in notes:
        print(f"migrate_docs_pages: {note}")
    if notes:
        for step in NEXT_STEPS:
            print(f"migrate_docs_pages: next, {step}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

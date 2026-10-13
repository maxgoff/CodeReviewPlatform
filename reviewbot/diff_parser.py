"""Parse a unified git diff and render it in a line-numbered form the model can cite reliably.

LLMs are bad at counting lines. Instead of asking the model to work out line numbers from
hunk headers, we print the new-file line number in a gutter next to every line and ask it to
copy that number. The parser also tells us which lines GitHub will accept inline comments on.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    kind: str  # "+", "-", or " "
    text: str
    old_no: int | None
    new_no: int | None


@dataclass
class Hunk:
    header: str
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileChange:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added | deleted | modified | renamed
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.kind == "+" and ln.new_no}

    @property
    def commentable_lines(self) -> set[int]:
        """New-side lines that appear in the diff (added or context). GitHub rejects comments elsewhere."""
        return {ln.new_no for h in self.hunks for ln in h.lines if ln.kind in "+ " and ln.new_no}

    @property
    def changed_line_count(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind in "+-")

    def line_text(self, new_no: int) -> str | None:
        for h in self.hunks:
            for ln in h.lines:
                if ln.new_no == new_no and ln.kind != "-":
                    return ln.text
        return None


def parse_diff(diff_text: str) -> list[FileChange]:
    files: list[FileChange] = []
    current: FileChange | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            parts = raw.split(" b/", 1)
            path = parts[1] if len(parts) == 2 else raw.split()[-1]
            old = raw[len("diff --git a/"):].split(" b/", 1)[0]
            current = FileChange(path=path, old_path=old)
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue

        m = HUNK_RE.match(raw)
        if m:
            old_no, new_no = int(m.group(1)), int(m.group(3))
            hunk = Hunk(header=raw)
            current.hunks.append(hunk)
            continue

        if hunk is None:
            # File-level metadata between "diff --git" and the first hunk.
            if raw.startswith("new file mode"):
                current.status = "added"
            elif raw.startswith("deleted file mode"):
                current.status = "deleted"
            elif raw.startswith("rename from "):
                current.status = "renamed"
                current.old_path = raw[len("rename from "):]
            elif raw.startswith("rename to "):
                current.path = raw[len("rename to "):]
            elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                current.is_binary = True
            elif raw.startswith("+++ "):
                target = raw[4:].strip()
                if target != "/dev/null":
                    current.path = target[2:] if target.startswith("b/") else target
            continue

        # Inside a hunk. Some tools strip the single space from blank context lines.
        kind, text = (raw[0], raw[1:]) if raw else (" ", "")
        if kind == "\\":  # "\ No newline at end of file"
            continue
        if kind not in "+- ":
            continue
        if kind == "+":
            hunk.lines.append(DiffLine("+", text, None, new_no))
            new_no += 1
        elif kind == "-":
            hunk.lines.append(DiffLine("-", text, old_no, None))
            old_no += 1
        else:
            hunk.lines.append(DiffLine(" ", text, old_no, new_no))
            old_no += 1
            new_no += 1

    return files


def should_skip(change: FileChange, skip_globs: list[str], max_file_lines: int) -> str | None:
    """Return a reason to skip this file, or None to review it."""
    if change.is_binary:
        return "binary file"
    if change.status == "deleted":
        return "file deleted"
    name = change.path.rsplit("/", 1)[-1]
    for pattern in skip_globs:
        if fnmatch.fnmatch(change.path, pattern) or fnmatch.fnmatch(name, pattern):
            return f"matches skip pattern {pattern!r}"
    if not change.added_lines:
        return "no added lines"
    if change.changed_line_count > max_file_lines:
        return f"too large ({change.changed_line_count} changed lines)"
    return None


def render_for_prompt(change: FileChange) -> str:
    """Render one file as a numbered diff:  `  42 + code` / `  -- - removed`."""
    out = [f"### File: {change.path} ({change.status})"]
    for h in change.hunks:
        out.append(h.header)
        for ln in h.lines:
            gutter = f"{ln.new_no:>5}" if ln.new_no is not None else "   --"
            out.append(f"{gutter} {ln.kind} {ln.text}")
    return "\n".join(out)


def chunk_files(changes: list[FileChange], char_budget: int) -> list[list[FileChange]]:
    """Group files into batches that fit the prompt budget. Large PRs become several requests."""
    chunks: list[list[FileChange]] = []
    current: list[FileChange] = []
    size = 0
    for change in changes:
        cost = len(render_for_prompt(change))
        if current and size + cost > char_budget:
            chunks.append(current)
            current, size = [], 0
        current.append(change)
        size += cost
    if current:
        chunks.append(current)
    return chunks

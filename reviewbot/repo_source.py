"""Load a whole repository for review: from GitHub (tarball download) or from a local directory.

The tarball is read in memory and never extracted to disk, so a hostile archive can't write
outside a temp folder, plant symlinks or fill the disk. Each file becomes a FileChange whose
lines are all "added", which lets the existing pipeline review whole files unchanged.
"""

from __future__ import annotations

import fnmatch
import io
import os
import re
import tarfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import httpx

from .diff_parser import DiffLine, FileChange, Hunk

# Files worth reviewing by default. Pass --include to review anything else.
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java", ".kt", ".kts", ".scala",
    ".rb", ".php", ".cs", ".rs", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm",
    ".sh", ".bash", ".ps1", ".sql", ".tf", ".hcl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".vue", ".svelte", ".lua", ".pl", ".r", ".dart", ".ex", ".exs", ".erl", ".clj", ".groovy",
}
CODE_FILENAMES = {"Dockerfile", "Makefile", "Jenkinsfile", "Procfile", "Gemfile", "Rakefile"}
SKIP_DIRS = {
    ".git", "node_modules", "vendor", "third_party", "dist", "build", "out", "target",
    ".venv", "venv", "env", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".next",
    ".idea", ".vscode", "coverage", "site-packages", "bower_components",
}

OWNER_REPO = r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9_.-]{1,100}?)"
_URL_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/{OWNER_REPO}(?:\.git)?(?:/tree/(?P<ref>[^/]+)(?:/(?P<path>.+?))?)?/?$"
)
_SSH_RE = re.compile(rf"^git@github\.com:{OWNER_REPO}(?:\.git)?$")
_SHORT_RE = re.compile(rf"^{OWNER_REPO}(?:@(?P<ref>[^\s]+))?$")


class RepoError(Exception):
    """A problem the user can fix: bad spec, repo not found, too many files, over budget."""


@dataclass
class RepoSpec:
    owner: str
    repo: str
    ref: str | None = None
    subpath: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_repo_spec(spec: str, ref: str | None = None, path: str | None = None) -> RepoSpec:
    """Accepts owner/repo, owner/repo@ref, https://github.com/owner/repo[/tree/ref/path], git@github.com:owner/repo.git"""
    spec = spec.strip()
    for pattern in (_URL_RE, _SSH_RE, _SHORT_RE):
        m = pattern.match(spec)
        if m:
            groups = m.groupdict()
            return RepoSpec(
                owner=groups["owner"],
                repo=groups["repo"],
                ref=ref or groups.get("ref"),
                subpath=_clean_subpath(path or groups.get("path")),
            )
    raise RepoError(f"Not a GitHub repository: {spec!r}. Use owner/repo, owner/repo@branch or a github.com URL.")


def _clean_subpath(path: str | None) -> str | None:
    if not path:
        return None
    clean = str(PurePosixPath(path.strip("/")))
    if clean.startswith("..") or "/../" in f"/{clean}/":
        raise RepoError(f"Invalid path: {path!r}")
    return None if clean in ("", ".") else clean


@dataclass
class FileFilter:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_file_bytes: int = 200_000

    def reason_to_skip(self, path: str, size: int) -> str | None:
        parts = PurePosixPath(path).parts
        name = parts[-1]
        if any(p in SKIP_DIRS for p in parts[:-1]):
            return "dependency, build or tooling directory"
        if any(_match(path, name, g) for g in self.exclude):
            return "excluded by --exclude"
        if self.include:
            if not any(_match(path, name, g) for g in self.include):
                return "not matched by --include"
        elif PurePosixPath(name).suffix.lower() not in CODE_EXTENSIONS and name not in CODE_FILENAMES:
            return "not a source file type"
        if size > self.max_file_bytes:
            return f"larger than {self.max_file_bytes // 1000} KB"
        return None


def _match(path: str, name: str, glob: str) -> bool:
    return fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(name, glob)


@dataclass
class SourceFile:
    path: str
    text: str


@dataclass
class RepoSnapshot:
    label: str  # "owner/repo@sha" or a local path
    commit: str | None
    files: list[SourceFile] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    repo_full_name: str | None = None  # set for GitHub sources, used for links in the report

    def top_directories(self, n: int = 8) -> list[tuple[str, int]]:
        counts = Counter(f.path.split("/", 1)[0] if "/" in f.path else "(root)" for f in self.files)
        return counts.most_common(n)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _add(snapshot: RepoSnapshot, path: str, size: int, read, flt: FileFilter, subpath: str | None) -> None:
    if subpath and not (path == subpath or path.startswith(subpath + "/")):
        return
    reason = flt.reason_to_skip(path, size)
    if reason:
        snapshot.skipped[reason] += 1
        return
    text = _decode(read())
    if text is None:
        snapshot.skipped["binary or not UTF-8"] += 1
    elif not text.strip():
        snapshot.skipped["empty"] += 1
    else:
        snapshot.files.append(SourceFile(path, text))


def read_tarball(data: bytes, label: str, commit: str | None, flt: FileFilter, subpath: str | None = None) -> RepoSnapshot:
    snapshot = RepoSnapshot(label=label, commit=commit)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():  # skips dirs, symlinks, hardlinks, devices
                continue
            parts = PurePosixPath(member.name).parts
            if len(parts) < 2 or any(p in ("..", "") for p in parts) or member.name.startswith("/"):
                continue
            path = "/".join(parts[1:])  # drop GitHub's "owner-repo-sha/" prefix
            _add(snapshot, path, member.size, lambda m=member: tar.extractfile(m).read(), flt, subpath)
    snapshot.files.sort(key=lambda f: f.path)
    return snapshot


def read_directory(root: str | Path, flt: FileFilter, subpath: str | None = None) -> RepoSnapshot:
    root = Path(root).resolve()
    if not root.is_dir():
        raise RepoError(f"Not a directory: {root}")
    snapshot = RepoSnapshot(label=root.name, commit=None)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not Path(dirpath, d).is_symlink())
        for name in sorted(filenames):
            full = Path(dirpath, name)
            if full.is_symlink() or not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            _add(snapshot, rel, full.stat().st_size, full.read_bytes, flt, subpath)
    snapshot.files.sort(key=lambda f: f.path)
    return snapshot


class GitHubRepoFetcher:
    """Downloads a repository snapshot through the GitHub REST API. Works for private repos with a token."""

    def __init__(self, token: str | None, api_url: str = "https://api.github.com",
                 http: httpx.AsyncClient | None = None, max_download_mb: int = 200):
        self._http = http or httpx.AsyncClient(timeout=60.0)
        self._api = api_url.rstrip("/")
        self._headers = {"X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reviewbot"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._max_bytes = max_download_mb * 1024 * 1024

    async def _get(self, url: str, accept: str) -> httpx.Response:
        resp = await self._http.get(url, headers={**self._headers, "Accept": accept})
        if resp.status_code in (401, 403, 404):
            hint = "" if "Authorization" in self._headers else " If it is private, set GITHUB_TOKEN."
            raise RepoError(f"GitHub returned {resp.status_code} for {url}.{hint}")
        resp.raise_for_status()
        return resp

    async def resolve_commit(self, spec: RepoSpec) -> str:
        ref = spec.ref
        if not ref:
            info = await self._get(f"{self._api}/repos/{spec.full_name}", "application/vnd.github+json")
            ref = info.json()["default_branch"]
        resp = await self._get(f"{self._api}/repos/{spec.full_name}/commits/{ref}", "application/vnd.github.sha")
        return resp.text.strip()

    async def download_tarball(self, spec: RepoSpec, sha: str) -> bytes:
        url = f"{self._api}/repos/{spec.full_name}/tarball/{sha}"
        headers = {**self._headers, "Accept": "application/vnd.github+json"}
        buf = bytearray()
        async with self._http.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            if resp.status_code in (401, 403, 404):
                raise RepoError(f"GitHub returned {resp.status_code} downloading {spec.full_name}.")
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > self._max_bytes:
                    raise RepoError(f"{spec.full_name} is larger than {self._max_bytes // 2**20} MB compressed.")
        return bytes(buf)

    async def fetch(self, spec: RepoSpec, flt: FileFilter) -> RepoSnapshot:
        sha = await self.resolve_commit(spec)  # pin the snapshot to one commit
        data = await self.download_tarball(spec, sha)
        snapshot = read_tarball(data, f"{spec.full_name}@{sha[:7]}", sha, flt, spec.subpath)
        snapshot.repo_full_name = spec.full_name
        if spec.subpath:
            snapshot.label += f" ({spec.subpath}/)"
        return snapshot


def to_changes(snapshot: RepoSnapshot) -> list[FileChange]:
    """Present each whole file as an added file so every line is reviewable and commentable."""
    changes = []
    for f in snapshot.files:
        lines = f.text.splitlines()
        hunk = Hunk(header=f"@@ -0,0 +1,{len(lines)} @@",
                    lines=[DiffLine("+", text, None, i) for i, text in enumerate(lines, start=1)])
        changes.append(FileChange(path=f.path, status="added", hunks=[hunk]))
    return changes


async def load_snapshot(source: str, flt: FileFilter, *, ref: str | None = None, path: str | None = None,
                        fetcher: GitHubRepoFetcher | None = None) -> RepoSnapshot:
    """`source` is a local directory or anything parse_repo_spec accepts."""
    if Path(source).expanduser().is_dir():
        return read_directory(Path(source).expanduser(), flt, _clean_subpath(path))
    spec = parse_repo_spec(source, ref=ref, path=path)
    if fetcher is None:
        raise RepoError("No GitHub fetcher configured.")
    return await fetcher.fetch(spec, flt)

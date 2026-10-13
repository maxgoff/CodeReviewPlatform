"""Turn a ReviewResult into GitHub review comments and a Markdown summary."""

from __future__ import annotations

from .models import SEVERITY_ICON, Finding, ReviewResult, Severity

MARKER = "<!-- reviewbot -->"


def comment_body(f: Finding) -> str:
    lines = [
        f"**{SEVERITY_ICON[f.severity]} {f.severity.value.title()} · {f.category.value}**: {f.title}",
        "",
        f.explanation,
    ]
    if f.suggestion.strip():
        # GitHub "suggestion" blocks replace the commented line, so only use one for single-line fixes.
        fence = "suggestion" if "\n" not in f.suggestion.strip() else ""
        lines += ["", f"```{fence}", f.suggestion.rstrip(), "```"]
    lines += ["", f"<sub>Flagged by {', '.join(f.reviewers)} · confidence {f.confidence:.2f}</sub>"]
    return "\n".join(lines)


def summary_markdown(result: ReviewResult) -> str:
    counts = result.counts_by_severity()
    total = sum(counts.values())
    out = [MARKER, "## 🤖 ReviewBot summary", ""]
    if total == 0:
        out.append("No issues above the confidence threshold. Nice work.")
    else:
        out.append("| " + " | ".join(f"{SEVERITY_ICON[Severity(s)]} {s}" for s in counts) + " |")
        out.append("|" + "---|" * len(counts))
        out.append("| " + " | ".join(str(n) for n in counts.values()) + " |")
    if result.overflow:
        out += ["", f"<details><summary>{len(result.overflow)} more findings (not posted inline)</summary>", ""]
        out += [f"- `{f.path}:{f.line}` {f.severity.value}: {f.title}" for f in result.overflow]
        out += ["", "</details>"]
    if result.files_skipped:
        out += ["", "<details><summary>Skipped files</summary>", ""]
        out += [f"- `{p}`: {why}" for p, why in result.files_skipped.items()]
        out += ["", "</details>"]
    if result.errors:
        out += ["", "⚠️ Some reviewers failed, so this review may be incomplete:"]
        out += [f"- {e}" for e in result.errors]
    cost = result.usage.estimated_cost_usd(result.model)
    meta = f"model `{result.model}` · {result.usage.calls} calls · {result.usage.input_tokens:,} in / " \
           f"{result.usage.output_tokens:,} out tokens · cache read {result.usage.cache_read_input_tokens:,}"
    if cost is not None:
        meta += f" · est. ${cost:.4f}"
    out += ["", f"<sub>{meta}</sub>"]
    return "\n".join(out)


def terminal_report(result: ReviewResult) -> str:
    out = []
    for f in result.findings + result.overflow:
        out.append(f"{SEVERITY_ICON[f.severity]} {f.severity.value.upper():8} {f.path}:{f.line}  {f.title}")
        out.append(f"   {f.explanation}")
        out.append(f"   [{', '.join(f.reviewers)} · confidence {f.confidence:.2f}]")
    if not out:
        out.append("No findings above the confidence threshold.")
    out.append("")
    out.append(f"Reviewed: {', '.join(result.files_reviewed) or '-'}")
    for p, why in result.files_skipped.items():
        out.append(f"Skipped:  {p} ({why})")
    if result.dropped:
        out.append("Dropped:  " + ", ".join(f"{k}={v}" for k, v in result.dropped.items()))
    for e in result.errors:
        out.append(f"Error:    {e}")
    out.append(
        f"Usage:    {result.usage.calls} calls, {result.usage.input_tokens:,} in, "
        f"{result.usage.output_tokens:,} out, cache read {result.usage.cache_read_input_tokens:,}"
    )
    return "\n".join(out)


def repo_markdown(
    result: ReviewResult,
    *,
    label: str,
    commit: str | None = None,
    repo_full_name: str | None = None,
    source_skipped: dict[str, int] | None = None,
    generated_at: str | None = None,
) -> str:
    """Standalone Markdown report for a whole-repository review."""

    def where(f: Finding) -> str:
        loc = f"{f.path}:{f.line}"
        if repo_full_name and commit:
            return f"[`{loc}`](https://github.com/{repo_full_name}/blob/{commit}/{f.path}#L{f.line})"
        return f"`{loc}`"

    findings = result.findings + result.overflow
    counts = result.counts_by_severity()
    out = [f"# ReviewBot repository review: {label}", ""]
    meta = [f"model `{result.model}`", f"{len(result.files_reviewed)} files reviewed", f"{len(findings)} findings"]
    if commit:
        meta.insert(0, f"commit `{commit[:12]}`")
    if generated_at:
        meta.append(generated_at)
    out += [" · ".join(meta), ""]
    out.append("| " + " | ".join(f"{SEVERITY_ICON[Severity(s)]} {s}" for s in counts) + " |")
    out.append("|" + "---|" * len(counts))
    out.append("| " + " | ".join(str(n) for n in counts.values()) + " |")

    if result.errors:
        out += ["", "> ⚠️ Some review calls failed, so this report may be incomplete:"]
        out += [f"> - {e}" for e in result.errors]

    out += ["", "## Findings"]
    if not findings:
        out += ["", "No issues above the confidence threshold."]
    for sev in Severity:
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        out += ["", f"### {SEVERITY_ICON[sev]} {sev.value.title()} ({len(group)})", ""]
        for f in sorted(group, key=lambda x: (x.path, x.line)):
            out.append(f"- **{f.title}** · {where(f)} · {f.category.value}")
            out.append(f"  {f.explanation}")
            if f.suggestion.strip():
                out += ["  ```", *("  " + ln for ln in f.suggestion.rstrip().splitlines()), "  ```"]
            out.append(f"  <sub>{', '.join(f.reviewers)} · confidence {f.confidence:.2f}</sub>")

    if findings:
        per_file: dict[str, int] = {}
        for f in findings:
            per_file[f.path] = per_file.get(f.path, 0) + 1
        top = sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        out += ["", "## Files with the most findings", "", "| File | Findings |", "|---|---|"]
        out += [f"| `{p}` | {n} |" for p, n in top]

    out += ["", "## Coverage", "", f"- Reviewed: {len(result.files_reviewed)} files"]
    skipped_reasons: dict[str, int] = dict(source_skipped or {})
    for why in result.files_skipped.values():
        key = "too large" if why.startswith("too large") else why
        skipped_reasons[key] = skipped_reasons.get(key, 0) + 1
    for why, n in sorted(skipped_reasons.items(), key=lambda kv: -kv[1]):
        out.append(f"- Skipped {n}: {why}")
    if result.dropped:
        out.append("- Filtered findings: " + ", ".join(f"{k} {v}" for k, v in result.dropped.items()))

    u = result.usage
    cost = u.estimated_cost_usd(result.model)
    out += ["", "## Usage", "",
            f"{u.calls} model calls · {u.input_tokens:,} input tokens · {u.output_tokens:,} output tokens · "
            f"cache write {u.cache_creation_input_tokens:,} · cache read {u.cache_read_input_tokens:,}"
            + (f" · est. ${cost:.2f}" if cost is not None else "")]
    return "\n".join(out) + "\n"

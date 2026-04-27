"""Shared rich Console singletons and rendering components for the CLI.

UX-DR6 ErrorDisplay — `render_error(title, hint)`: red-bordered Panel on stderr.
UX-DR7 ConfirmationLine — `render_confirmation(message)`: plain single line on stdout.
UX-DR1 KeyPanel — `render_key_panel(issued)`: double-border header + naked
    plaintext line (UX-DR17 copy-paste fidelity) + double-border context panel.
UX-DR9 Shared Console — singletons avoid terminal redetection between calls.
NO_COLOR compliance — rich respects the env var automatically. We do NOT set
`force_terminal=True` here — that would defeat NO_COLOR.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from rich import box
from rich.console import Console
from rich.markup import escape as _rich_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from compadre.counters import AggregatedCacheStatus
from compadre.keys.formatters import format_seconds_ago
from compadre.keys.health import HealthCheck, HealthReport
from compadre.keys.models import (
    ApiKeyListRow,
    AuditEventRow,
    BillingMode,
    CustomerDetailRow,
    CustomerListRow,
    CustomerStatus,
    IssuedKey,
    KeyDetailRow,
    Last24hStats,
    RevenueReport,
)

_stdout_console: Console | None = None
_stderr_console: Console | None = None

# Label column width = length of longest label ("billing mode:" / "first charge:" = 13).
_LABEL_WIDTH = 13

# Shared --format choices for list-customers / list-keys (UX-DR10 usage-error
# message). Hosted here so both command modules agree on the tuple identity.
LIST_FORMAT_CHOICES: tuple[str, ...] = ("table", "json")

# Story 4.1 — export-customer accepts only JSON (default, machine-readable)
# or YAML (human-readable, LGPD deliverable). Table mode is deliberately
# excluded: LGPD access responses are data-subject deliverables, not
# operator monitoring surfaces.
EXPORT_FORMAT_CHOICES: tuple[str, ...] = ("json", "yaml")


_USER_TEXT_MAX_LEN = 200
"""Cap for stored ``name`` / ``email`` / ``notes`` / ``customer_name`` echoes.

Mirrors ``_truncate_ident`` (commands/keys.py) for OPERATOR-positional args;
this constant covers stored-row echoes that ride through render_* helpers.
A 1 MiB note pasted via ``--add-note`` would otherwise blow Panel layout.
"""


def _safe_user_text(value: str | None, *, max_len: int = _USER_TEXT_MAX_LEN) -> str:
    """Return ``value`` with control bytes neutralised, length-capped, and
    Rich-markup-escaped. The single chokepoint for any operator-stored
    string surfaced in render_* helpers (``name``, ``email``, ``notes``,
    ``customer_name``).

    F24 + F25 + F30 hardening:
      - C0 control bytes (ord < 0x20) other than tab become ``?``. Catches
        ANSI escape (``\\x1b``), BEL, LF/CR (line-splitter into bogus
        table rows), NUL.
      - Length cap at ``max_len`` chars with a U+2026 ellipsis. Prevents a
        pathological 1 MiB ``--add-note`` from blowing Panel layout.
      - Rich markup escape so ``[red]evil[/red]`` renders literally
        instead of restyling the panel/table cell.

    Returns the empty string for ``None`` so callers can safely interpolate.
    """
    if value is None:
        return ""
    cleaned = "".join("?" if ch != "\t" and ord(ch) < 0x20 else ch for ch in value)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "…"
    return _rich_escape(cleaned)


def stdout_console() -> Console:
    """Return the shared stdout Console (singleton)."""
    global _stdout_console
    if _stdout_console is None:
        _stdout_console = Console()
    return _stdout_console


def stderr_console() -> Console:
    """Return the shared stderr Console (singleton)."""
    global _stderr_console
    if _stderr_console is None:
        _stderr_console = Console(stderr=True)
    return _stderr_console


def render_error(message: str, hint: str) -> None:
    """UX-DR6: red-bordered Panel on stderr. Caller decides the exit code."""
    stderr_console().print(Panel(hint, title=message, border_style="red", expand=False))


def render_confirmation(message: str) -> None:
    """UX-DR7: plain single-line confirmation on stdout."""
    stdout_console().print(message)


def _format_label(label: str, value: str) -> str:
    """Return `label<padding>value` with the label right-padded to _LABEL_WIDTH."""
    return f"{label.ljust(_LABEL_WIDTH)} {value}"


def _format_brl(amount: float) -> str:
    """Format ``amount`` using Brazilian Portuguese number conventions.

    Dot is the thousands separator, comma is the decimal separator
    (e.g. 1234.56 → ``1.234,56``). Implemented without ``locale`` so the
    result is independent of the process locale / LC_ALL env.
    """
    integer, fraction = f"{amount:,.2f}".split(".")
    # Python's `,` formatter gives us `1,234` — swap to the pt-BR convention.
    integer_br = integer.replace(",", ".")
    return f"{integer_br},{fraction}"


def _context_block(issued: IssuedKey) -> str:
    """Assemble the labeled context rows for the KeyPanel."""
    rec = issued.record
    lines: list[str] = []
    lines.append(_format_label("prefix:", rec.key_prefix))
    lines.append(
        _format_label("customer:", f"{issued.customer_name} ({rec.customer_id})")
    )
    lines.append(_format_label("rate limit:", f"{rec.rate_limit_rpm}/min"))
    lines.append(_format_label("billing mode:", rec.billing_mode))

    if rec.billing_mode == "complimentary":
        lines.append(_format_label("price:", "R$ 0,00 (bundled externally)"))
        # U+2014 EM DASH (not hyphen-minus): AC2 explicit requirement.
        lines.append(_format_label("paid until:", "\u2014 (not applicable)"))
    else:
        lines.append(
            _format_label("price:", f"R$ {_format_brl(rec.price_monthly)}/month")
        )
        fc_iso = issued.first_charge_date
        if fc_iso is not None:
            n = issued.first_charge_days_free
            if n is None:
                lines.append(_format_label("first charge:", fc_iso))
            else:
                lines.append(
                    _format_label(
                        "first charge:", f"{fc_iso} ({n} days free until then)"
                    )
                )
        else:
            # Story 1.6 — rotate-key path. ``issue-key`` always carries a
            # non-None ``first_charge_date`` on manual-mode keys (the
            # trial-window date). ``rotate-key`` passes ``first_charge_date=None``
            # because rotation preserves billing state; the operator's mental
            # model is "only the plaintext and last_used_at changed". Render
            # the existing ``paid_until`` value so the post-rotation panel
            # answers the same "when does this key next bill?" question as
            # the DetailPanel.
            lines.append(_format_label("paid until:", _iso_date(rec.paid_until)))

    return "\n".join(lines)


def render_key_panel(issued: IssuedKey) -> None:
    """UX-DR1 + UX-DR17: one-shot key display with copy-friendly plaintext.

    Industry-standard three-piece layout (AWS IAM, GitHub PAT, Stripe):
      1. Double-border Panel with title "API KEY (copy now — shown only once)"
         and a short warning body.
      2. The plaintext as a naked line (column 0, no border chars, no label, no
         wrapping) so triple-click selects only the key (UX-DR17). The plaintext
         is deliberately OUTSIDE any panel — wrapping it inside a Panel would
         flank it with vertical border glyphs (``║``) that defeat triple-click.
      3. A blank line separating the plaintext from the context block.
      4. Double-border Panel with labeled context rows (prefix, customer, rate
         limit, billing mode, price, first charge / paid until).

    The `IssuedKey` dataclass is ``frozen=True, slots=True`` — the plaintext is
    typed through rather than passed as a bare ``str`` to discourage accidental
    logging.
    """
    console = stdout_console()

    warning = (
        "Copy the key below now. It is the last time it will be shown — "
        "there is no way to recover it."
    )
    console.print(
        Panel(
            warning,
            box=box.DOUBLE,
            title="API KEY (copy now — shown only once)",
            expand=False,
        )
    )

    # Naked plaintext line at column 0 — UX-DR17 copy-paste fidelity.
    # - ``highlight=False`` + ``markup=False`` keeps the output a plain ASCII
    #   run with no ANSI noise.
    # - ``no_wrap=True`` + ``overflow="ignore"`` + ``crop=False`` ensure the
    #   53-char plaintext stays on a single line even on terminals narrower
    #   than 53 columns; wrapping would break triple-click selection.
    console.print(
        issued.plaintext,
        highlight=False,
        markup=False,
        no_wrap=True,
        overflow="ignore",
        crop=False,
    )

    # Blank line between the plaintext and the context panel so the copyable
    # key has breathing room above the metadata rows.
    console.print("")

    console.print(
        Panel(
            _context_block(issued),
            box=box.DOUBLE,
            expand=False,
        )
    )


def _status_label(status: CustomerStatus, billing_mode: BillingMode) -> tuple[str, str]:
    """Return ``(text, rich_color)`` for the status cell.

    UX-DR8 is text-first with color as accelerator: the label word carries
    the meaning. ``complimentary`` blue supersedes ``active`` green because
    the blue badge is the operator-meaningful signal — it tells the reader
    "this key is bundled, not a billing target" without them having to
    cross-reference the billing_mode column.
    """
    if billing_mode == "complimentary":
        return ("complimentary", "blue")
    if status == "active":
        return ("active", "green")
    if status == "suspended":
        return ("suspended", "yellow")
    if status == "revoked":
        return ("revoked", "red")
    # Fail loudly when CustomerStatus/BillingMode grows a new literal and
    # this branch table falls out of date — a silent "" style would hide
    # the drift until a reviewer noticed missing color.
    raise ValueError(
        f"Unhandled status/billing_mode combination: {status=}, {billing_mode=}"
    )


def _iso_date(value: str | None) -> str:
    """Truncate an ISO 8601 UTC timestamp to its ``YYYY-MM-DD`` date prefix.

    Returns the em-dash ``—`` (U+2014) for NULL or malformed values. The
    slice is safe against both date-only and datetime strings because
    SQLite stores both formats with ``YYYY-MM-DD`` as the first 10
    characters; anything shorter or not shaped like an ISO date is
    rendered as the em-dash so the operator sees "no data" rather than
    a corrupted partial string.
    """
    if value is None or len(value) < 10:
        return "\u2014"
    head = value[:10]
    if (
        head[4] != "-"
        or head[7] != "-"
        or not (head[0:4].isdigit() and head[5:7].isdigit() and head[8:10].isdigit())
    ):
        return "\u2014"
    return head


def render_customer_list(title: str, rows: list[CustomerListRow]) -> None:
    """Render the ``list-customers`` StatusTable variant (no status column).

    Callers must handle the empty case via ``render_empty_state`` — this
    renderer assumes ``rows`` is non-empty so the caller's policy for
    first-run / all-purged / subsequent-empty states stays visible at
    the call site (UX-DR13).
    """
    console = stdout_console()
    table = Table(title=title, expand=False)
    table.add_column("Customer ID", justify="left", no_wrap=True)
    table.add_column("Name", justify="left")
    table.add_column("Email", justify="left")
    table.add_column("Keys", justify="right")
    table.add_column("Registered", justify="left", no_wrap=True)
    for row in rows:
        table.add_row(
            row.id,
            _safe_user_text(row.name),
            _safe_user_text(row.email),
            str(row.key_count),
            _iso_date(row.registered_at),
        )
    console.print(table)
    render_confirmation(f"{len(rows)} customers found.")


def render_key_list(title: str, rows: list[ApiKeyListRow]) -> None:
    """Render the ``list-keys`` StatusTable variant with color-coded status.

    Callers must handle the empty case via ``render_empty_state``.
    """
    console = stdout_console()
    table = Table(title=title, expand=False)
    table.add_column("Prefix", justify="left", no_wrap=True)
    table.add_column("Customer", justify="left")
    table.add_column("Status", justify="left")
    table.add_column("RPM", justify="right")
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Paid Until", justify="left", no_wrap=True)
    for row in rows:
        label, color = _status_label(row.status, row.billing_mode)
        status_cell = Text(label, style=color)
        rpm_cell = f"{row.rate_limit_rpm}/min"
        if row.billing_mode == "complimentary":
            price_cell = f"R$ {_format_brl(0.0)}"
        else:
            price_cell = f"R$ {_format_brl(row.price_monthly)}/month"
        paid_cell = (
            _iso_date(row.paid_until)
            if row.billing_mode != "complimentary"
            else "\u2014"
        )
        table.add_row(
            row.key_prefix,
            _safe_user_text(row.customer_name),
            status_cell,
            rpm_cell,
            price_cell,
            paid_cell,
        )
    console.print(table)
    render_confirmation(f"{len(rows)} keys found.")


def render_at_risk_table(
    rows: list[tuple[ApiKeyListRow, int]],
    window_days: int,
) -> None:
    """Story 3.2 UX-DR2 — StatusTable for ``at-risk`` with yellow badges.

    Column order mirrors ``render_key_list`` but replaces the tail "Paid
    Until" column with "Paid Until" + "Days Left" (right-justified signed
    integer). Status cell is always ``Text("at-risk", style="yellow")``:
    the at-risk set is a semantic overlay on active manual keys (the
    underlying status is still ``active``), so the colored label here is
    hard-coded rather than routed through ``_status_label``.

    Callers must handle the empty case via ``render_empty_state``; this
    renderer assumes ``rows`` is non-empty. Each entry is a
    ``(row, days_left)`` pair where ``days_left`` is the signed integer
    the caller already computed during filtering — the renderer does NOT
    re-derive it from ``row.paid_until`` so the two surfaces can't drift.
    """
    console = stdout_console()
    table = Table(title=f"At-Risk Keys (next {window_days} days)", expand=False)
    table.add_column("Prefix", justify="left", no_wrap=True)
    table.add_column("Customer", justify="left")
    table.add_column("Status", justify="left")
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Paid Until", justify="left", no_wrap=True)
    table.add_column("Days Left", justify="right", no_wrap=True)
    for row, days_left in rows:
        # Caller filters NULL paid_until; the renderer only runs on rows
        # where the filter guarantees a non-NULL ISO date. Runtime guard
        # (not ``assert``) so ``python -O`` doesn't strip the check.
        if row.paid_until is None:
            continue
        table.add_row(
            row.key_prefix,
            _safe_user_text(row.customer_name),
            Text("at-risk", style="yellow"),
            f"R$ {_format_brl(row.price_monthly)}/month",
            _iso_date(row.paid_until),
            str(days_left),
        )
    console.print(table)
    render_confirmation(f"{len(rows)} keys found.")


def render_suspend_sweep_preview(
    rows: list[ApiKeyListRow],
    cutoff_date: date,
) -> None:
    """Story 3.3 UX-DR2 — StatusTable preview for ``suspend-sweep --dry-run``.

    Column order mirrors ``render_at_risk_table`` minus the look-ahead
    ``Days Left`` column — dry-run rows are already past cutoff, so the
    column would read negative-days-overdue and only clutter the eye.
    Status cell is always ``Text("to-suspend", style="yellow")``: the
    would-be-suspended set is a semantic overlay on ``active`` rows, so
    the colored label is hard-coded rather than routed through
    ``_status_label``.

    Callers must handle the empty case via ``render_empty_state``; this
    renderer assumes ``rows`` is non-empty.
    """
    console = stdout_console()
    table = Table(
        title=f"Dry-run preview: keys to suspend at cutoff {cutoff_date.isoformat()}",
        expand=False,
    )
    table.add_column("Prefix", justify="left", no_wrap=True)
    table.add_column("Customer", justify="left")
    table.add_column("Status", justify="left")
    table.add_column("Price", justify="right", no_wrap=True)
    table.add_column("Paid Until", justify="left", no_wrap=True)
    for row in rows:
        # Caller filters NULL paid_until before calling this renderer.
        # Runtime guard (not ``assert``) so ``python -O`` doesn't strip it.
        if row.paid_until is None:
            continue
        table.add_row(
            row.key_prefix,
            Text(row.customer_name),
            Text("to-suspend", style="yellow"),
            f"R$ {_format_brl(row.price_monthly)}/month",
            _iso_date(row.paid_until),
        )
    console.print(table)
    render_confirmation(
        f"{len(rows)} key(s) would be suspended. Run without --dry-run to proceed."
    )


_REVENUE_REPORT_COUNT_LABELS: tuple[str, ...] = (
    "Paid customers:",
    "Pending:",
    "Suspended (carry-over):",
    "Complimentary:",
)

_REVENUE_REPORT_TOTAL_LABELS: tuple[str, ...] = (
    "Monthly recurring:",
    "Projected (if all pay):",
    "Next charge date:",
)


def render_revenue_report(report: RevenueReport) -> None:
    """Story 3.2 UX-DR4 — single-border Panel with count + totals + (opt) comp list.

    Structure:
        Count block → Rule() → totals block (bold on mrr + projected) →
        [Rule() → complimentary listing] (only when ``complimentary_rows``
        is non-empty). Panel title is ``Revenue Report — {month_label}``.

    Rich markup is enabled on the panel so ``[bold]`` renders, but each
    complimentary row line is emitted with ``markup=False, highlight=False``
    at print time — the panel is assembled as a Rich ``Group`` rather than a
    single markup string so operator-supplied ``notes`` cannot inject
    arbitrary markup. Callers must short-circuit to ``render_empty_state``
    when every bucket count is zero (the renderer assumes the portfolio is
    non-trivial — any count > 0 is enough).
    """
    from rich.console import Group, RenderableType

    # Column widths are derived from the longest label on each side of the
    # Rule() so a future label rename does not drift the layout.
    all_labels = (*_REVENUE_REPORT_COUNT_LABELS, *_REVENUE_REPORT_TOTAL_LABELS)
    label_width = max(len(label) for label in all_labels)

    counts = (
        report.paid_count,
        report.pending_count,
        report.suspended_count,
        report.complimentary_count,
    )
    count_lines = "\n".join(
        f"{label.ljust(label_width)}  {value}"
        for label, value in zip(_REVENUE_REPORT_COUNT_LABELS, counts, strict=True)
    )

    # Bold on the monetary totals only; label and prefix stay unbolded so
    # triple-click selects a readable "R$ 1.643,00" segment.
    totals_lines = "\n".join(
        (
            (
                f"{_REVENUE_REPORT_TOTAL_LABELS[0].ljust(label_width)}  "
                f"[bold]R$ {_format_brl(report.mrr)}[/bold]"
            ),
            (
                f"{_REVENUE_REPORT_TOTAL_LABELS[1].ljust(label_width)}  "
                f"[bold]R$ {_format_brl(report.projected)}[/bold]"
            ),
            (
                f"{_REVENUE_REPORT_TOTAL_LABELS[2].ljust(label_width)}  "
                f"{report.next_charge_date}"
            ),
        )
    )

    children: list[RenderableType] = [
        Text(count_lines),
        Rule(),
        Text.from_markup(totals_lines),
    ]

    if report.complimentary_rows:
        children.append(Rule())
        children.append(Text("Complimentary keys:"))
        for row in report.complimentary_rows:
            # Operator-supplied ``notes`` is untrusted — render as plain
            # Text() (no markup parse) so a value like ``[red]evil[/]`` is
            # displayed literally instead of restyling the panel.
            # Strip control bytes (NL/CR would phantom-row Panel layout)
            # and cap length on operator-stored fields. Text() already
            # ignores Rich markup; the rich-escape inside _safe_user_text
            # is a harmless no-op for this Text() path.
            notes = _safe_user_text(row.notes) if row.notes else "\u2014"
            # B15 \u2014 surface suspended/revoked complimentary keys explicitly.
            # ``revenue`` historically rendered them indistinguishably from
            # active ones, hiding a "this bundled customer is currently
            # blocked" signal that operators need before quarterly
            # reconciliation. ``[suspended]`` / ``[revoked]`` is a literal
            # tag (no Rich markup parse) so the panel layout stays stable.
            status_tag = f" [{row.status}]" if row.status != "active" else ""
            children.append(
                Text(
                    f"  {row.key_prefix} "
                    f"({_safe_user_text(row.customer_name)}){status_tag} \u2014 "
                    f"{notes}"
                )
            )

    stdout_console().print(
        Panel(
            Group(*children),
            title=f"Revenue Report \u2014 {report.month_label}",
            box=box.ROUNDED,
            expand=False,
        )
    )


def render_json(payload: list[dict[str, object]]) -> None:
    """Emit ``payload`` as pretty-printed JSON via the stdout console.

    Uses ``Console.print_json(data=...)`` so ``NO_COLOR`` / ``force_terminal``
    still apply consistently with the other render_* helpers. Empty lists
    serialize as ``[]``.
    """
    stdout_console().print_json(data=payload)


def render_empty_state(lines: list[str]) -> None:
    """Emit one or more plain informational lines via ``render_confirmation``.

    Used for the UX-DR12 / UX-DR13 "empty results are not errors" path —
    callers pass the exact lines (first-run hint, all-purged hint, or a
    single-line informational sentence) and this helper keeps the output
    policy centralized.
    """
    for line in lines:
        render_confirmation(line)


_CUSTOMER_DETAIL_LABELS: tuple[str, ...] = (
    "ID:",
    "Name:",
    "Email:",
    "Tax ID Kind:",
    "Keys:",
    "Registered:",
    "Notes:",
)

_KEY_DETAIL_LABELS: tuple[str, ...] = (
    "Prefix:",
    "Customer:",
    "Status:",
    "RPM:",
    "Price:",
    "Billing:",
    "Paid Until:",
    "Created:",
    "Last Used:",
    "Age:",
    "Notes:",
)


def _pad_label(label: str, width: int) -> str:
    """Return ``label`` right-padded to ``width`` plus a trailing space."""
    return f"{label.ljust(width)} "


def _format_last_used(value: str | None) -> str:
    """Render ``last_used_at`` as ``YYYY-MM-DD HH:MM:SS`` or ``never``."""
    if value is None:
        return "never"
    # SQLite TEXT ISO 8601 UTC form: "2026-05-12T14:23:45Z" → "2026-05-12 14:23:45".
    return value.replace("T", " ").removesuffix("Z")


def _format_age(days: int) -> str:
    """Render ``age_days`` — ``today`` for zero, ``{n} days`` otherwise."""
    if days == 0:
        return "today"
    return f"{days} days"


def render_customer_detail(row: CustomerDetailRow) -> None:
    """UX-DR3 DetailPanel — single-line border, labeled rows, no tax-ID ciphertext."""
    width = max(len(label) for label in _CUSTOMER_DETAIL_LABELS)
    notes_safe = _safe_user_text(row.notes) if row.notes else "\u2014"
    lines = [
        f"{_pad_label('ID:', width)}{row.id}",
        f"{_pad_label('Name:', width)}{_safe_user_text(row.name)}",
        f"{_pad_label('Email:', width)}{_safe_user_text(row.email)}",
        f"{_pad_label('Tax ID Kind:', width)}{row.tax_id_kind.upper()}",
        f"{_pad_label('Keys:', width)}{row.key_count}",
        f"{_pad_label('Registered:', width)}{_iso_date(row.registered_at)}",
        f"{_pad_label('Notes:', width)}{notes_safe}",
    ]
    stdout_console().print(
        Panel(
            "\n".join(lines),
            title=f"Customer: {row.id}",
            box=box.ROUNDED,
            expand=False,
        )
    )


def render_key_detail(row: KeyDetailRow) -> None:
    """UX-DR3 DetailPanel for keys — color-coded status, pt-BR price, age in days."""
    width = max(len(label) for label in _KEY_DETAIL_LABELS)

    label, color = _status_label(row.status, row.billing_mode)
    status_cell = f"[{color}]{label}[/]" if color else label

    if row.billing_mode == "complimentary":
        price_cell = "R$ 0,00 (bundled externally)"
    else:
        price_cell = f"R$ {_format_brl(row.price_monthly)}/month"

    notes_safe = _safe_user_text(row.notes) if row.notes else "\u2014"
    lines = [
        f"{_pad_label('Prefix:', width)}{row.key_prefix}",
        f"{_pad_label('Customer:', width)}{_safe_user_text(row.customer_name)} "
        f"({row.customer_id})",
        f"{_pad_label('Status:', width)}{status_cell}",
        f"{_pad_label('RPM:', width)}{row.rate_limit_rpm}/min",
        f"{_pad_label('Price:', width)}{price_cell}",
        f"{_pad_label('Billing:', width)}{row.billing_mode}",
        f"{_pad_label('Paid Until:', width)}{_iso_date(row.paid_until)}",
        f"{_pad_label('Created:', width)}{_iso_date(row.created_at)}",
        f"{_pad_label('Last Used:', width)}{_format_last_used(row.last_used_at)}",
        f"{_pad_label('Age:', width)}{_format_age(row.age_days)}",
        f"{_pad_label('Notes:', width)}{notes_safe}",
    ]
    stdout_console().print(
        Panel(
            "\n".join(lines),
            title=f"Key: {row.key_prefix}",
            box=box.ROUNDED,
            expand=False,
        )
    )


def render_audit_tail(events: list[AuditEventRow]) -> None:
    """UX-DR5 AuditTail — plain indented lines under the DetailPanel.

    No panel border (this is the supplementary section); a single blank line
    separates it from the DetailPanel above. Empty result renders as the
    single literal ``  No events recorded`` per UX-DR5.
    """
    render_confirmation("")
    if not events:
        render_confirmation("  No events recorded")
        return

    # B6 — singular/plural agreement when the audit-tail returns exactly 1.
    plural = "" if len(events) == 1 else "s"
    render_confirmation(f"Last {len(events)} event{plural}:")
    action_width = max(len(event.action) for event in events)
    for event in events:
        timestamp = event.occurred_at.replace("T", " ").removesuffix("Z")
        action = event.action.upper().ljust(action_width)
        tail = f"  {event.details_json}" if event.details_json is not None else ""
        line = f"  {timestamp}  {action}  {event.actor}{tail}"
        # Disable Rich markup interpretation so a details_json that happens to
        # contain ``[foo]`` doesn't get eaten by Rich's console markup parser.
        stdout_console().print(line, markup=False, highlight=False)


CACHE_STATUS_BOUND_MESSAGE = (
    "Auth cache propagation bound: 60 seconds. Key-state changes become "
    "visible to the API within this window. This command does not force "
    "invalidation; to empty the cache faster, restart the affected "
    "replica(s)."
)
"""Story 2.3 AC9 / AC10 — verbatim string; do not rewrap or edit inline.

Public (no underscore) because both ``cache-status`` and its tests import
it as the single source of truth for AC9's byte-identity assertion.
"""


def _format_uptime(seconds: int) -> str:
    """Render ``seconds`` as ``HHh MMm`` / ``MMm SSs`` / ``SSs``.

    The snapshot's ``process_uptime_seconds`` can range from 0 (just
    started) to weeks; we pick a fixed two-unit display so the CLI output
    stays column-stable regardless of age.
    """
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes:02d}m"


def _format_decimal(value: float, *, places: int = 1) -> str:
    """Return ``value`` with fixed decimal places. Used for hit-rate / avg rpm."""
    return f"{value:.{places}f}"


def render_cache_status(status: AggregatedCacheStatus | None) -> None:
    """Story 2.3 AC9 — emit the cache-status table output.

    ``status=None`` → no readable snapshot from any replica. The verbatim
    ``CACHE_STATUS_BOUND_MESSAGE`` always leads — AC9 requires it on every
    reachable state. Multi-replica aggregates are surfaced with a
    ``Replicas:`` row so operators see the cluster topology; single-replica
    output still reads naturally (``Replicas: 1``).

    Data is shown with an honest age label regardless of freshness: the
    operator decides whether a 3-minute-old aggregate is acceptable for
    their diagnostic. We do not hide data behind an arbitrary cutoff.

    No ``markup`` / ``highlight`` processing: the message is plain prose,
    not Rich markup, and all numeric fields are already-typed integers.
    """
    console = stdout_console()
    console.print(CACHE_STATUS_BOUND_MESSAGE, markup=False, highlight=False)
    console.print("", markup=False, highlight=False)

    if status is None:
        console.print(
            "Cache health: unavailable (no running API instance).",
            markup=False,
            highlight=False,
        )
        return

    lookups = status.lookups
    hit_rate_pct = status.hit_rate * 100.0
    age_label = format_seconds_ago(status.snapshot_age_seconds)
    console.print(
        f"Cache health (from snapshot written {age_label}):",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Replicas:  {status.replicas}",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Entries:   {status.entries} (maxsize {status.maxsize_per_replica}/replica)",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Hit rate:  {_format_decimal(hit_rate_pct)}% "
        f"({status.hits} hits / {lookups} lookups)",
        markup=False,
        highlight=False,
    )
    uptime_label = _format_uptime(status.process_uptime_seconds)
    console.print(
        f"  Uptime:    {uptime_label} (min across replicas)",
        markup=False,
        highlight=False,
    )


_LAST_24H_UNAVAILABLE_LINES: tuple[str, ...] = (
    "Last-24h stats: unavailable.",
    "  The API may not be running, or this key has not served a request",
    "  since the last restart. Counters reset on restart (not durable).",
)


def render_last_24h_stats(stats: Last24hStats | None) -> None:
    """Story 2.3 §7 — render the last-24h stats section for key targets.

    Prints a blank leader line to separate this section from the preceding
    DetailPanel (mirrors ``render_audit_tail``). When ``stats`` is ``None``
    the canonical "unavailable" breadcrumb is rendered; callers DO NOT
    need to compute their own message.

    Multi-replica aggregate: when ``stats.replicas > 1`` the header
    appends "aggregated across N replicas" so the operator can't mistake
    the counters for single-replica-only. Data is shown regardless of age
    — freshness is surfaced as a label, not used to hide data behind an
    arbitrary cutoff.

    ``markup=False, highlight=False`` avoids the Rich console gobbling any
    bracketed substring in a future counter field.
    """
    console = stdout_console()
    console.print("", markup=False, highlight=False)

    if stats is None:
        for line in _LAST_24H_UNAVAILABLE_LINES:
            console.print(line, markup=False, highlight=False)
        return

    replica_note = (
        f", aggregated across {stats.replicas} replicas" if stats.replicas > 1 else ""
    )
    header = (
        f"Last-24h stats (from snapshot written "
        f"{format_seconds_ago(stats.snapshot_age_seconds)}, "
        f"uptime {_format_uptime(stats.process_uptime_seconds)}"
        f"{replica_note}):"
    )
    console.print(header, markup=False, highlight=False)
    console.print(
        f"  Requests total:     {stats.requests_total}",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Auth success:       {stats.auth_success}",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Rate-limit 429:     {stats.rate_limit_429}",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Peak RPM:           {stats.peak_rpm}",
        markup=False,
        highlight=False,
    )
    console.print(
        f"  Avg RPM (last 60s): {stats.avg_rpm_last_60s}",
        markup=False,
        highlight=False,
    )
    if stats.last_activity_at is None:
        last_text = "never"
    else:
        last_text = _last_activity_ago(stats.last_activity_at)
    console.print(
        f"  Last activity:      {last_text}",
        markup=False,
        highlight=False,
    )


def _last_activity_ago(iso_ts: str) -> str:
    """Return "Xs ago" from an ISO 8601 UTC timestamp.

    Robust against coarse ISO forms (trailing ``Z``, no fractional seconds).
    On parse failure returns the raw timestamp — the renderer would rather
    surface a strange string than a misleading "X seconds ago".
    """
    try:
        iso_clean = iso_ts.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso_clean)
        now = datetime.now(UTC)
        delta_seconds = int((now - parsed).total_seconds())
        return format_seconds_ago(delta_seconds)
    except (ValueError, TypeError):
        return iso_ts


def render_show_json(
    *,
    target_type: Literal["customer", "api_key"],
    target: dict[str, object],
    events: list[dict[str, object]] | None,
    stats: dict[str, object] | None = None,
    include_stats: bool = False,
) -> None:
    """Emit the AC8 single-object JSON envelope via the shared stdout console.

    ``events=None`` omits the ``events`` key entirely — distinct from an
    empty-list emission triggered by ``--audit-tail N`` returning zero rows.

    Story 2.3 — ``include_stats=True`` adds ``"stats": <dict or null>`` to
    the payload. Presence is caller-controlled: a key-target ``show`` with
    ``--audit-tail`` sets ``include_stats=True`` and passes the parsed
    ``Last24hStats`` dict (or ``None`` when the snapshot is unavailable).
    Customer targets and bare (no ``--audit-tail``) ``show`` calls leave
    ``include_stats=False`` so the JSON payload stays schema-stable.
    """
    payload: dict[str, object] = {"target_type": target_type, "target": target}
    if events is not None:
        payload["events"] = events
    if include_stats:
        payload["stats"] = stats
    stdout_console().print_json(data=payload)


_SEVERITY_STYLE: dict[str, str] = {
    "ok": "",
    "info": "",
    "warn": "yellow",
    "fail": "red",
}


def _health_row(check: HealthCheck, label_width: int) -> str:
    label = _rich_escape(check.label).ljust(label_width)
    value = _rich_escape(check.value)
    style = _SEVERITY_STYLE.get(check.severity, "")
    if style:
        value = f"[{style}]{value}[/{style}]"
    detail_segment = ""
    if check.detail is not None and check.detail != "":
        detail_segment = f" [dim]({_rich_escape(check.detail)})[/dim]"
    return f"{label} {value}{detail_segment}"


def render_health_report(report: HealthReport) -> None:
    """UX-DR3 DetailPanel for ``compadre-keys health`` (Story 3.4).

    Single-border rounded Panel titled ``System Health``. One row per
    ``HealthCheck`` in the report's ``checks`` order; labels are
    right-padded to the longest label so values column-align. Severity
    drives colour (``warn`` → yellow, ``fail`` → red; ``ok``/``info``
    plain). Footer is a ``render_confirmation`` line with the aggregate
    verdict.
    """
    checks = report.checks
    # Runtime guard (not ``assert`` — survives ``python -O``). The
    # collector always emits 8 checks today; future extensions append.
    if len(checks) == 0:
        render_confirmation("System Health: no checks available.")
        return
    label_width = max(len(c.label) for c in checks)
    body = "\n".join(_health_row(c, label_width) for c in checks)
    stdout_console().print(
        Panel(
            body,
            title="System Health",
            box=box.ROUNDED,
            expand=False,
        )
    )
    warn_count = sum(1 for c in checks if c.severity == "warn")
    fail_count = sum(1 for c in checks if c.severity == "fail")
    if report.is_healthy:
        render_confirmation("Healthy.")
    else:
        render_confirmation(
            f"Unhealthy: {warn_count} warning(s), {fail_count} failure(s)."
        )


def render_export_json(payload: dict[str, object]) -> None:
    """Story 4.1 — emit the export payload as indented JSON to stdout.

    Uses ``Console.print_json`` so NO_COLOR / force_terminal flags apply
    consistently with the other render_* helpers. Key order is preserved
    from the caller's dict insertion order (Python 3.7+ / CPython 3.6+).
    """
    stdout_console().print_json(data=payload, indent=2)


def render_export_yaml(payload: dict[str, object]) -> None:
    """Story 4.1 — emit the export payload as YAML 1.1 (safe subset) to stdout.

    Key-order preserved from the ``payload`` dict's insertion order so
    the AC1 field-ordering contract holds on YAML output too. Unicode
    passes through verbatim (names / notes with accented characters).

    Written directly to the underlying console file rather than through
    ``console.print`` so rich markup parsing cannot eat bracketed
    substrings in operator-supplied notes (``[urgent]`` etc.).

    ``yaml.safe_dump`` is the chosen surface — never ``yaml.dump`` —
    because it caps at YAML 1.1 standard types and never emits the
    ``!!python/object`` tag that ``yaml.load`` would honor on the
    consumer side.
    """
    import yaml

    file = stdout_console().file
    file.write(
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    )
    # Flush explicitly: ``Console.print_json`` routes through Rich's buffer
    # flush, but the direct ``file.write`` above skips that discipline.
    # Without this, a SIGPIPE from a short-reading downstream (e.g. ``| head``)
    # can abort the interpreter before the at-exit flush, leaving the LGPD
    # deliverable truncated on disk.
    file.flush()


def _reset_for_tests() -> None:
    """Test-only hook: reset console singletons so fixtures can re-wire I/O."""
    global _stdout_console, _stderr_console
    _stdout_console = None
    _stderr_console = None

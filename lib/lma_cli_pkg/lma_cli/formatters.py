# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Rich console output helpers for LMA CLI."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ── Theme ─────────────────────────────────────────────────────

LMA_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "heading": "bold magenta",
        "key": "bold cyan",
        "value": "white",
        "dim": "dim",
        "stack_name": "bold blue",
    }
)

console = Console(theme=LMA_THEME)
err_console = Console(stderr=True, theme=LMA_THEME)


# ── Helpers ───────────────────────────────────────────────────


def print_success(message: str) -> None:
    """Print a success message."""
    console.print(f"[success]✓[/success] {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    err_console.print(f"[error]✗[/error] {message}")


def print_warning(message: str) -> None:
    """Print a warning message."""
    console.print(f"[warning]⚠[/warning] {message}")


def print_info(message: str) -> None:
    """Print an info message."""
    console.print(f"[info]ℹ[/info] {message}")


def print_key_value(key: str, value: str, indent: int = 0) -> None:
    """Print a key-value pair."""
    pad = " " * indent
    console.print(f"{pad}[key]{key}:[/key] [value]{value}[/value]")


def print_heading(text: str) -> None:
    """Print a section heading."""
    console.print(f"\n[heading]{text}[/heading]")


# ── Stack status formatting ───────────────────────────────────


def format_stack_status(status: str) -> str:
    """Colourize a CloudFormation stack status string."""
    if "COMPLETE" in status and "ROLLBACK" not in status:
        return f"[success]{status}[/success]"
    if "IN_PROGRESS" in status:
        return f"[warning]{status}[/warning]"
    if "FAILED" in status or "ROLLBACK" in status:
        return f"[error]{status}[/error]"
    return status


def stack_info_table(stack) -> Table:
    """Create a Rich table for stack info."""
    table = Table(title="Stack Information", show_header=False, expand=True)
    table.add_column("Property", style="key", min_width=20)
    table.add_column("Value", style="value")

    table.add_row("Stack Name", stack.stack_name)
    table.add_row("Status", format_stack_status(stack.status))
    if stack.status_reason:
        table.add_row("Status Reason", stack.status_reason)
    if stack.creation_time:
        table.add_row("Created", str(stack.creation_time))
    if stack.last_updated_time:
        table.add_row("Last Updated", str(stack.last_updated_time))
    if stack.stack_id:
        table.add_row("Stack ID", Text(stack.stack_id, overflow="fold"))

    return table


def stack_outputs_table(outputs: list) -> Table:
    """Create a Rich table for stack outputs."""
    table = Table(title="Stack Outputs", expand=True)
    table.add_column("Key", style="key")
    table.add_column("Value", style="value", overflow="fold")
    table.add_column("Description", style="dim")

    for o in outputs:
        table.add_row(o.key, o.value, o.description)

    return table


# ── Publish formatting ────────────────────────────────────────


def publish_result_panel(result) -> Panel:
    """Create a Rich panel summarising publish results."""
    lines = []
    lines.append(f"[key]Version:[/key]  {result.version}")
    lines.append(f"[key]Bucket:[/key]   {result.bucket}")
    lines.append(f"[key]Region:[/key]   {result.region}")
    lines.append(f"[key]Duration:[/key] {result.duration_seconds}s")
    lines.append("")

    for sr in result.stack_results:
        if sr.skipped:
            icon = "[dim]⊘[/dim]"
            msg = f"[dim]{sr.stack_name} — skipped (unchanged)[/dim]"
        elif sr.success:
            icon = "[success]✓[/success]"
            msg = f"{sr.stack_name} — {sr.message} ({sr.duration_seconds}s)"
        else:
            icon = "[error]✗[/error]"
            msg = f"[error]{sr.stack_name} — {sr.message}[/error]"
        lines.append(f"  {icon} {msg}")

    status = "[success]SUCCESS[/success]" if result.success else "[error]FAILED[/error]"
    panel = Panel(
        "\n".join(lines),
        title=f"Publish Result — {status}",
        border_style="green" if result.success else "red",
        expand=True,
    )
    console.print(panel)

    # Print URLs outside the panel so they render in full
    if result.template_url:
        console.print(f"\n[key]Template URL:[/key]")
        console.print(Text(f"  {result.template_url}", overflow="fold"))
    if result.console_url:
        console.print(f"\n[key]Console URL:[/key]")
        console.print(Text(f"  {result.console_url}", overflow="fold"))

    console.print()
    return None  # Already printed

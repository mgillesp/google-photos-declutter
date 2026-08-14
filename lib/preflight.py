"""Pre-flight dependency checks with human-readable failure messages.

The scripts in this repo shell out to a few Homebrew tools. When one is missing,
the default failure is either a silent skip (you quietly get worse results and
never find out why) or a raw OSError traceback. Neither is useful if you're not
the person who wrote this.

check_tools() prints one clear block up front: what's installed, what isn't,
what each missing tool costs you, and the exact command to fix it.

Usage:
    from lib.preflight import check_tools
    check_tools(required=["exiftool"], optional=["czkawka_cli", "tesseract"])
"""
from __future__ import annotations

import shutil
import sys

# tool name -> (brew formula, what it does, what happens without it)
TOOLS: dict[str, tuple[str, str, str]] = {
    "czkawka_cli": (
        "czkawka",
        "exact + near-duplicate detection",
        "Duplicate and similar-photo groups will be MISSING from your review "
        "sheet. This is the main reason to use this tool, so you almost "
        "certainly want it installed.",
    ),
    "exiftool": (
        "exiftool",
        "writing correct capture dates onto keeper files",
        "Required. Without it, re-uploaded photos land at today's date instead "
        "of their original date, which scrambles your timeline.",
    ),
    "ffprobe": (
        "ffmpeg",
        "reading video duration and size",
        "Oversized-video flagging will be skipped. Everything else still works.",
    ),
    "tesseract": (
        "tesseract",
        "local OCR for spotting text-heavy/document photos",
        "Text/document flagging will be skipped. Everything else still works.",
    ),
}

INSTALL_HINT = (
    "If you don't have Homebrew yet, install it first from https://brew.sh "
    "(one paste-able command), then re-run the line above."
)


def _describe(tool: str) -> tuple[str, str, str]:
    return TOOLS.get(tool, (tool, "an external step", "That step will fail."))


def check_tools(required: list[str] | None = None,
                optional: list[str] | None = None,
                quiet_when_ok: bool = True) -> None:
    """Verify external CLI tools are on PATH.

    Missing *required* tools print an explanation and exit(4).
    Missing *optional* tools print a warning and let the run continue.
    """
    required = required or []
    optional = optional or []

    missing_required = [t for t in required if not shutil.which(t)]
    missing_optional = [t for t in optional if not shutil.which(t)]

    if not missing_required and not missing_optional:
        if not quiet_when_ok:
            print("Pre-flight: all external tools found.")
        return

    if missing_optional and not missing_required:
        print("\n" + "-" * 68, file=sys.stderr)
        print("Heads up: some optional tools are missing.", file=sys.stderr)
        print("-" * 68, file=sys.stderr)
        for tool in missing_optional:
            formula, does, without = _describe(tool)
            print(f"\n  {tool} — {does}", file=sys.stderr)
            print(f"    Without it: {without}", file=sys.stderr)
            print(f"    Install:    brew install {formula}", file=sys.stderr)
        print(f"\n{INSTALL_HINT}", file=sys.stderr)
        print("\nContinuing without them.\n" + "-" * 68 + "\n", file=sys.stderr)
        return

    print("\n" + "=" * 68, file=sys.stderr)
    print("Can't continue — a required tool isn't installed.", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    for tool in missing_required:
        formula, does, without = _describe(tool)
        print(f"\n  {tool} — {does}", file=sys.stderr)
        print(f"    {without}", file=sys.stderr)
        print(f"    Install:    brew install {formula}", file=sys.stderr)
    if missing_optional:
        print("\nAlso missing (optional, but worth installing while you're "
              "at it):", file=sys.stderr)
        for tool in missing_optional:
            formula, does, _ = _describe(tool)
            print(f"  brew install {formula}    # {does}", file=sys.stderr)

    formulas = sorted({_describe(t)[0] for t in missing_required + missing_optional})
    print(f"\nAll at once:\n  brew install {' '.join(formulas)}", file=sys.stderr)
    print(f"\n{INSTALL_HINT}", file=sys.stderr)
    print("=" * 68 + "\n", file=sys.stderr)
    raise SystemExit(4)


def explain_empty_batch(batch_dir, takeout_dir) -> str:
    """Message for when a batch folder exists but has no media in it."""
    return (
        f"\nNo photos or videos found under:\n  {takeout_dir}\n\n"
        "This is usually NOT a folder-depth problem -- the search here is "
        "recursive, so nesting variations under 'takeout/' are handled fine. "
        "The real cause is almost always one of:\n\n"
        "  1. An interrupted download: a multi-gigabyte Takeout zip cut off "
        "partway extracts to an empty or near-empty folder, often without an "
        "obvious error. Check integrity before extracting next time:\n"
        "       unzip -t <zip>\n"
        "  2. The zip was never actually extracted into "
        f"{batch_dir}/takeout/ at all.\n"
        "  3. The wrong path was given to this script.\n"
    )


def explain_missing_takeout(batch_dir, takeout_dir) -> str:
    """Message for when the takeout dir doesn't exist at all."""
    return (
        f"\nCouldn't find the exported photos for this batch.\n\n"
        f"  Expected: {takeout_dir}\n\n"
        "Before running this step you need to:\n"
        "  1. In Google Photos, search the month you're working on, Select all, "
        "and add everything to a temporary album.\n"
        "  2. Run Google Takeout scoped to just that album and download the zip.\n"
        f"  3. Extract the zip into {batch_dir}/takeout/\n\n"
        "See QUICKSTART.md, step 1, for the full walkthrough.\n"
    )

"""
sheets.bootstrap
----------------
One-time CLI that materializes the Target spreadsheet from
`config/org_structure.yaml`.

Reads the YAML, opens the Target sheet, and for each tab:
  1. Creates the tab if it doesn't exist.
  2. Writes the header row.
  3. Writes any static (non-personnel) rows.
  4. Applies column widths if specified.
  5. Verifies the tab is empty of personnel data afterwards.

Also creates the two state tabs (`PENDING REQUESTS`, `AUDIT_LOG`) — but
those are normally created lazily by their services on first use, and the
bootstrap step ensures they exist with the right schema.

NEVER touches the Nova sheet. NEVER copies any personnel rows.

Run with:
    .venv\\Scripts\\python -m sheets.bootstrap
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from config import get_settings
from sheets import repository, client as sheets_client
from sheets.exceptions import SheetsError

logger = logging.getLogger("sheets.bootstrap")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_anchors(yaml_obj: dict[str, Any]) -> dict[str, Any]:
    """Resolve ${anchor} references in the org_structure.yaml.

    PyYAML doesn't expand ${...} by default; this is a tiny substitution so
    the same header set can be reused across many tabs.
    """
    if isinstance(yaml_obj, dict):
        return {k: _resolve_anchors(v) for k, v in yaml_obj.items()}
    if isinstance(yaml_obj, list):
        return [_resolve_anchors(v) for v in yaml_obj]
    if isinstance(yaml_obj, str) and yaml_obj.startswith("${") and yaml_obj.endswith("}"):
        anchor = yaml_obj[2:-1].strip()
        # caller must inject the resolved anchor via context — for simplicity
        # we only support the top-level anchors declared under special keys.
        # The real expansion is handled in _expand_spec below.
        return yaml_obj
    return yaml_obj


def _expand_spec(spec: dict[str, Any], anchors: dict[str, list[str]]) -> dict[str, Any]:
    """Walk the spec and expand ${anchor} placeholders into real header lists."""
    if "headers" not in spec:
        return spec
    headers = spec["headers"]
    if isinstance(headers, str) and headers.startswith("${") and headers.endswith("}"):
        anchor = headers[2:-1].strip()
        if anchor not in anchors:
            raise ValueError(f"Unknown anchor in org_structure.yaml: {anchor!r}")
        spec = {**spec, "headers": list(anchors[anchor])}
    return spec


# ---------------------------------------------------------------------------
# Bootstrap logic
# ---------------------------------------------------------------------------

def _bootstrap_one(spreadsheet, tab_spec: dict[str, Any], anchors: dict[str, list[str]]) -> dict[str, Any]:
    """Bootstrap a single tab. Returns a small report dict."""
    tab_spec = _expand_spec(tab_spec, anchors)
    name = tab_spec["name"]
    headers = tab_spec.get("headers", [])
    static_rows = tab_spec.get("static_rows", [])
    widths = tab_spec.get("column_widths")

    ws = repository.ensure_tab(spreadsheet, name)
    logger.info("Tab ensured: %s", name)

    if headers:
        repository.write_headers(ws, headers)
        logger.info("  headers written (%d cols)", len(headers))

    if static_rows:
        # Belt-and-suspenders: refuse to write any row that looks like it
        # contains a Discord ID (17–20 digit integer). Nova's static ref
        # data is short strings, codes, and divisions.
        for r in static_rows:
            for cell in r:
                if isinstance(cell, str) and cell.strip().isdigit() and len(cell.strip()) >= 17:
                    raise ValueError(
                        f"Refusing to write Discord-ID-shaped value into static row "
                        f"of tab '{name}': {cell!r}. Static rows must not contain personnel data."
                    )
        repository.append_static_rows(ws, static_rows)
        logger.info("  static rows appended (%d rows)", len(static_rows))

    if widths:
        repository.apply_column_widths(ws, widths)
        logger.info("  column widths applied")

    # Verify no personnel-shaped row slipped in.
    rows = repository.read_all_rows(ws)
    personnel_leaked = 0
    for r in rows:
        for header in ("Discord ID", "Discord USER ID", "Discord User ID"):
            v = (r.get(header) or "").strip()
            if v and v.isdigit() and len(v) >= 17:
                personnel_leaked += 1
                break
    if personnel_leaked:
        raise ValueError(
            f"Personnel data detected in tab '{name}' after bootstrap — aborting."
        )

    return {
        "tab": name,
        "headers": len(headers),
        "static_rows": len(static_rows),
        "personnel_rows_after": 0,
    }


def bootstrap_target_sheet() -> list[dict[str, Any]]:
    """Top-level entry: bootstrap every tab described in org_structure.yaml.

    Returns a list of per-tab report dicts.
    """
    settings = get_settings()
    spec_path = settings.org_structure_path
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if "tabs" not in spec:
        raise ValueError(f"{spec_path} missing 'tabs' key")

    # Pull top-level anchor definitions (the header-set blocks) out of the
    # raw YAML so we can resolve ${...} placeholders.
    anchors = {k: v for k, v in spec.items() if k.endswith("_headers")}

    spreadsheet = sheets_client.open_target_spreadsheet()
    reports: list[dict[str, Any]] = []

    for tab_spec in spec["tabs"]:
        reports.append(_bootstrap_one(spreadsheet, tab_spec, anchors))

    # State tabs (created here too so a fresh deployment has the right
    # schema, even though services will create them lazily).
    for tab_spec in spec.get("state_tabs", []):
        reports.append(_bootstrap_one(spreadsheet, tab_spec, anchors))

    return reports


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap DCRP Target sheet")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report what would be done, without touching the sheet.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.dry_run:
        spec = yaml.safe_load(Path("config/org_structure.yaml").read_text(encoding="utf-8"))
        anchors = {k: v for k, v in spec.items() if k.endswith("_headers")}
        print("Would create the following tabs in the Target sheet:")
        for t in spec["tabs"]:
            t = _expand_spec(t, anchors)
            print(f"  - {t['name']:<30}  status={t['status']:<4}  "
                  f"headers={len(t.get('headers', []))}  static_rows={len(t.get('static_rows', []))}")
        for t in spec.get("state_tabs", []):
            t = _expand_spec(t, anchors)
            print(f"  - {t['name']:<30}  status=state  "
                  f"headers={len(t.get('headers', []))}  static_rows={len(t.get('static_rows', []))}")
        return 0

    try:
        reports = bootstrap_target_sheet()
    except SheetsError as exc:
        logger.error("Bootstrap failed: %s", exc)
        return 2
    except FileNotFoundError as exc:
        logger.error("Missing file: %s", exc)
        return 2
    except ValueError as exc:
        logger.error("Bootstrap refused: %s", exc)
        return 3

    print(f"\nBootstrap complete — {len(reports)} tab(s) ensured.\n")
    for r in reports:
        print(
            f"  {r['tab']:<30}  headers={r['headers']:<3}  "
            f"static_rows={r['static_rows']:<3}  personnel_rows={r['personnel_rows_after']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

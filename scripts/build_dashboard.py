#!/usr/bin/env python3
"""
Render dashboards/tiles.yaml into a Dynatrace Platform dashboard document.

Output format follows the version-17 document shape that the Dynatrace Documents
API accepts (verified against a dashboard exported from a live tenant): tiles keyed
by stringified index, a parallel `layouts` map with x/y/w/h on a 24-column grid,
markdown tiles for section headers, and `type: data` tiles carrying DQL.

Keeping the JSON generated rather than hand-maintained is the point: the layout
arithmetic is mechanical and easy to get wrong (Dynatrace silently overlaps tiles),
and the tests can then assert the committed JSON matches the reviewed queries.

  python3 scripts/build_dashboard.py            # write dashboards/dynatrace-dashboard.json
  python3 scripts/build_dashboard.py --check    # fail if the committed file is stale
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required.\n"
          "  pip3 install pyyaml\n"
          "If that fails with 'externally-managed-environment' (Homebrew or\n"
          "Debian Python), use a virtualenv:\n"
          "  python3 -m venv .venv && .venv/bin/pip install pyyaml\n"
          "  .venv/bin/python <this script>", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "dashboards", "tiles.yaml")
TARGET = os.path.join(ROOT, "dashboards", "dynatrace-dashboard.json")

GRID_WIDTH = 24
SECTION_HEADER_HEIGHT = 2
TILE_HEIGHT = {"singleValue": 4, "table": 8, "areaChart": 6,
               "lineChart": 6, "barChart": 6}
DEFAULT_TILE_HEIGHT = 6
# Visualizations we know the document format accepts. Anything else is likely a
# typo that Dynatrace would render as an empty tile.
VALID_VIZ = set(TILE_HEIGHT)


def visualization_settings(viz: str, field: str | None) -> dict:
    if viz == "singleValue":
        return {"singleValue": {"showTrend": True,
                                "recordField": field,
                                "colorThresholdTarget": "VALUE"}}
    if viz == "table":
        return {"table": {"rowDensity": "condensed", "enableSparklines": False}}
    return {"chartSettings": {"legend": {"hidden": False, "position": "BOTTOM"}}}


def build(spec: dict, service_filter: str | None = None) -> dict:
    service_filter = (service_filter
                      or spec.get("service_filter", 'service.name == "apollo-router"'))
    tiles: dict[str, dict] = {}
    layouts: dict[str, dict] = {}
    index = 0
    row = 0

    for section in spec.get("sections", []):
        title = section.get("title", "").rstrip()
        if title:
            tiles[str(index)] = {"type": "markdown", "title": "", "markdown": title}
            height = int(section.get("height", SECTION_HEADER_HEIGHT))
            layouts[str(index)] = {"x": 0, "y": row, "w": GRID_WIDTH, "h": height}
            index += 1
            row += height

        column = 0
        row_height = 0
        for tile in section.get("tiles", []) or []:
            viz = tile.get("viz", "lineChart")
            if viz not in VALID_VIZ:
                raise ValueError(f"tile {tile.get('name')!r}: unknown viz {viz!r}; "
                                 f"expected one of {sorted(VALID_VIZ)}")
            # {SERVICE} is for `timeseries ..., filter: {expr}` (braces are the
            # inline-filter argument syntax); {SERVICE_EXPR} is the bare condition
            # for a piped `fetch ... | filter <expr>` command, which takes no braces.
            query = tile["query"].replace("{SERVICE}", "{" + service_filter + "}")
            query = query.replace("{SERVICE_EXPR}", service_filter)
            width = int(tile.get("width", 12))
            if width < 1 or width > GRID_WIDTH:
                raise ValueError(f"tile {tile.get('name')!r}: width {width} "
                                 f"outside 1..{GRID_WIDTH}")
            height = int(tile.get("height", TILE_HEIGHT.get(viz, DEFAULT_TILE_HEIGHT)))

            if column + width > GRID_WIDTH:      # wrap to the next row
                row += row_height
                column, row_height = 0, 0

            tiles[str(index)] = {
                "type": "data",
                "title": tile["name"],
                "query": query.strip(),
                "visualization": viz,
                "visualizationSettings": visualization_settings(viz, tile.get("field")),
            }
            layouts[str(index)] = {"x": column, "y": row, "w": width, "h": height}
            index += 1
            column += width
            row_height = max(row_height, height)

        row += row_height

    return {"version": 17, "variables": [], "tiles": tiles, "layouts": layouts}


def assert_no_overlap(dashboard: dict):
    """Dynatrace accepts overlapping tiles and renders them on top of each other,
    so catch it here instead of in the UI."""
    occupied = {}
    for key, box in dashboard["layouts"].items():
        for x in range(box["x"], box["x"] + box["w"]):
            for y in range(box["y"], box["y"] + box["h"]):
                if (x, y) in occupied:
                    raise ValueError(f"tiles {occupied[(x, y)]} and {key} overlap "
                                     f"at ({x},{y})")
                occupied[(x, y)] = key


def render(service_filter: str | None = None) -> str:
    with open(SOURCE) as fh:
        spec = yaml.safe_load(fh)
    dashboard = build(spec, service_filter)
    assert_no_overlap(dashboard)
    return json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed dashboard is out of date")
    # Every tile filters on service.name. If your router reports anything other
    # than "apollo-router", the imported dashboard is silently empty — so make the
    # override a flag rather than a file edit.
    ap.add_argument("--service-name", metavar="NAME",
                    help="service.name your router reports (default: apollo-router)")
    ap.add_argument("--service-filter", metavar="DQL",
                    help="full filter expression, if service.name alone is not enough "
                         "(e.g. 'service.name == \"router\" and k8s.namespace.name == \"prod\"')")
    args = ap.parse_args(argv)

    if args.service_filter and args.service_name:
        print("use --service-name or --service-filter, not both", file=sys.stderr)
        return 2
    service_filter = args.service_filter
    if args.service_name:
        service_filter = f'service.name == "{args.service_name}"'

    rendered = render(service_filter)
    if args.check:
        if service_filter:
            print("--check compares against the committed default; drop the "
                  "service override to use it", file=sys.stderr)
            return 2
        current = open(TARGET).read() if os.path.exists(TARGET) else ""
        if current != rendered:
            print(f"{TARGET} is stale - run scripts/build_dashboard.py",
                  file=sys.stderr)
            return 1
        print("dashboard is up to date")
        return 0

    with open(TARGET, "w") as fh:
        fh.write(rendered)
    payload = json.loads(rendered)
    data_tiles = sum(1 for t in payload["tiles"].values() if t["type"] == "data")
    print(f"wrote {TARGET} ({len(payload['tiles'])} tiles, {data_tiles} with queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

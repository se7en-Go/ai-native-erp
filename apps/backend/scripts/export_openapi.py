from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from erp_core.api_main import create_app


def export_openapi(output_path: Path, *, check_only: bool = False) -> int:
    app = create_app()
    schema = app.openapi()
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if check_only:
        if not output_path.exists():
            print(f"Missing OpenAPI contract file: {output_path}", file=sys.stderr)
            return 1
        existing = output_path.read_text(encoding="utf-8")
        if existing != rendered:
            print(f"OpenAPI contract drift detected at {output_path}", file=sys.stderr)
            return 1
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"Exported OpenAPI 3.1 schema to {output_path}")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[3]
    default_output = repo_root / "contracts" / "openapi.json"
    parser = argparse.ArgumentParser(description="Export FastAPI OpenAPI 3.1 JSON contract.")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return export_openapi(args.output, check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())

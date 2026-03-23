#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import AsyncSessionLocal
from app.memory.graph_import import apply_graph_import_plan, build_plan_from_export


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Import graph memory seeds from an exported memories JSON file.")
    parser.add_argument("export_path", help="Path to a memories export JSON file.")
    parser.add_argument("--include-inactive", action="store_true", help="Also consider inactive memories.")
    parser.add_argument("--apply", action="store_true", help="Persist the import instead of dry-running.")
    args = parser.parse_args()

    export_path = Path(args.export_path)
    if not export_path.exists():
        raise SystemExit(f"Export file not found: {export_path}")

    async with AsyncSessionLocal() as db:
        plan = await build_plan_from_export(db, export_path, include_inactive=args.include_inactive)
        print(f"user_id={plan.user_id}")
        print(f"entity candidates={len(plan.entities)}")
        print(f"relation candidates={len(plan.relations)}")
        print(f"observation candidates={len(plan.observations)}")
        print(f"skipped memories={len(plan.skipped_memories)}")
        if plan.skipped_memories:
            print("skipped memory ids=" + ", ".join(str(item) for item in plan.skipped_memories))

        if not args.apply:
            return

        summary = await apply_graph_import_plan(db, plan)
        await db.commit()
        print("applied:")
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(_main())

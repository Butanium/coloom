"""Dry-run the legacy-setups → templates/generators migration against a COPY of
the live dev db (run before restarting the :4444 server on the new code).

Copies db + WAL sidecars, opens a WeaveStore (which runs the migration), applies
the boot steps create_app would (builtin import from coloom.yaml + seeding), and
prints every profile's generators with their resolved usability.

Run: uv run scripts/small-smokes/smoke_migration_dryrun.py [--db /tmp/coloom-ui-smoke.sqlite]
"""

import argparse
import json
import shutil
from pathlib import Path

from coloom.config import load_config
from coloom.store import WeaveStore

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("/tmp/coloom-ui-smoke.sqlite"))
    parser.add_argument("--config", type=Path, default=REPO / "coloom.yaml")
    parser.add_argument(
        "--copy", type=Path, default=Path("/tmp/coloom-migration-dryrun.sqlite")
    )
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):  # WAL holds un-checkpointed mutations
        src = Path(str(args.db) + suffix)
        if src.exists():
            shutil.copy(src, Path(str(args.copy) + suffix))

    store = WeaveStore(args.copy)  # runs _migrate_setups_to_generators
    config = load_config(args.config)

    # replicate create_app's boot steps
    preset_names = list(config.presets) or list(config.endpoints)
    for name in preset_names:
        endpoint, params = config.resolve_preset(name)
        store.upsert_builtin_template(
            name=name,
            base_url=endpoint.base_url,
            model=endpoint.model,
            api_key=endpoint.api_key,
            api_key_env=endpoint.api_key_env,
            params=params,
        )
    for prof in store.list_profiles():
        store.seed_profile_generators(prof["name"], log=False)

    report = {
        "templates": [
            {"name": t.name, "builtin": t.builtin, "model": t.model}
            for t in store.list_templates()
        ],
        "profiles": {},
    }
    for prof in store.list_profiles():
        gens = store.list_generators(prof["name"])
        report["profiles"][prof["name"]] = [
            {
                "name": g.name,
                "parent": g.parent.model_dump() if g.parent else None,
                "usable": store.resolve_generator(g.id).usable,
                "resolved_model": store.resolve_generator(g.id).model,
            }
            for g in gens
        ]
    store.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

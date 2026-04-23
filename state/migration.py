"""Migration script from state.yaml to SQLite database."""
import asyncio
import argparse
import os
import sys
from pathlib import Path

import psutil
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from state.repository import StateRepository

STATE_YAML_PATH = Path(__file__).resolve().parent.parent / "state.yaml"
STATE_YAML_BACKUP = Path(__file__).resolve().parent.parent / "state.yaml.backup"


def is_app_running() -> bool:
    """Check if WeiboMonitor app is currently running."""
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = ' '.join(proc.info['cmdline'] or [])
                if 'main.py' in cmdline or 'core/main' in cmdline:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _ensure_backup_exists() -> bool:
    """Ensure state.yaml is backed up before migration. Returns False if backup already exists."""
    if STATE_YAML_BACKUP.exists():
        return False
    if STATE_YAML_PATH.exists():
        STATE_YAML_PATH.rename(STATE_YAML_BACKUP)
        return True


async def migrate_from_yaml() -> bool:
    """Migrate state from YAML to SQLite database."""
    print("=" * 50)
    print("WeiboMonitor State Migration")
    print("=" * 50)

    if is_app_running():
        print("ERROR: WeiboMonitor is running. Please stop it before migration.")
        print("Run: python core/main.py (in a separate terminal to stop)")
        return False

    if not STATE_YAML_PATH.exists():
        print("state.yaml not found, skipping migration")
        print("The database will be created fresh on next run.")
        return True

    with open(STATE_YAML_PATH, "r", encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}

    accounts = yaml_data.get("accounts", {})
    if not accounts:
        print("No accounts found in state.yaml, skipping migration")
        _ensure_backup_exists()
        print(f"Empty state.yaml backed up as {STATE_YAML_BACKUP}")
        return True

    repo = StateRepository()
    await repo.initialize()

    migrated_count = 0
    for weiboid, data in accounts.items():
        latest_id = data.get("latest_id", "")
        if latest_id:
            await repo.set_latest_id(weiboid, latest_id)
            print(f"Migrated: {weiboid} -> {latest_id}")
            migrated_count += 1

    print(f"\nMigration complete: {migrated_count} accounts migrated")

    if not _ensure_backup_exists():
        print(f"Warning: {STATE_YAML_BACKUP} already exists")
        if STATE_YAML_PATH.exists():
            # Backup failed, but yaml still exists - rename with timestamp
            import time
            ts_backup = STATE_YAML_PATH.with_suffix(f'.yaml.backup.{int(time.time())}')
            STATE_YAML_PATH.rename(ts_backup)
            print(f"Migrated yaml renamed to {ts_backup}")

    return True


async def rollback_to_yaml() -> bool:
    """Rollback from SQLite database to state.yaml."""
    print("=" * 50)
    print("WeiboMonitor State Rollback")
    print("=" * 50)

    if is_app_running():
        print("ERROR: WeiboMonitor is running. Please stop it before rollback.")
        return False

    if not STATE_YAML_BACKUP.exists():
        print(f"ERROR: {STATE_YAML_BACKUP} not found. Cannot rollback.")
        return False

    repo = StateRepository()
    await repo.initialize()

    # Export all account states from database
    async with repo._connect() as db:
        async with db.execute("SELECT weiboid, latest_id FROM account_state") as cursor:
            accounts = {row[0]: {"latest_id": row[1]} async for row in cursor}

    yaml_data = {"accounts": accounts}

    # Write to state.yaml
    with open(STATE_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(yaml_data, f, allow_unicode=True)

    print(f"Rollback complete: {len(accounts)} accounts restored to state.yaml")

    # Remove backup
    STATE_YAML_BACKUP.unlink()
    print(f"Backup file {STATE_YAML_BACKUP} removed")

    return True


def main():
    parser = argparse.ArgumentParser(description="WeiboMonitor State Migration Tool")
    parser.add_argument(
        "action",
        choices=["migrate", "rollback"],
        help="Action to perform: migrate (yaml->sqlite) or rollback (sqlite->yaml)"
    )
    args = parser.parse_args()

    if args.action == "migrate":
        success = asyncio.run(migrate_from_yaml())
    else:
        success = asyncio.run(rollback_to_yaml())

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
"""Migration script from state.yaml to SQLite database."""
import asyncio
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
        STATE_YAML_PATH.rename(STATE_YAML_BACKUP)
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

    if STATE_YAML_BACKUP.exists():
        print(f"Warning: {STATE_YAML_BACKUP} already exists, not overwriting")
    else:
        STATE_YAML_PATH.rename(STATE_YAML_BACKUP)
        print(f"Original file backed up as {STATE_YAML_BACKUP}")

    return True


if __name__ == "__main__":
    success = asyncio.run(migrate_from_yaml())
    sys.exit(0 if success else 1)
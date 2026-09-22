"""
Weekly Snapshot Creator

Runs alongside weekly_retrain.py on Sunday night.

Creates a complete snapshot of:
- Full trader-v2 source code
- Database
- Models (current + history)
- Trade data
- Config files

Saves to:
- /home/ralph/trader-v2-snapshots/snapshot-{date}/
- /mnt/file-cabinet/trader-v2/snapshots/snapshot-{date}/

Each snapshot has its own SNAPSHOT_README.md describing what's inside.
Keeps last 100 snapshots on NAS.
"""
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

V2_ROOT = Path("/home/ralph/trader-v2")
LOCAL_SNAPSHOTS = Path("/home/ralph/trader-v2-snapshots")
NAS_SNAPSHOTS = Path("/mnt/file-cabinet/trader-v2/snapshots")

MAX_SNAPSHOTS = 100


def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def cleanup_old_snapshots(root: Path):
    """Keep only the most recent MAX_SNAPSHOTS snapshots."""
    if not root.exists():
        return
    snapshots = sorted([d for d in root.iterdir() if d.is_dir() and d.name.startswith("snapshot-")],
                       key=lambda p: p.stat().st_mtime)
    if len(snapshots) > MAX_SNAPSHOTS:
        for old in snapshots[:-MAX_SNAPSHOTS]:
            shutil.rmtree(old)
            print(f"  Cleaned up old snapshot: {old.name}")


def make_snapshot(target_root: Path):
    """Create a complete snapshot at target_root/snapshot-{timestamp}/."""
    snapshot_name = f"snapshot-{timestamp()}"
    snapshot_dir = target_root / snapshot_name
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Copy trader-v2 (without data/)
    src_dir = snapshot_dir / "trader-v2"
    shutil.copytree(V2_ROOT, src_dir, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".git", "logs", "*.log"
    ))

    # Copy database
    shutil.copy(V2_ROOT / "data" / "trader.db", snapshot_dir / "trader.db")

    # Copy models
    if (V2_ROOT / "models").exists():
        shutil.copytree(V2_ROOT / "models", snapshot_dir / "models")

    # Copy service files
    service_dir = Path("/home/ralph/.config/systemd/user")
    if service_dir.exists():
        for svc in ["trader-v2.service", "rh-sync.service"]:
            svc_path = service_dir / svc
            if svc_path.exists():
                shutil.copy(svc_path, snapshot_dir / svc)

    # Manifest
    manifest = f"""# Snapshot — {snapshot_name}

Created: {datetime.now().isoformat()}

## Contents
- trader-v2/ — Full source code (excluding __pycache__, logs)
- trader.db — SQLite database snapshot
- models/ — All ML models (current + backups)
- trader-v2.service — Systemd unit file
- rh-sync.service — Robinhood sync service unit

## Restore
1. systemctl --user stop trader-v2.service
2. systemctl --user stop rh-sync.service
3. rm -rf /home/ralph/trader-v2 /home/ralph/trader-v2/data/trader.db
4. cp -r {snapshot_dir}/trader-v2 /home/ralph/trader-v2
5. cp {snapshot_dir}/trader.db /home/ralph/trader-v2/data/trader.db
6. cp -r {snapshot_dir}/models /home/ralph/trader-v2/models
7. systemctl --user start trader-v2.service
8. systemctl --user start rh-sync.service

## Auto-cleanup
This snapshot directory will be automatically deleted if more than
{MAX_SNAPSHOTS} newer snapshots exist. Storage target: ~100 snapshots.
"""
    with open(snapshot_dir / "SNAPSHOT_README.md", 'w') as f:
        f.write(manifest)

    return snapshot_dir


def main():
    print(f"Creating weekly snapshots at {datetime.now().isoformat()}")

    LOCAL_SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    NAS_SNAPSHOTS.mkdir(parents=True, exist_ok=True)

    print("\nLocal snapshot:")
    local_dir = make_snapshot(LOCAL_SNAPSHOTS)
    print(f"  {local_dir}")

    print("\nNAS snapshot:")
    nas_dir = make_snapshot(NAS_SNAPSHOTS)
    print(f"  {nas_dir}")

    print("\nCleaning up old snapshots...")
    cleanup_old_snapshots(LOCAL_SNAPSHOTS)
    cleanup_old_snapshots(NAS_SNAPSHOTS)

    print("\nDone")


if __name__ == "__main__":
    main()
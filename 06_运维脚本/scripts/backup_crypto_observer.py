"""Create a consistent SQLite backup, including committed WAL contents."""
import argparse
import sqlite3
from pathlib import Path


def backup(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or output.exists():
        raise ValueError("Source must exist and backup destination must be new")
    output.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri()+"?mode=ro", uri=True)
    # Exclusive creation protects an existing backup from accidental replacement.
    with output.open("xb"):
        pass
    dst = sqlite3.connect(output)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
    finally:
        dst.close()
        src.close()
    output.chmod(0o600)
    return str(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(backup(args.db, args.output))

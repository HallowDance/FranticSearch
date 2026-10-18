"""Command line: ``python -m franticsearch <command>``.

  serve            run the development web server
  migrate          create/upgrade the database schema
  sync [--force] [--file PATH]
                   update the Scryfall card cache (or import a local bulk file)
  check            verify stock against the ledger (exit 1 on mismatch)
"""

from __future__ import annotations

import argparse
import os
import sys

from . import db, ledger, scryfall


def _db_path(args: argparse.Namespace) -> str:
    return args.db or os.environ.get("FS_DB", os.path.join("data", "franticsearch.db"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="franticsearch")
    parser.add_argument("--db", help="SQLite file (default: $FS_DB or data/franticsearch.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the development web server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=6602)
    p_serve.add_argument("--no-auto-sync", action="store_true")

    sub.add_parser("migrate", help="create/upgrade the database schema")

    p_sync = sub.add_parser("sync", help="update the Scryfall card cache")
    p_sync.add_argument("--force", action="store_true", help="re-import even if unchanged")
    p_sync.add_argument("--file", help="import a local Scryfall bulk JSON file instead")

    sub.add_parser("check", help="verify stock against the ledger")

    args = parser.parse_args(argv)
    path = _db_path(args)

    if args.cmd == "serve":
        from .app import create_app

        app = create_app({"DATABASE": path, **({"AUTO_SYNC": False} if args.no_auto_sync else {})})
        app.run(host=args.host, port=args.port)
        return 0

    conn = db.connect(path)
    applied = db.migrate(conn)
    if args.cmd == "migrate":
        print("applied: " + ", ".join(applied) if applied else "schema up to date")
        return 0
    if args.cmd == "sync":
        if args.file:
            with open(args.file, "rb") as fp:
                n = scryfall.import_stream(conn, fp, progress=lambda n: print(f"\r{n} cards", end=""))
            print(f"\nimported {n} printings from {args.file}")
        else:
            print(scryfall.sync(conn, force=args.force))
        return 0
    if args.cmd == "check":
        problems = ledger.check(conn)
        if not problems:
            print("OK: stock matches the ledger")
            return 0
        for p in problems:
            print(p)
        print(f"{len(problems)} mismatch(es)", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

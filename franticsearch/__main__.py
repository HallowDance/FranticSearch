"""Command line: ``python -m franticsearch <command>``.

  serve            run the development web server
  migrate          create/upgrade the database schema
  sync [--force] [--file PATH]
                   update the Scryfall card cache (or import a local bulk file)
  check            verify stock against the ledger (exit 1 on mismatch)
  user add NAME [--admin] [--password-stdin]
  user passwd NAME [--password-stdin]
  user list        manage accounts (the first admin adopts data from before users)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import db, ledger, scryfall, users


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

    p_user = sub.add_parser("user", help="manage accounts")
    usub = p_user.add_subparsers(dest="ucmd", required=True)
    for name, help_ in (("add", "create an account"), ("passwd", "set an account's password")):
        p = usub.add_parser(name, help=help_)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        if name == "add":
            p.add_argument("--admin", action="store_true")
    usub.add_parser("list", help="list accounts")

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
    if args.cmd == "user":
        return _user_cmd(conn, args)
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


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat: "):
        raise users.LedgerError("passwords don't match")
    return pw


def _user_cmd(conn, args: argparse.Namespace) -> int:
    try:
        if args.ucmd == "list":
            for u in users.list_all(conn):
                print(f"{u['username']:<20} {'admin' if u['is_admin'] else '':<6}"
                      f" {u['locations']:>4} locations {u['copies']:>6} copies")
            return 0
        if args.ucmd == "add":
            first_admin = args.admin and not any(u["is_admin"] for u in users.list_all(conn))
            users.create(conn, args.name, _read_password(args), is_admin=args.admin)
            print(f"created {args.name}" + (" (admin; adopted existing data)" if first_admin else ""))
            return 0
        user = users.by_name(conn, args.name)
        if user is None:
            raise users.LedgerError(f"no user {args.name!r}")
        users.set_password(conn, user["id"], _read_password(args))
        print(f"password set for {args.name}")
        return 0
    except users.LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

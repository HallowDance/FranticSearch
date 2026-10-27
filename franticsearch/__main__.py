"""Command line: ``python -m franticsearch <command>``.

  serve            run the development web server
  migrate          create/upgrade the database schema
  sync [--force] [--file PATH]
                   update the Scryfall card cache (or import a local bulk file)
  check            verify stock against the ledger (exit 1 on mismatch)
  backup [--dir DIR] [--keep N] [--user-data]
                   gzipped online backup (default: <db dir>/backups, keep 14);
                   --user-data leaves out the card cache (<db dir>/backups/user-data)
  offsite --key PUBKEY --dest HOST:DIR|DIR [--keep N] [--local-keep N]
                   user-data backup, encrypted with gpg and copied off the machine
  restore FILE [--force] [--port N]
                   replace the database with a backup (stop the server first)
  user add NAME [--admin] [--password-stdin]
  user passwd NAME [--password-stdin]
  user disable NAME | user enable NAME
  user list        manage accounts (the first admin adopts data from before users)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import backup, db, ledger, logs, offsite, scryfall, users


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

    p_backup = sub.add_parser("backup", help="write a gzipped backup of the database")
    p_backup.add_argument("--dir", help="backup directory (default: <db dir>/backups)")
    p_backup.add_argument("--keep", type=int, default=backup.KEEP, help="backups to keep (default 14)")
    p_backup.add_argument("--user-data", action="store_true",
                          help="leave out the card cache (default dir: <db dir>/backups/user-data)")

    p_off = sub.add_parser("offsite", help="encrypted user-data backup, copied off the machine")
    p_off.add_argument("--key", required=True, help="gpg public key file to encrypt to")
    p_off.add_argument("--dest", required=True, help="HOST:DIR (over SSH) or a local directory")
    p_off.add_argument("--keep", type=int, default=offsite.KEEP, help="copies to keep there (default 30)")
    p_off.add_argument("--local-keep", type=int, default=backup.KEEP,
                       help="user-data backups to keep here (default 14)")

    p_restore = sub.add_parser("restore", help="replace the database with a backup")
    p_restore.add_argument("file", help="a .db or .db.gz backup")
    p_restore.add_argument("--force", action="store_true", help="even if the server port is in use")
    p_restore.add_argument("--port", type=int, default=6602, help="server port to check (default 6602)")

    p_user = sub.add_parser("user", help="manage accounts")
    usub = p_user.add_subparsers(dest="ucmd", required=True)
    for name, help_ in (("add", "create an account"), ("passwd", "set an account's password")):
        p = usub.add_parser(name, help=help_)
        p.add_argument("name")
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        if name == "add":
            p.add_argument("--admin", action="store_true")
    for name, help_ in (("disable", "block an account's login and hide what it shared"),
                        ("enable", "let a disabled account log in again")):
        usub.add_parser(name, help=help_).add_argument("name")
    usub.add_parser("list", help="list accounts")

    args = parser.parse_args(argv)
    path = _db_path(args)
    logs.configure()

    if args.cmd == "serve":
        from .app import create_app

        app = create_app({"DATABASE": path, **({"AUTO_SYNC": False} if args.no_auto_sync else {})})
        app.run(host=args.host, port=args.port)
        return 0

    if args.cmd in ("backup", "restore", "offsite"):
        return _backup_cmd(path, args)

    conn = db.connect(path)
    applied = db.migrate(conn)
    db.optimize(conn)
    if args.cmd == "migrate":
        print("applied: " + ", ".join(applied) if applied else "schema up to date")
        return 0
    if args.cmd == "sync":
        if args.file:
            with open(args.file, "rb") as fp:
                n = scryfall.import_stream(conn, fp, progress=lambda n: print(f"\r{n} cards", end=""))
            db.optimize(conn, checkpoint=True)
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


def _backup_cmd(path: str, args: argparse.Namespace) -> int:
    try:
        if args.cmd == "backup":
            if args.keep < 1:
                raise backup.BackupError("--keep must be at least 1")
            out = backup.backup(path, args.dir, args.keep, user_data=args.user_data)
            print(f"backup written: {out} ({out.stat().st_size / 1e6:.1f} MB)")
            return 0
        if args.cmd == "offsite":
            if args.local_keep < 1:
                raise backup.BackupError("--local-keep must be at least 1")
            out = backup.backup(path, keep=args.local_keep, user_data=True)
            r = offsite.push(out, args.key, args.dest, args.keep)
            print(f"{r['file']} copied to {r['dest']}"
                  + (f"; removed {len(r['removed'])} old cop{'y' if len(r['removed']) == 1 else 'ies'}"
                     if r["removed"] else ""))
            return 0
        r = backup.restore(path, args.file, force=args.force, port=args.port)
    except (backup.BackupError, offsite.OffsiteError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if r["saved"]:
        print(f"previous database saved as {r['saved']}")
    print(f"restored {args.file} into {path}")
    if r["migrations"]:
        print("applied migrations: " + ", ".join(r["migrations"]))
    if r["problems"]:
        print(f"WARNING: {len(r['problems'])} stock/ledger mismatch(es); run `check`", file=sys.stderr)
        return 1
    print("OK: stock matches the ledger")
    if r["user_data"]:
        print("This backup has no card cache: run `python -m franticsearch sync` "
              "(the server also syncs on start) to download it again.")
    return 0


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
                      f" {u['locations']:>4} locations {u['copies']:>6} copies"
                      + ("  DISABLED" if u["disabled"] else ""))
            return 0
        if args.ucmd == "add":
            first_admin = args.admin and not any(u["is_admin"] for u in users.list_all(conn))
            users.create(conn, args.name, _read_password(args), is_admin=args.admin)
            print(f"created {args.name}" + (" (admin; adopted existing data)" if first_admin else ""))
            return 0
        user = users.by_name(conn, args.name)
        if user is None:
            raise users.LedgerError(f"no user {args.name!r}")
        if args.ucmd in ("disable", "enable"):
            users.set_disabled(conn, user["id"], args.ucmd == "disable")
            print(f"{args.name} {args.ucmd}d")
            return 0
        users.set_password(conn, user["id"], _read_password(args))
        print(f"password set for {args.name}")
        return 0
    except users.LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

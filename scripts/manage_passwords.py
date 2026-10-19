#!/usr/bin/env python3
"""Change passwords locally without putting them in arguments or shell history.

Stop the backend first. Resetting a password alone does not revoke JWTs;
rotate JWT_SECRET_KEY separately when existing sessions must be invalidated.
"""

import argparse
import asyncio
import getpass
from pathlib import Path
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.auth.password_admin import PasswordAdminError


async def run(args, password=None):
    from app.auth.password_admin import generated_password, reset_password, rotate_default_passwords, write_private_credentials
    from app.db.session import AsyncSessionLocal, engine

    try:
        async with AsyncSessionLocal() as db:
            if args.rotate_defaults:
                names = await rotate_default_passwords(db, args.output_file)
            else:
                if args.output_file:
                    password = generated_password()
                    write_private_credentials(args.output_file, {args.username: password})
                await reset_password(db, args.username, password)
                names = [args.username]
            await db.commit()
        print("Passwords updated for: " + (", ".join(names) or "none"))
        if names and args.output_file:
            print(f"Private credentials file: {args.output_file.resolve()}")
        print("Existing JWTs are not revoked by a password reset alone.")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--username")
    targets.add_argument("--rotate-defaults", action="store_true")
    parser.add_argument("--output-file", type=Path, help="Generate passwords into a NEW owner-only JSON file, never stdout")
    args = parser.parse_args()
    if args.rotate_defaults and not args.output_file:
        parser.error("--rotate-defaults requires --output-file")
    password = None
    if args.username and not args.output_file:
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            parser.error("An interactive terminal is required for hidden password entry")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("New password (at least 16 characters): ")
                confirmation = getpass.getpass("Confirm new password: ")
            except getpass.GetPassWarning:
                parser.error("Hidden password entry is unavailable; use a working interactive terminal")
        if password != confirmation:
            parser.error("Passwords do not match")
    try:
        asyncio.run(run(args, password))
    except PasswordAdminError as exc:
        parser.exit(1, f"{exc}\n")
    except Exception:
        # Database exception strings can contain connection parameters.
        parser.exit(1, "Password update failed; no secret details were printed. Verify the account, database and recovery-file path.\n")


if __name__ == "__main__":
    main()

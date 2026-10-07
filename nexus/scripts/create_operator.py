"""Create the account that is allowed to use the demo.

    python -m nexus.scripts.create_operator --username judge --display-name "评审账号"

The password is read from a TTY, or piped in, or generated. It is never taken
as a command-line argument on purpose: ``--password`` puts it in the shell
history, in ``ps`` output visible to every other user on the machine, and in
this transcript of what the operator typed. Anything that passes a secret as an
argv has already leaked it.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import string
import sys

from sqlalchemy import select

from backend.core import auth as auth_module
from backend.core import database
from backend.core.models import OperatorAccount


def _generate() -> str:
    """A password worth having, built from a CSPRNG.

    24 characters over a 72-symbol alphabet is ~146 bits, which is far past the
    point where length stops mattering. Generated rather than composed from
    wordlists on purpose: ``Nexus@2026!`` is guessable, and a generator cannot
    be nudged into repeating the product name.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(24))
        # Make the shape a human can retype from a printout without weakening it.
        if (any(c.islower() for c in candidate) and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(c in "!@#$%^&*-_=+" for c in candidate)):
            return candidate


def _read_password(generate: bool) -> str:
    if generate or not sys.stdin.isatty():
        if generate:
            return _generate()
        # Piped input, e.g. from a secret manager. Read a whole line.
        piped = sys.stdin.readline().rstrip("\n")
        if not piped:
            raise SystemExit("没有读到口令。使用 --generate 让工具生成一个。")
        return piped
    first = getpass.getpass("口令（不回显）: ")
    again = getpass.getpass("再输一次: ")
    if first != again:
        raise SystemExit("两次输入不一致。")
    return first


async def create(username: str, display_name: str, password: str, force: bool) -> int:
    # A fresh server has no schema yet. Creating the account is usually the very
    # first thing an operator does, and "no such table: operator_accounts" is
    # a confusing first impression for a script whose whole job is one insert.
    await database.init_db()
    async with database.session_scope() as session:
        existing = await session.scalar(
            select(OperatorAccount).where(OperatorAccount.username == username))
        if existing is not None and not force:
            print(f"账号 {username!r} 已存在。要覆盖口令请加 --force。", file=sys.stderr)
            return 1
        digest = auth_module.hash_password(password)
        if existing is not None:
            existing.password_hash = digest
            existing.display_name = display_name or existing.display_name
            existing.is_active = True
            print(f"已重置账号 {username!r} 的口令。")
        else:
            session.add(OperatorAccount(
                username=username, display_name=display_name or username,
                password_hash=digest, is_active=True,
            ))
            print(f"已创建账号 {username!r}。")
        # Never the password, at any log level.
        logger_line = f"operator_created username={username}"
        print(logger_line, file=sys.stderr)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="创建一个可以登录 AI 银行管家的账号")
    parser.add_argument("--username", required=True, help="登录名")
    parser.add_argument("--display-name", default="", help="页面上显示的名字")
    parser.add_argument("--generate", action="store_true",
                        help="由工具生成一个 24 位强口令（推荐）")
    parser.add_argument("--force", action="store_true", help="账号已存在时覆盖口令")
    args = parser.parse_args()

    password = _read_password(args.generate)
    try:
        return asyncio.run(create(args.username.strip(), args.display_name.strip(),
                                  password, args.force))
    finally:
        # Do not leave the plaintext sitting in a frame while we unwind.
        del password


if __name__ == "__main__":
    raise SystemExit(main())

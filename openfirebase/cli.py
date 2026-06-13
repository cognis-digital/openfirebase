"""openfirebase command-line interface.

Subcommands
-----------
* ``serve``  - start the local HTTP server exposing all services
* ``get``    - read a value from the realtime tree (REST convenience, local)
* ``set``    - write a value to the realtime tree
* ``version``- print version
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .server import serve_forever, App


def _cmd_serve(args) -> int:
    serve_forever(
        host=args.host,
        port=args.port,
        data_dir=None if args.memory else args.data_dir,
        public_dir=args.public,
        secret=args.secret,
        spa_fallback=args.spa,
    )
    return 0


def _cmd_set(args) -> int:
    app = App(data_dir=None if args.memory else args.data_dir)
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    app.rtdb.set(args.path, value)
    print(json.dumps(app.rtdb.get(args.path)))
    return 0


def _cmd_get(args) -> int:
    app = App(data_dir=None if args.memory else args.data_dir)
    print(json.dumps(app.rtdb.get(args.path)))
    return 0


def _cmd_version(args) -> int:
    print(f"openfirebase {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openfirebase",
        description="Local reimplementation of core Firebase developer primitives.",
    )
    parser.add_argument("--data-dir", default=".openfirebase",
                        help="directory for persistent sqlite storage")
    parser.add_argument("--memory", action="store_true",
                        help="use an in-memory store (no persistence)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="start the local HTTP server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--public", default=None,
                         help="directory of static files to host")
    p_serve.add_argument("--secret", default=None,
                         help="HMAC secret for local auth tokens")
    p_serve.add_argument("--spa", action="store_true",
                         help="SPA fallback to index.html for unknown paths")
    p_serve.set_defaults(func=_cmd_serve)

    p_set = sub.add_parser("set", help="set a value in the realtime tree")
    p_set.add_argument("path")
    p_set.add_argument("value", help="JSON value (or raw string)")
    p_set.set_defaults(func=_cmd_set)

    p_get = sub.add_parser("get", help="get a value from the realtime tree")
    p_get.add_argument("path")
    p_get.set_defaults(func=_cmd_get)

    p_ver = sub.add_parser("version", help="print version")
    p_ver.set_defaults(func=_cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

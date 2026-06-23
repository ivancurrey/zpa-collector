"""Command-line entrypoint (argparse dispatch): run | --selftest | synth-send."""
import argparse
import asyncio

from collector import app
from collector import selftest
from collector import synth_sender
from collector.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector",
                                     description="ZPA Hygiene Collector")
    parser.add_argument("--selftest", action="store_true",
                        help="run the loopback self-test and exit")
    sub = parser.add_subparsers(dest="command")

    synth = sub.add_parser("synth-send",
                           help="send synthetic LSS lines to a receiver")
    synth.add_argument("--host", required=True)
    synth.add_argument("--port", type=int, required=True)
    synth.add_argument("--count", type=int, default=None,
                       help="number of lines to send (default: all built-in)")
    synth.add_argument("--no-tls", action="store_true",
                       help="send plaintext (no TLS) for local smoke tests")

    args = parser.parse_args(argv)

    if args.selftest:
        return selftest.run_selftest()

    if args.command == "synth-send":
        lines = synth_sender.default_lines()
        if args.count is not None:
            lines = lines[:args.count]
        synth_sender.send(args.host, args.port, lines, tls=not args.no_tls)
        return 0

    # default: run the collector
    asyncio.run(app.run(load_settings()))
    return 0

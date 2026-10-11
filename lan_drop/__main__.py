"""CLI entry point; default to the browser-free desktop application."""

import argparse
import json
import sys
from pathlib import Path

from .server import local_addresses

ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
DEFAULT_DATA = ROOT / "邻传数据" if getattr(sys, "frozen", False) else ROOT / "outputs" / "lan-drop"


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from .installer import run_helper

        run_helper(sys.argv[2])
        return
    parser = argparse.ArgumentParser(description="邻传 · 局域网桌面互传")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--name")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        from .selftest import run

        run(args.data_dir, host=args.host)
    elif args.headless:
        from .peer import PeerNode

        addresses = local_addresses()
        node = PeerNode(
            args.host or (addresses[0] if addresses else "127.0.0.1"),
            args.port,
            args.data_dir,
            args.name,
        )
        if sys.stdout:
            print(
                json.dumps({"host": node.host, **node.identity()}, ensure_ascii=False), flush=True
            )
        try:
            node.closed.wait()
        except KeyboardInterrupt:
            pass
        finally:
            node.close()
    else:
        from .desktop import run

        run(args)


if __name__ == "__main__":
    main()

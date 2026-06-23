"""`python -m collector` entrypoint."""
import sys

from collector.cli import main

if __name__ == "__main__":
    sys.exit(main())

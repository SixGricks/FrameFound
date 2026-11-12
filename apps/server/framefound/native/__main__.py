"""`python -m framefound.native …` — what the `framefound` command runs."""

import sys

from framefound.native.cli import main

if __name__ == "__main__":
    sys.exit(main())

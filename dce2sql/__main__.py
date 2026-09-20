"""Entry point for ``python -m dce2sql``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

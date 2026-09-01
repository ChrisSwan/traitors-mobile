"""Entry point: ``python -m traitors_sim`` routes to traitors_mobile.integration.main."""

import sys

from traitors_mobile.integration import main

if __name__ == "__main__":
    sys.exit(main())

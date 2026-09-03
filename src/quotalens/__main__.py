"""``python -m quotalens`` entry point, used by the detached service process."""

import sys

from quotalens.cli import main

sys.exit(main())

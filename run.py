"""Entry point for the .exe build and for autostart (works from any working directory)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pc_agent.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

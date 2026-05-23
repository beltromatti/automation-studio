"""PyInstaller entrypoint for the frozen backend. Dispatches the same modes as
the `automation-backend` console script (api / control-server / run-workflow /
reap / hb), so the single frozen executable can re-invoke itself."""
import sys

from orchestrator.cli import main

if __name__ == "__main__":
    sys.exit(main())

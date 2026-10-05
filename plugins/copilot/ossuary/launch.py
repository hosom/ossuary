"""Start a source checkout even when Copilot installs only the plugin subdirectory."""

from __future__ import annotations

import os
from pathlib import Path


def main() -> None:
    configured = os.environ.get("OSSUARY_PROJECT")
    project = (
        Path(configured).expanduser().resolve()
        if configured
        else Path(__file__).resolve().parents[3]
    )
    if not (
        (project / "pyproject.toml").is_file()
        and (project / "src" / "ossuary" / "mcp_server.py").is_file()
        and (project / "uv.lock").is_file()
    ):
        raise SystemExit(
            f"Ossuary checkout not found at {project}. Copilot may have copied "
            "only the plugin directory. Set OSSUARY_PROJECT to the absolute path "
            "of your Ossuary checkout before starting Copilot, then restart it."
        )
    try:
        os.execvp("uv", ["uv", "run", "--project", str(project), "ossuary-mcp"])
    except OSError as exc:
        raise SystemExit(f"Could not start Ossuary with uv: {exc}") from exc


if __name__ == "__main__":
    main()

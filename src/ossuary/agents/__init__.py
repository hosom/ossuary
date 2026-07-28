"""The two agents: A investigates one session, B clusters across the corpus.

Both are backend-independent: prompts come from `agents.yaml`, tools from
`agents.tools`, and whoever runs the inference from `ossuary.backends`.
"""

from .clusterer import build_clusterer_backend
from .deps import ClustererDeps, ScannerDeps
from .scanner import build_scanner_backend, scanner_prompt
from .tools import clusterer_tools, scanner_tools

__all__ = [
    "ClustererDeps",
    "ScannerDeps",
    "build_clusterer_backend",
    "build_scanner_backend",
    "clusterer_tools",
    "scanner_prompt",
    "scanner_tools",
]

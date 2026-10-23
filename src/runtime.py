"""Process-wide runtime singletons shared across entry points.

get_worker_manager used to live in main.py, but ui.py imported it via
`from main import get_worker_manager`. When the app is started with
`python main.py`, that import re-executes main.py under a fresh module
namespace (distinct from `__main__`), where _worker_manager is still
None — so every UI toggle silently skipped history loading with no log
line at all. Keeping the holder in an importable module removes the
duplicate-__main__ trap.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.worker.manager import WorkerManager

_worker_manager: "WorkerManager | None" = None


def set_worker_manager(manager: "WorkerManager") -> None:
    """Register the live WorkerManager instance for this process."""
    global _worker_manager
    _worker_manager = manager


def get_worker_manager() -> "WorkerManager | None":
    """Return the live WorkerManager, or None before startup registration."""
    return _worker_manager

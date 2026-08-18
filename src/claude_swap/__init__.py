"""Multi-account switcher for Claude Code."""

from importlib.metadata import PackageNotFoundError, version

#: Distribution names this package may be installed under, newest first.
#:
#: The DISTRIBUTION was renamed claude-swap -> cfuel; the IMPORT package was
#: deliberately left alone, because the on-disk data directory is derived from
#: it and renaming that would orphan every stored account. Both names can
#: therefore legitimately be present, and a hard lookup on either one alone
#: raises PackageNotFoundError at import time — which takes the whole tool
#: down before it can print anything useful.
_DIST_NAMES = ("cfuel", "claude-swap")


def _installed_version() -> str:
    for name in _DIST_NAMES:
        try:
            return version(name)
        except PackageNotFoundError:
            continue
    # Running straight from a source tree with nothing installed. Not a
    # version anyone should ship, but not a reason to refuse to start.
    return "0+unknown"


__version__ = _installed_version()

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]

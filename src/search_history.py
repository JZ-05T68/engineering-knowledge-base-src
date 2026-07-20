"""Browser history synchronization for the Streamlit search page."""

from __future__ import annotations


def search_history_reload_html() -> str:
    """Reload the local search page when browser back/forward changes its URL."""

    return """
    <script>
    (() => {
      try {
        const hostWindow = window.parent;
        if (hostWindow.__ekbSearchPopstateHandler) {
          hostWindow.removeEventListener(
            "popstate", hostWindow.__ekbSearchPopstateHandler
          );
        }
        const handler = () => hostWindow.location.reload();
        hostWindow.__ekbSearchPopstateHandler = handler;
        hostWindow.addEventListener("popstate", handler);
      } catch (_error) {
        // URL refresh remains available even if the optional listener cannot attach.
      }
    })();
    </script>
    """


__all__ = ["search_history_reload_html"]

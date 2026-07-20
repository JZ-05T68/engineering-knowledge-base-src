"""Optional browser-side keyboard shortcuts for the Streamlit review page."""

from __future__ import annotations


def review_shortcuts_html() -> str:
    """Return a fail-safe shortcut listener that delegates to visible buttons."""

    return """
    <script>
    (() => {
      try {
        const hostWindow = window.parent;
        const hostDocument = hostWindow.document;
        if (hostWindow.__ekbReviewShortcutHandler) {
          hostDocument.removeEventListener(
            "keydown", hostWindow.__ekbReviewShortcutHandler, true
          );
        }
        const clickButton = (label) => {
          const button = Array.from(hostDocument.querySelectorAll("button")).find(
            (item) => item.textContent.trim() === label && !item.disabled
          );
          if (!button) return false;
          button.click();
          return true;
        };
        const handler = (event) => {
          if (event.repeat || event.isComposing) return;
          const target = event.target;
          const isEditing = Boolean(
            target && target.closest &&
            target.closest("textarea,input,[contenteditable='true']")
          );
          let label = null;
          if ((event.ctrlKey || event.metaKey) && !event.altKey &&
              event.key.toLowerCase() === "s") {
            label = "保存草稿";
          } else if ((event.ctrlKey || event.metaKey) && !event.altKey &&
                     event.key === "Enter") {
            label = "保存、复核并进入下一页";
          } else if (event.altKey && !event.ctrlKey && !event.metaKey && !isEditing &&
                     event.key === "ArrowRight") {
            label = "下一待处理页";
          } else if (event.altKey && !event.ctrlKey && !event.metaKey && !isEditing &&
                     event.key === "ArrowLeft") {
            label = "上一待处理页";
          }
          if (label && isEditing &&
              (label === "保存草稿" || label === "保存、复核并进入下一页")) {
            // Streamlit synchronizes a focused text area during focus change. Let that
            // browser event finish before invoking the same visible action as a click.
            target.blur();
            hostWindow.setTimeout(() => clickButton(label), 0);
            event.preventDefault();
            event.stopPropagation();
          } else if (label && clickButton(label)) {
            event.preventDefault();
            event.stopPropagation();
          }
        };
        hostWindow.__ekbReviewShortcutHandler = handler;
        hostDocument.addEventListener("keydown", handler, true);
      } catch (_error) {
        // Shortcuts are optional; visible Streamlit buttons remain authoritative.
      }
    })();
    </script>
    """


__all__ = ["review_shortcuts_html"]

"""Comment modal for adding comments to issues/epics from the dashboard."""
from __future__ import annotations
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class CommentModal(ModalScreen[Optional[str]]):
    """TextArea input for adding comments to issues.

    Returns the comment text if submitted, None if cancelled.
    """

    def __init__(self, issue_id: str, title: str = "") -> None:
        super().__init__()
        self.issue_id = issue_id
        self.issue_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="comment-dialog"):
            header = f"Comment on {self.issue_id}"
            if self.issue_title:
                header += f" — {self.issue_title}"
            yield Static(header, classes="dialog-title")
            yield TextArea(id="comment-text", show_line_numbers=False)
            with Horizontal(classes="dialog-buttons"):
                yield Button("Submit", variant="success", id="submit")
                yield Button("Cancel", variant="primary", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#comment-text", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            text = self.query_one("#comment-text", TextArea).text.strip()
            if not text:
                self.notify("Comment cannot be empty", severity="error")
                return
            self.dismiss(text)
        else:
            self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)

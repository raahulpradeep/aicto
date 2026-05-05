"""Dashboard screens — drill-down views for epics, agents, and diffs."""
from dashboard.screens.epic_detail import EpicDetailScreen
from dashboard.screens.agent_detail import AgentDetailScreen
from dashboard.screens.diff_viewer import DiffViewerScreen

__all__ = ["EpicDetailScreen", "AgentDetailScreen", "DiffViewerScreen"]

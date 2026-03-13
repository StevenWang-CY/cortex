"""
Cortex Focus Transition Graph Schemas

Models for representing the directed graph of focus transitions
between applications and tabs, used for thrashing detection.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FocusNode(BaseModel):
    """A node in the focus transition graph."""
    node_id: str = Field(..., description="Unique node identifier (app_name:title_hash)")
    app_name: str = Field(..., description="Application name")
    window_title: str = Field("", max_length=200, description="Window/tab title")
    total_dwell_ms: float = Field(0.0, ge=0.0, description="Total time spent on this node in ms")
    visit_count: int = Field(0, ge=0, description="Number of times this node was visited")


class FocusEdge(BaseModel):
    """An edge in the focus transition graph."""
    from_node_id: str = Field(..., description="Source node ID")
    to_node_id: str = Field(..., description="Target node ID")
    count: int = Field(0, ge=0, description="Number of transitions on this edge")
    mean_dwell_ms: float = Field(0.0, ge=0.0, description="Mean dwell time before transition (ms)")
    last_transition_ts: float = Field(0.0, description="Timestamp of last transition")


class FocusTransitionGraph(BaseModel):
    """Complete focus transition graph for thrashing analysis."""
    nodes: list[FocusNode] = Field(default_factory=list)
    edges: list[FocusEdge] = Field(default_factory=list)
    thrashing_score: float = Field(0.0, ge=0.0, le=1.0, description="Overall thrashing score")
    alignment_score: float = Field(1.0, ge=0.0, le=1.0, description="Alignment with session goal")
    window_seconds: float = Field(60.0, description="Analysis window duration")
    unique_nodes_visited: int = Field(0, ge=0, description="Unique nodes in analysis window")
    mean_dwell_seconds: float = Field(0.0, ge=0.0, description="Mean dwell time per node")

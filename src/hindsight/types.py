"""
Hindsight Memory Type Definitions.
Dataclasses used across the local engine and the cloud client wrapper.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class MemoryUnit:
    """A single stored memory fact inside a Hindsight bank."""
    id: str
    fact: str
    timestamp: str
    type: str  # "world", "experience", "observation"
    tags: List[str] = field(default_factory=list)
    entities: List[Dict[str, str]] = field(default_factory=list)
    context: Optional[str] = None
    confidence: float = 0.95


@dataclass
class RecallResult:
    """One result item returned from a recall query."""
    fact: str
    score: float
    type: str
    timestamp: str
    tags: List[str] = field(default_factory=list)
    entities: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class RecallResponse:
    """Full response from a recall operation."""
    results: List[RecallResult]
    query: str
    bank_id: str
    total_memories_searched: int


@dataclass
class ReflectResponse:
    """Full response from a reflect (disposition synthesis) operation."""
    answer: str
    based_on: List[str]
    disposition: Dict[str, Any]
    directives: List[str]

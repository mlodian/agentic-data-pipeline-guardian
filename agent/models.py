"""Shared data structures for findings produced by the check library and
consumed by the Guardian agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class Finding:
    check_name: str
    severity: Severity
    table: str
    message: str
    column: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "check_name": self.check_name,
            "severity": self.severity.value,
            "table": self.table,
            "column": self.column,
            "message": self.message,
            "details": self.details,
        }

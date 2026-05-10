"""
Smart Vision Alert — Data Models
Structured data types used throughout the pipeline.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json


@dataclass
class AnalysisResult:
    """Result from Gemini AI safety analysis."""

    is_safe: bool
    risk_level: str  # safe, low, medium, high, critical
    description: str
    detected_hazards: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw_response: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Temporal / multi-frame analysis fields
    motion_detected: bool = True  # False = stillness (potential emergency)
    scene_change_level: str = "unknown"  # none, minimal, moderate, significant
    stillness_warning: bool = False  # True if person appears motionless across frames
    temporal_description: str = ""  # Description of changes between frames
    analysis_mode: str = "single"  # single or multi_frame
    frames_analyzed: int = 1

    def to_dict(self) -> dict:
        """Convert to dictionary (excluding raw_response for brevity)."""
        d = asdict(self)
        d.pop("raw_response", None)
        return d

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "AnalysisResult":
        """Create from dictionary (e.g., parsed from Gemini response)."""
        return cls(
            is_safe=data.get("is_safe", True),
            risk_level=data.get("risk_level", "safe"),
            description=data.get("description", "No description"),
            detected_hazards=data.get("detected_hazards", []),
            confidence=float(data.get("confidence", 0.0)),
            motion_detected=data.get("motion_detected", True),
            scene_change_level=data.get("scene_change_level", "unknown"),
            stillness_warning=data.get("stillness_warning", False),
            temporal_description=data.get("temporal_description", ""),
        )

    @classmethod
    def error_result(cls, error_msg: str) -> "AnalysisResult":
        """Create a result representing an analysis error."""
        return cls(
            is_safe=True,  # Default to safe on error (avoid false alarms)
            risk_level="safe",
            description=f"Analysis error: {error_msg}",
            detected_hazards=[],
            confidence=0.0,
        )


@dataclass
class AlertRecord:
    """Record of a sent alert for logging and cooldown tracking."""

    timestamp: str
    risk_level: str
    description: str
    hazards: list[str]
    image_path: str
    telegram_sent: bool = False
    telegram_message_id: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class CaptureInfo:
    """Metadata about a captured image."""

    file_path: str
    source: str  # xiaomi_cloud, local_folder, url
    captured_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    file_size_kb: float = 0.0

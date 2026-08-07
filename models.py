"""Pydantic request models untuk API bridge-clipping."""

from pydantic import BaseModel


class ClipRequest(BaseModel):
    url: str
    start: str
    end: str
    mode: str = "normal"  # "normal" atau "gaming"


class BatchClipRequest(BaseModel):
    url: str
    clips: list[dict]  # [{"start": "HH:MM:SS", "end": "HH:MM:SS"}, ...]
    mode: str = "normal"


class GamingCompilationRequest(BaseModel):
    url: str
    clips: list[dict]  # [{"start": "HH:MM:SS", "end": "HH:MM:SS"}, ...]  (5 momen)
    facecam_position: str = "btmleft"  # "btmleft" | "btmright" | "topleft" | "topright"

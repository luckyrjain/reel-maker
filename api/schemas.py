from typing import Optional
from pydantic import BaseModel, ConfigDict


class CreateReelRequest(BaseModel):
    context: str
    niche: Optional[str] = None
    voiceover_mode: str = "voiceover"


class ReelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    job_id: int


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    progress: int
    error: Optional[str] = None

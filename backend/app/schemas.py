from typing import Literal, Optional, List
from pydantic import BaseModel, EmailStr

Duration = Literal[5, 10, 15]
Difficulty = Literal["easy", "medium", "hard"]

class SignUpIn(BaseModel):
    email: EmailStr
    password: str
    name: str = ""

class LoginIn(BaseModel):
    email: EmailStr
    password: str

class CreateInterviewIn(BaseModel):
    jd_text: str = ""
    duration_min: Duration = 5
    difficulty: Difficulty = "medium"
    save_transcript: bool = True

class InterviewOut(BaseModel):
    id: str
    status: str
    duration_min: int
    difficulty: str
    created_at: str

class RubricDimension(BaseModel):
    name: str
    score_out_of_10: int
    level: Literal["Below", "Meets", "Exceeds"]
    evidence: str          # Must be grounded in transcript quotes/paraphrases
    strong_answer: str     # What an ideal answer would have looked like

class Rubric(BaseModel):
    is_complete: bool                           # False if interview ended too early
    incomplete_reason: Optional[str] = None    # Populated when is_complete=False
    overall_score_out_of_100: Optional[int] = None  # None when is_complete=False
    dimensions: List[RubricDimension]          # Empty list when is_complete=False
    jd_match_summary: str
    strengths: List[str]
    weaknesses: List[str]

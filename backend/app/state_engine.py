# Server-authoritative interview state + deterministic phase/time gating.
# The LLM only fills wording & adaptive follow-ups; the MOVE is decided here.
from typing import Dict, Any, List

PHASES_BY_DURATION: Dict[int, List[str]] = {
    5: ["exploration", "challenge", "wrap_up"],
    10: ["exploration", "deep_dive", "challenge", "wrap_up"],
    15: ["exploration", "deep_dive", "challenge", "wrap_up"],
}

def new_state(interview_id: str, duration_min: int, difficulty: str,
              jd_skills: List[str], title: str = "Technical Interview") -> Dict[str, Any]:
    return {
        "interview_id": interview_id,
        "title": title,
        "phase": "exploration",
        "duration_min": duration_min,
        "time_remaining_sec": duration_min * 60,
        "difficulty": difficulty,
        "jd_skills": jd_skills,
        "skills_assessed": {s: "unknown" for s in jd_skills},
        "topics_covered": [],
        "open_questions": [],
        "candidate_claims": [],
        "strengths": [],
        "weaknesses": [],
        "challenge_used": False,
        "hints_given": 0,
        "consecutive_blanks": 0,        # tracks unanswered questions in a row
        "candidate_wants_to_end": False,  # set True if user verbally asks to end
        "next_objective": "Ask the first targeted technical question based strictly on the JD skills listed. Do NOT ask generic questions.",
        "next_move": "follow_up",
    }

def _phase_for_elapsed(duration_min: int, elapsed_sec: int) -> str:
    phases = PHASES_BY_DURATION[duration_min]
    total = duration_min * 60
    # reserve final ~15% for wrap-up
    wrap_start = total * 0.85
    if elapsed_sec >= wrap_start:
        return "wrap_up"
    work = [p for p in phases if p != "wrap_up"]
    seg = wrap_start / len(work)
    idx = min(int(elapsed_sec // seg), len(work) - 1)
    return work[idx]

def advance(state: Dict[str, Any], elapsed_sec: int, candidate_text: str = "") -> Dict[str, Any]:
    """Update phase/time and choose next_move deterministically."""
    state["time_remaining_sec"] = max(0, state["duration_min"] * 60 - elapsed_sec)

    # --- Blank / unanswered tracking ---
    # A response is considered "blank" if it has no real content (< 8 words)
    word_count = len(candidate_text.strip().split()) if candidate_text.strip() else 0
    if word_count < 8:
        state["consecutive_blanks"] = state.get("consecutive_blanks", 0) + 1
    else:
        state["consecutive_blanks"] = 0

    # Force-end conditions: 3 blanks in a row OR candidate asked to end
    if state.get("consecutive_blanks", 0) >= 3 or state.get("candidate_wants_to_end", False):
        state["next_move"] = "force_end"
        state["phase"] = "wrap_up"
        state["next_objective"] = (
            "The candidate has been unable to answer the last three questions, or has requested to end the interview. "
            "Professionally close the interview now. Thank them briefly (1 sentence) and let them know the session is complete. "
            "End with exactly the phrase: '[INTERVIEW_COMPLETE]'"
        )
        return state

    phase = _phase_for_elapsed(state["duration_min"], elapsed_sec)
    state["phase"] = phase

    skills = state.get("jd_skills", [])
    skills_str = ", ".join(skills) if skills else "the JD requirements"

    if phase == "wrap_up":
        state["next_move"] = "wrap_up"
        state["next_objective"] = "Briefly thank the candidate, let them know the interview is complete, and ask if they have any questions about the process."
    elif phase == "challenge" and not state["challenge_used"]:
        state["next_move"] = "challenge"
        state["challenge_used"] = True
        state["next_objective"] = (
            f"Ask a harder, more nuanced question about one of the JD skills: [{skills_str}]. "
            "The question must probe edge cases, trade-offs, or real-world failure scenarios "
            "directly related to those skills. Do NOT ask generic system design questions."
        )
    else:
        state["next_move"] = "follow_up"
        state["next_objective"] = (
            f"Based on the candidate's last answer, ask a targeted follow-up question "
            f"that probes deeper into one of the JD skills: [{skills_str}]. "
            "Stay strictly on-topic to the job description."
        )
    return state

# Keywords that signal the candidate wants to stop the interview
_END_KEYWORDS = [
    "end the interview", "stop the interview", "i want to stop", "end interview",
    "let's end", "lets end", "finish the interview", "i'm done", "i am done",
    "please end", "terminate the interview", "stop session", "end session",
    "conclude the interview", "that's all", "thats all", "wrap it up",
]

def record_turn(state: Dict[str, Any], role: str, text: str) -> Dict[str, Any]:
    if role == "candidate" and text.strip():
        state["candidate_claims"].append(text.strip()[:280])
        # Check if candidate is asking to end the interview
        lower = text.lower()
        if any(kw in lower for kw in _END_KEYWORDS):
            state["candidate_wants_to_end"] = True
    return state

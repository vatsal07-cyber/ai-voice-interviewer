import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from .db import get_db
from .models import User, Interview, Message, Report
from .schemas import LoginIn, SignUpIn, CreateInterviewIn
from .auth import current_user, make_session_token, hash_password, verify_password
from .config import settings
from .state_engine import new_state
from . import llm

router = APIRouter()


@router.post("/auth/signup")
def signup(body: SignUpIn, db: Session = Depends(get_db)):
    """Register a new user with email + password."""
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(409, "An account with this email already exists.")
    user = User(email=body.email, hashed_password=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"token": make_session_token(user.id)}


@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    """Authenticate with email + password."""
    user = db.query(User).filter(User.email == body.email).first()
    if not user:
        raise HTTPException(401, "Invalid email or password.")
    if not user.hashed_password:
        raise HTTPException(401, "This account has no password set. Please sign up again.")
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Invalid email or password.")
    return {"token": make_session_token(user.id)}


@router.post("/interviews")
async def create_interview(body: CreateInterviewIn, user: User = Depends(current_user),
                           db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    user_today = db.query(func.count(Interview.id)).filter(
        Interview.user_id == user.id,
        func.date(Interview.created_at) == today).scalar()
    if user_today >= settings.max_interviews_per_user_per_day:
        raise HTTPException(429, "Daily interview limit reached. Try again tomorrow.")
    global_today = db.query(func.count(Interview.id)).filter(
        func.date(Interview.created_at) == today).scalar()
    if global_today >= settings.global_daily_session_budget:
        raise HTTPException(503, "Service is busy right now. Please try later.")

    extraction = await llm.extract_skills(body.jd_text)
    skills = extraction.get("skills", [])
    title = extraction.get("title", "Interview")
    role_category = extraction.get("role_category", "technical")

    iv = Interview(user_id=user.id, jd_text=body.jd_text,
                   duration_min=body.duration_min, difficulty=body.difficulty,
                   save_transcript=body.save_transcript)
    db.add(iv); db.commit(); db.refresh(iv)
    state = new_state(iv.id, iv.duration_min, iv.difficulty, skills, title, role_category)
    iv.state_json = json.dumps(state); db.commit()
    return {"id": iv.id, "jd_skills": skills, "title": title}


def _interviewer_instructions(iv: Interview, skills: list, role_category: str = "technical") -> str:
    """
    Build the system prompt for the interviewer LLM.
    The persona and question framing adapt to the role_category derived from the JD.
    No system design, architecture, or engineering assumptions are made by default.
    """
    skills_list = ", ".join(skills) if skills else "the requirements listed in the job description"

    # Role-specific persona and question guidance
    persona_map = {
        "technical": (
            "You are a professional technical interviewer. "
            "You assess candidates on their practical knowledge of specific technologies, "
            "tools, and concepts directly mentioned in the job description."
        ),
        "analytical": (
            "You are a professional analytical interviewer. "
            "You assess candidates on their data analysis skills, quantitative reasoning, "
            "and domain-specific tools listed in the job description."
        ),
        "non-technical": (
            "You are a professional interviewer for a non-technical role. "
            "You assess candidates on their domain knowledge, communication skills, "
            "behavioural competencies, and role-specific experience from the job description."
        ),
    }
    persona = persona_map.get(role_category, persona_map["technical"])

    # Role-specific question type guidance
    question_guidance_map = {
        "technical": (
            "Ask questions about specific technologies, programming concepts, tools, and frameworks "
            "listed in the JD. For example, if the JD says 'React', ask about hooks, state management, "
            "or rendering patterns — NOT about system design or distributed systems unless those appear in the JD."
        ),
        "analytical": (
            "Ask questions about data handling, SQL, statistical reasoning, BI tools, Python for data, "
            "or whichever analytical skills appear in the JD. Ask scenario-based questions "
            "like 'How would you approach this data problem?' — not coding or architecture questions."
        ),
        "non-technical": (
            "Ask situational and behavioural questions based on the JD responsibilities. "
            "For example: 'Tell me about a time you handled X.' or 'How would you approach Y scenario?' "
            "Do NOT ask any technical, coding, or system design questions."
        ),
    }
    question_guidance = question_guidance_map.get(role_category, question_guidance_map["technical"])

    return (
        f"{persona}\n"
        "YOU MUST ALWAYS SPEAK ENGLISH. Do not speak any other language.\n\n"
        "=== INTERVIEW PARAMETERS ===\n"
        f"Difficulty: {iv.difficulty}\n"
        f"Duration: {iv.duration_min} minutes\n"
        f"JOB DESCRIPTION: \"{iv.jd_text}\"\n"
        f"KEY SKILLS/COMPETENCIES TO ASSESS: [{skills_list}]\n\n"
        "=== QUESTION RULES ===\n\n"
        f"RULE 1 — SCOPE: {question_guidance}\n\n"
        "RULE 2 — VERIFICATION: Before asking any question, internally verify: "
        "'Does this question directly test a skill or responsibility explicitly listed in the JD above?' "
        "If the answer is NO, you MUST NOT ask it.\n\n"
        "RULE 3 — NO SYSTEM DESIGN BY DEFAULT: Unless the words 'system design', 'architecture', "
        "'scalability', 'distributed', or 'microservices' appear explicitly in the JD, "
        "you must NEVER ask questions about those topics.\n\n"
        "RULE 4 — COVERAGE: Track which JD skills have been covered. "
        "Move to the next skill only after the current one is sufficiently explored.\n\n"
        "RULE 5 — FOLLOW-UPS: Follow-up questions must probe deeper into the candidate's last answer. "
        "Do NOT introduce new unrelated topics in a follow-up.\n\n"
        "RULE 6 — CONCISENESS: Keep questions to 1-2 sentences. Ask ONE question at a time. "
        "Never ask compound questions.\n\n"
        "RULE 7 — NO FEEDBACK: Do NOT provide answers, hints, or correctness feedback. "
        "Never say 'That is correct' or 'Actually, the answer is...'\n\n"
        "RULE 8 — REDIRECTION: If the candidate goes off-topic, politely redirect them once. "
        "If they go off-topic again, move on.\n\n"
        "RULE 9 — 3-STRIKES: If the candidate gives three consecutive very short, blank, "
        "or completely off-topic responses, close the interview immediately. "
        "Say: 'Thank you for your time. Unfortunately, we were unable to complete enough of the assessment today.' "
        "Then append exactly: [INTERVIEW_COMPLETE]\n\n"
        "RULE 10 — CANDIDATE REQUESTS TO END: If the candidate explicitly asks to end the interview, comply. "
        "Say: 'Of course, thank you for your time today. The interview is now concluded.' "
        "Then append exactly: [INTERVIEW_COMPLETE]\n\n"
        "=== BEHAVIOR ===\n"
        "- Start by introducing yourself in ONE brief sentence, then immediately ask the FIRST question.\n"
        "- Keep all replies short and direct (1-3 sentences max).\n"
        "- Maintain a professional, focused, and objective tone throughout.\n"
    )


async def _auto_complete_interview(iid: str, db, transcript: str | None = None) -> None:
    """Internal helper called by ws.py to complete an interview without an HTTP request."""
    from .models import Report as ReportModel
    iv = db.query(Interview).filter(Interview.id == iid).first()
    if not iv or iv.status == "completed":
        return
    if transcript is None:
        msgs = db.query(Message).filter(Message.interview_id == iid).order_by(Message.ts).all()
        transcript = "\n".join(f"{m.role}: {m.text}" for m in msgs)
    state = json.loads(iv.state_json or "{}")
    try:
        rubric = await llm.generate_report(
            transcript=transcript,
            jd_skills=state.get("jd_skills", []),
            role_category=state.get("role_category", "technical"),
            started_at=iv.started_at,
            ended_at=datetime.utcnow(),
        )
        md = llm.render_report_md(rubric)
        rep = ReportModel(interview_id=iid, rubric_json=rubric.model_dump_json(), report_md=md)
        db.add(rep)
    except Exception as e:
        print(f"[auto_complete] Report generation failed: {e}")
    iv.status = "completed"
    iv.ended_at = datetime.utcnow()
    db.commit()


@router.post("/interviews/{iid}/complete")
async def complete(iid: str, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    iv = db.query(Interview).filter(Interview.id == iid,
                                    Interview.user_id == user.id).first()
    if not iv:
        raise HTTPException(404, "Not found")
    msgs = db.query(Message).filter(Message.interview_id == iid).order_by(Message.ts).all()
    transcript = "\n".join(f"{m.role}: {m.text}" for m in msgs)
    state = json.loads(iv.state_json or "{}")
    rubric = await llm.generate_report(
        transcript=transcript,
        jd_skills=state.get("jd_skills", []),
        role_category=state.get("role_category", "technical"),
        started_at=iv.started_at,
        ended_at=datetime.utcnow(),
    )
    md = llm.render_report_md(rubric)
    rep = Report(interview_id=iid, rubric_json=rubric.model_dump_json(), report_md=md)
    db.add(rep)
    iv.status = "completed"; iv.ended_at = datetime.utcnow(); db.commit()
    return {"report_md": md}


@router.get("/interviews")
def list_interviews(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(Interview).filter(Interview.user_id == user.id)\
        .order_by(Interview.created_at.desc()).all()
    return [{"id": r.id, "status": r.status, "duration_min": r.duration_min,
             "difficulty": r.difficulty, "created_at": r.created_at.isoformat()} for r in rows]


@router.get("/interviews/{iid}/report")
def get_report(iid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    iv = db.query(Interview).filter(Interview.id == iid,
                                    Interview.user_id == user.id).first()
    if not iv:
        raise HTTPException(404, "Not found")
    rep = db.query(Report).filter(Report.interview_id == iid)\
        .order_by(Report.created_at.desc()).first()
    if not rep:
        raise HTTPException(404, "No report yet")
    state = json.loads(iv.state_json or "{}")
    return {"report_md": rep.report_md, "title": state.get("title", "Interview")}


@router.delete("/interviews/{iid}")
def delete_interview(iid: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    iv = db.query(Interview).filter(Interview.id == iid,
                                    Interview.user_id == user.id).first()
    if not iv:
        raise HTTPException(404, "Not found")
    db.query(Message).filter(Message.interview_id == iid).delete()
    db.query(Report).filter(Report.interview_id == iid).delete()
    db.delete(iv); db.commit()
    return {"ok": True}

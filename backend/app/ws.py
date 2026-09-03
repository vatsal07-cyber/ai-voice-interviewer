# Control/state channel ONLY (no audio). Receives turn events from the client,
# updates state via the deterministic engine, persists EVERY turn,
# and returns the next move + timer + phase.
import json
import base64
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import jwt, JWTError
from .config import settings
from .db import SessionLocal
from .models import Interview, Message, User
from .state_engine import advance, record_turn
from .audio_pipeline import transcribe_audio_groq, generate_chat_response_groq, synthesize_speech_groq
from .routes import _interviewer_instructions

ws_router = APIRouter()

ALGO = "HS256"

@ws_router.websocket("/interviews/{iid}/control")
async def control(
    ws: WebSocket,
    iid: str,
    token: str = Query(default=""),
):
    await ws.accept()
    db = SessionLocal()
    try:
        # Auth gate: validate JWT passed as ?token= query param
        if not token:
            await ws.close(code=4401, reason="Missing token")
            return
        try:
            payload = jwt.decode(token, settings.app_secret, algorithms=[ALGO])
            user_id = payload.get("sub")
        except JWTError:
            await ws.close(code=4403, reason="Invalid token")
            return

        # Validate interview ownership
        iv = db.query(Interview).filter(
            Interview.id == iid,
            Interview.user_id == user_id,
        ).first()
        if not iv:
            await ws.close(code=4404, reason="Interview not found")
            return

        # Mark as running on connect if not already
        if iv.status != "running":
            iv.status = "running"
            iv.started_at = datetime.utcnow()
            db.commit()

        state = json.loads(iv.state_json or "{}")

        # Get system instructions (role-aware)
        role_category = state.get("role_category", "technical")
        instructions = _interviewer_instructions(iv, state.get("jd_skills", []), role_category)

        # Initial greeting if no messages
        msgs = db.query(Message).filter(Message.interview_id == iid).order_by(Message.ts).all()
        # In-memory conversation for this session: the LLM needs full history
        # even when save_transcript=False (no Message rows exist in that mode).
        history = [
            {"role": "assistant" if m.role == "interviewer" else "user", "content": m.text}
            for m in msgs
        ]
        if not msgs:
            # Generate the first message proactively
            initial_prompt = [{"role": "system", "content": instructions}, {"role": "user", "content": "Hello, I am ready to begin the interview."}]
            try:
                ai_text = await generate_chat_response_groq(initial_prompt)
                ai_audio = await synthesize_speech_groq(ai_text)
                
                if iv.save_transcript:
                    db.add(Message(interview_id=iid, role="interviewer", text=ai_text, ts=datetime.utcnow()))
                    db.commit()
                history.append({"role": "assistant", "content": ai_text})

                await ws.send_text(json.dumps({
                    "type": "ai_response",
                    "text": ai_text,
                    "audio_b64": base64.b64encode(ai_audio).decode("utf-8")
                }))
            except WebSocketDisconnect:
                raise
            except Exception as e:
                print(f"[ws] Error generating initial greeting: {e}")

        while True:
            raw = await ws.receive_text()
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue  # ignore malformed messages

            if evt.get("type") == "audio_chunk":
                audio_b64 = evt.get("audio_b64", "")
                elapsed = int(evt.get("elapsed_sec", 0))

                # 1. Decode & Transcribe
                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    candidate_text = await transcribe_audio_groq(audio_bytes)
                except Exception as e:
                    print(f"[ws] STT error: {type(e).__name__}: {e}")
                    await ws.send_text(json.dumps({"type": "error", "message": "Failed to transcribe audio"}))
                    continue

                if not candidate_text.strip():
                    # Send state update anyway
                    await ws.send_text(json.dumps({
                        "type": "state",
                        "phase": state.get("phase", ""),
                        "time_remaining_sec": state.get("time_remaining_sec", 0),
                    }))
                    continue

                # Save candidate text
                if iv.save_transcript:
                    db.add(Message(interview_id=iid, role="candidate", text=candidate_text, ts=datetime.utcnow()))
                history.append({"role": "user", "content": candidate_text})

                state = record_turn(state, "candidate", candidate_text)
                state = advance(state, elapsed, candidate_text)  # pass text for blank-detection
                iv.state_json = json.dumps(state)
                db.commit()

                # Send state update
                await ws.send_text(json.dumps({
                    "type": "state",
                    "phase": state["phase"],
                    "time_remaining_sec": state["time_remaining_sec"],
                    "next_move": state["next_move"],
                    "next_objective": state["next_objective"],
                    "candidate_text": candidate_text, # send back what we transcribed
                }))

                # 2. Generate LLM response from the in-memory history
                chat_history = [{"role": "system", "content": instructions}]

                # Add context about the current phase / objective from the state engine
                chat_history.append({
                    "role": "system",
                    "content": f"CURRENT PHASE: {state['phase']}\nYOUR NEXT OBJECTIVE: {state['next_objective']}"
                })

                chat_history.extend(history)

                try:
                    ai_text = await generate_chat_response_groq(chat_history)
                    # Strip the sentinel token from the spoken text before TTS
                    spoken_text = ai_text.replace("[INTERVIEW_COMPLETE]", "").strip()
                    if not spoken_text:
                        # Empty LLM reply — keep transcript/history/audio in sync by
                        # using the same re-ask text everywhere instead of storing ""
                        print(f"[ws] empty LLM reply for interview {iid}; falling back to a re-ask")
                        spoken_text = "Sorry, could you say that again?"
                    # 3. Generate TTS
                    ai_audio_bytes = await synthesize_speech_groq(spoken_text)
                    
                    if iv.save_transcript:
                        db.add(Message(interview_id=iid, role="interviewer", text=spoken_text, ts=datetime.utcnow()))
                    history.append({"role": "assistant", "content": spoken_text})

                    state = record_turn(state, "interviewer", spoken_text)
                    iv.state_json = json.dumps(state)
                    db.commit()

                    await ws.send_text(json.dumps({
                        "type": "ai_response",
                        "text": spoken_text,
                        "audio_b64": base64.b64encode(ai_audio_bytes).decode("utf-8")
                    }))

                    # --- Auto-end detection ---
                    # End if the state engine forced it OR the LLM included the sentinel
                    if state.get("next_move") == "force_end" or "[INTERVIEW_COMPLETE]" in ai_text:
                        # Auto-complete server-side (pass in-memory transcript so
                        # save_transcript=False interviews still get a report)
                        from .routes import _auto_complete_interview
                        session_transcript = "\n".join(
                            f"{'interviewer' if h['role'] == 'assistant' else 'candidate'}: {h['content']}"
                            for h in history
                        )
                        await _auto_complete_interview(iid, db, transcript=session_transcript)
                        await ws.send_text(json.dumps({"type": "interview_ended"}))
                        break  # exit the while loop; WebSocket will close naturally

                except Exception as e:
                    print(f"[ws] LLM/TTS error: {type(e).__name__}: {e}")
                    await ws.send_text(json.dumps({"type": "error", "message": "Failed to generate AI response"}))

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] unexpected error for {iid}: {exc}")
    finally:
        db.close()


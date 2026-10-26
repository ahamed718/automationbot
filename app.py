"""InternFlow Assistant - Flask backend.

Flow per review cycle:
  1. Staff submits candidate email + requirement doc + past review notes.
  2. Gemini breaks the doc into small tasks (first cycle) and writes a warm,
     human explanation of the next milestone plus feedback.
  3. Python assembles the final email (schedule + Meet link are inserted
     deterministically so the AI can never alter them).
  4. Staff reviews the draft, then sends it via SMTP or copies it.
  5. Progress is stored in SQLite; the loop repeats until all tasks are done.
"""
import json
import logging
import re
import smtplib
import sqlite3
from datetime import datetime
from email.message import EmailMessage

import google.generativeai as genai
from flask import Flask, jsonify, render_template, request

from config import Config

Config.validate()
log = logging.getLogger("internflow.app")

app = Flask(__name__)
app.config.from_object(Config)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MEET_RE = re.compile(r"^https://meet\.google\.com/[a-z0-9\-]+(\?.*)?$", re.I)
TASKS_PER_MILESTONE = 2  # how many pending tasks make up one review cycle

# ----------------------------------------------------------------- database
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                email TEXT PRIMARY KEY, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
                title TEXT NOT NULL, detail TEXT, position INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
                notes TEXT, review_at TEXT, meet_link TEXT, created_at TEXT NOT NULL);
            """
        )


def get_tasks(email: str) -> list[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT id, title, detail, status FROM tasks WHERE email=? ORDER BY position",
            (email,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- gemini
def ask_gemini(prompt: str) -> dict:
    """Call Gemini and return parsed JSON. Raises RuntimeError with a clear message."""
    if not Config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing. Add it to your .env file.")
    genai.configure(api_key=Config.GEMINI_API_KEY)
    model = genai.GenerativeModel(
        Config.GEMINI_MODEL,
        generation_config={"response_mime_type": "application/json", "temperature": 0.9},
    )
    try:
        text = model.generate_content(prompt).text
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("Gemini returned non-JSON output: %s", exc)
        raise RuntimeError("The AI reply could not be read. Please try again.") from exc
    except Exception as exc:  # network, quota, safety blocks, etc.
        log.exception("Gemini call failed")
        raise RuntimeError(f"Gemini request failed: {exc}") from exc


def build_prompt(needs_breakdown: bool, requirement: str, notes: str,
                 done: list[str], milestone: list[dict]) -> str:
    """Compose the prompt. Tone rules live here so every email stays human."""
    breakdown = (
        "Also break the requirement document into 4-10 small, concrete, ordered tasks "
        'and return them as "tasks": [{"title": "...", "detail": "one plain sentence"}].'
        if needs_breakdown else 'Return "tasks": [] (tasks already exist).'
    )
    return f"""You are a kind, experienced mentor writing to an intern. Write the way a
thoughtful senior colleague talks over coffee: warm, specific, encouraging, and honest.
Never use stiff phrases like "I hope this email finds you well", "as per", or "kindly
revert". No bullet-point templates for the feedback. Vary sentence length. Name real
strengths first, then frame improvements as next steps, not criticism.

REQUIREMENT DOCUMENT:
{requirement or "(already processed in an earlier cycle)"}

PAST REVIEW NOTES FROM STAFF:
{notes or "(none yet - this is the first review, so welcome them warmly)"}

TASKS ALREADY COMPLETED: {json.dumps(done)}
NEXT MILESTONE TASKS: {json.dumps([{"title": t["title"], "detail": t["detail"]} for t in milestone])}

{breakdown}

Return ONLY JSON with these keys:
"tasks": as described above,
"subject": a friendly, specific email subject line,
"feedback": 1-2 short paragraphs of constructive feedback based on the past notes,
"explanation": 2-3 short paragraphs explaining the next milestone tasks in simple
   language, with a concrete first step for each, as if explaining to a smart beginner,
"closing": one warm sentence encouraging them before the review (no sign-off name)."""


# -------------------------------------------------------------------- email
def format_review_time(raw: str) -> str:
    """Turn an HTML datetime-local value into 'Friday, 14 March 2026 at 03:30 PM'."""
    return datetime.fromisoformat(raw).strftime("%A, %d %B %Y at %I:%M %p")


def assemble_email(ai: dict, review_at: str, meet: str) -> str:
    """Final body; schedule and Meet link are fixed text, never AI-generated."""
    return (
        f"Hi there,\n\n{ai['feedback'].strip()}\n\n"
        f"Here's what's next:\n\n{ai['explanation'].strip()}\n\n"
        f"Our next review:\n"
        f"When: {format_review_time(review_at)}\n"
        f"Google Meet: {meet}\n\n"
        f"{ai['closing'].strip()}\n\n"
        f"Warmly,\n{Config.SENDER_NAME}"
    )


# ------------------------------------------------------------------- routes
@app.get("/")
def index():
    return render_template("index.html", smtp_ready=Config.smtp_ready())


@app.get("/api/candidate")
def candidate():
    email = request.args.get("email", "").strip().lower()
    if not EMAIL_RE.match(email):
        return jsonify(error="Enter a valid email address."), 400
    return jsonify(email=email, tasks=get_tasks(email))


@app.post("/api/generate")
def generate():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    requirement = (data.get("requirement") or "").strip()
    notes = (data.get("notes") or "").strip()
    review_at = (data.get("review_at") or "").strip()
    meet = (data.get("meet") or "").strip()

    # --- validate input ---
    if not EMAIL_RE.match(email):
        return jsonify(error="Enter a valid candidate email."), 400
    if not MEET_RE.match(meet):
        return jsonify(error="Enter a Google Meet link like https://meet.google.com/abc-defg-hij."), 400
    try:
        format_review_time(review_at)
    except ValueError:
        return jsonify(error="Choose a valid review date and time."), 400

    tasks = get_tasks(email)
    if not tasks and not requirement:
        return jsonify(error="This is a new intern - paste the requirement document first."), 400
    needs_breakdown = not tasks

    pending = [t for t in tasks if t["status"] == "pending"]
    if tasks and not pending:
        return jsonify(complete=True, message="All tasks are complete. This intern has finished the project.", tasks=tasks)

    try:
        # First call may create the task list; later calls reuse stored tasks.
        milestone = pending[:TASKS_PER_MILESTONE]
        done = [t["title"] for t in tasks if t["status"] == "done"]
        ai = ask_gemini(build_prompt(needs_breakdown, requirement, notes, done, milestone))

        if needs_breakdown:
            new = [t for t in ai.get("tasks", []) if t.get("title")]
            if not new:
                return jsonify(error="No tasks could be extracted. Try a clearer document."), 502
            with db() as c:
                c.execute("INSERT OR IGNORE INTO candidates VALUES (?, ?)", (email, datetime.now().isoformat()))
                c.executemany(
                    "INSERT INTO tasks (email, title, detail, position) VALUES (?,?,?,?)",
                    [(email, t["title"], t.get("detail", ""), i) for i, t in enumerate(new)],
                )
            tasks = get_tasks(email)
            milestone = tasks[:TASKS_PER_MILESTONE]
            # Regenerate explanation against the real saved milestone for accuracy.
            ai = ask_gemini(build_prompt(False, requirement, notes, [], milestone))

        body = assemble_email(ai, review_at, meet)
        with db() as c:
            c.execute(
                "INSERT INTO reviews (email, notes, review_at, meet_link, created_at) VALUES (?,?,?,?,?)",
                (email, notes, review_at, meet, datetime.now().isoformat()),
            )
        return jsonify(to=email, subject=ai.get("subject", "Your next internship milestone"),
                       body=body, tasks=get_tasks(email))
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502
    except Exception:
        log.exception("Unexpected error in /api/generate")
        return jsonify(error="Something went wrong on the server. Check the logs."), 500


@app.post("/api/send")
def send():
    data = request.get_json(silent=True) or {}
    to, subject, body = data.get("to", ""), data.get("subject", ""), data.get("body", "")
    if not EMAIL_RE.match(to) or not subject or not body:
        return jsonify(error="Recipient, subject and body are all required."), 400
    if not Config.smtp_ready():
        return jsonify(error="SMTP isn't configured. Copy the draft, or set SMTP_USER and SMTP_PASSWORD in .env."), 400

    msg = EmailMessage()
    msg["From"] = f"{Config.SENDER_NAME} <{Config.SMTP_USER}>"
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=20) as s:
            s.starttls()
            s.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            s.send_message(msg)
        log.info("Email sent to %s", to)
        return jsonify(ok=True)
    except smtplib.SMTPAuthenticationError:
        return jsonify(error="SMTP login failed. For Gmail, use an App Password."), 502
    except Exception as exc:
        log.exception("SMTP send failed")
        return jsonify(error=f"Could not send the email: {exc}"), 502


@app.post("/api/task/<int:task_id>")
def update_task(task_id: int):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("pending", "done"):
        return jsonify(error="Status must be 'pending' or 'done'."), 400
    with db() as c:
        cur = c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    if cur.rowcount == 0:
        return jsonify(error="Task not found."), 404
    return jsonify(ok=True)


init_db()

if __name__ == "__main__":
    app.run(debug=Config.DEBUG, port=5000)

# InternFlow Assistant

A Flask + Gemini app that turns a requirement document into small tasks, then writes a warm follow-up email for each review cycle: feedback, a plain-language explanation of the next milestone, the review date/time, and a Google Meet link. Progress is stored per intern email in SQLite, so you repeat the cycle until every task is done.

## How the cycle works
1. Enter the intern's email, paste the requirement document, add review notes, pick the next review time, and paste a Meet link.
2. First cycle: Gemini splits the document into ordered tasks and saves them.
3. Review and edit the draft, then send it (SMTP) or copy it.
4. Tick tasks off as the intern finishes them. Next cycle, leave the document blank; the app reuses saved tasks and explains the next two.
5. When all tasks are done, the app tells you the project is complete.

## Local setup
Requires Python 3.10+.

```bash
cd internflow
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Edit `.env`:
- `GEMINI_API_KEY`: create one at https://aistudio.google.com/apikey
- Optional SMTP: for Gmail, turn on 2-Step Verification and create an App Password, then set `SMTP_USER` and `SMTP_PASSWORD`. Without these the app works in draft-only mode.

Run:
```bash
python app.py
```
Open http://127.0.0.1:5000.

## Production
```bash
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```
Put it behind HTTPS and add authentication (for example a reverse proxy with SSO), because this tool handles intern emails and feedback.

## Notes and limits
- The Meet link is entered by staff. Creating Meet links automatically needs the Google Calendar API with OAuth, which is a natural next step.
- The schedule and link are inserted by Python, not the AI, so they can't be altered.
- Change `GEMINI_MODEL` in `.env` if your account uses a different model.
- Never commit `.env` or the `.db` file (both are in `.gitignore`).

## Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit: InternFlow Assistant"
git branch -M main
# Create an empty repo on github.com first, then:
git remote add origin https://github.com/<your-username>/internflow-assistant.git
git push -u origin main
```
Check `git status` first to confirm `.env` is not listed.

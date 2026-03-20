from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import psycopg2
import psycopg2.extras
import json
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import resend
from datetime import datetime, timedelta
from flask_cors import CORS
from google import genai
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-in-production")
CORS(app)

# ==================== RESEND EMAIL ====================
resend.api_key = os.getenv("RESEND_API_KEY", "")
MAIL_FROM = os.getenv("MAIL_FROM", "onboarding@resend.dev")

# ==================== SUPABASE / POSTGRES ====================
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

# ==================== GEMINI ====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not set.")
client = genai.Client(api_key=GEMINI_API_KEY)

flash_model      = "gemini-2.5-flash"
flash_lite_model = "gemini-2.5-flash-lite"

def generate_with_fallback(contents):
    try:
        return client.models.generate_content(model=flash_model, contents=contents).text
    except Exception as e:
        print(f"Flash error: {e}. Falling back.")
        try:
            return client.models.generate_content(model=flash_lite_model, contents=contents).text
        except Exception as e2:
            print("Flash LITE FAILED", e2)
            return "Service is currently unavailable. Please try again later."

# ==================== DB INIT ====================
def init_db():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                 SERIAL PRIMARY KEY,
            name               TEXT,
            email              TEXT UNIQUE NOT NULL,
            password           TEXT,
            level              TEXT,
            subjects           TEXT,
            reset_token        TEXT,
            reset_token_expiry TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tutor_sessions (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
            topic      TEXT NOT NULL,
            history    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

init_db()

# ==================== HELPERS ====================
def get_current_user_id():
    if "user" not in session:
        return None
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", (session["user"]["email"],))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row["id"] if row else None

# ==================== PAGE ROUTES ====================

@app.route("/")
def index():
    return redirect(url_for("auth"))

@app.route("/auth")
def auth():
    return render_template("auth.html")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("auth"))
    return render_template("index.html", user=session["user"])

@app.route("/forgot-password")
def forgot_password_page():
    return render_template("forgot.html")

@app.route("/reset-password/<token>")
def reset_password_page(token):
    return render_template("reset.html", token=token)

@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("auth"))

# ==================== AUTH API ====================

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request"}), 400

    name     = data.get("name")
    email    = data.get("email")
    password = data.get("password")
    level    = data.get("level")
    subjects = ",".join(data.get("subjects", []))

    if not all([name, email, password, level]):
        return jsonify({"success": False, "message": "All fields are required"}), 400

    hashed = generate_password_hash(password)

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"success": False, "message": "User already exists"})
        cur.execute(
            "INSERT INTO users (name, email, password, level, subjects) VALUES (%s, %s, %s, %s, %s)",
            (name, email, hashed, level, subjects)
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Signup error: {e}")
        return jsonify({"success": False, "message": "Server error during signup"}), 500


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request"}), 400

    email    = data.get("email")
    password = data.get("password")

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT name, email, password, level, subjects FROM users WHERE email = %s", (email,)
        )
        user = cur.fetchone()
        cur.close(); conn.close()

        if user and check_password_hash(user["password"], password):
            session["user"] = {
                "name":     user["name"],
                "email":    user["email"],
                "level":    user["level"],
                "subjects": user["subjects"].split(",") if user["subjects"] else []
            }
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"success": False, "message": "Server error during login"}), 500


# ==================== FORGOT / RESET ====================

@app.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    try:
        data = request.get_json(force=True, silent=True)
        if not data or "email" not in data:
            return jsonify({"success": False, "message": "Email is required"})

        email = data.get("email")
        conn  = get_db()
        cur   = conn.cursor()
        cur.execute("SELECT email FROM users WHERE email = %s", (email,))
        user = cur.fetchone()

        if not user:
            cur.close(); conn.close()
            return jsonify({"success": False, "message": "No account found with that email."})

        token  = secrets.token_urlsafe(32)
        expiry = datetime.utcnow() + timedelta(minutes=15)

        cur.execute(
            "UPDATE users SET reset_token = %s, reset_token_expiry = %s WHERE email = %s",
            (token, expiry, email)
        )
        conn.commit()
        cur.close(); conn.close()

        reset_link = f"{request.url_root.rstrip('/')}/reset-password/{token}"
        resend.Emails.send({
            "from":    MAIL_FROM,
            "to":      email,
            "subject": "Reset your EduSpark password",
            "text":    f"Click the link below to reset your password (valid 15 minutes):\n\n{reset_link}\n\nIf you didn't request this, ignore this email."
        })

        return jsonify({"success": True, "message": "Password reset link sent to your email."})
    except Exception as e:
        print(f"Forgot password error: {e}")
        return jsonify({"success": False, "message": f"Server error: {str(e)}"})


@app.route("/api/reset-password/<token>", methods=["POST"])
def reset_password(token):
    try:
        data     = request.get_json(force=True, silent=True)
        password = data.get("password")
        confirm  = data.get("confirm_password")

        if password != confirm:
            return jsonify({"success": False, "message": "Passwords do not match"})

        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, reset_token_expiry FROM users WHERE reset_token = %s", (token,)
        )
        user = cur.fetchone()

        if not user:
            cur.close(); conn.close()
            return jsonify({"success": False, "message": "Invalid or expired token"})

        if not user["reset_token_expiry"] or user["reset_token_expiry"] < datetime.utcnow():
            cur.close(); conn.close()
            return jsonify({"success": False, "message": "Token expired. Please request a new link."})

        hashed = generate_password_hash(password)
        cur.execute(
            "UPDATE users SET password = %s, reset_token = NULL, reset_token_expiry = NULL WHERE id = %s",
            (hashed, user["id"])
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "message": "Password reset successful"})
    except Exception as e:
        print(f"Reset error: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


# ==================== AI TUTOR ====================

@app.route("/api/tutor/start", methods=["POST"])
def start_tutor_session():
    if "user" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data    = request.get_json()
    topic   = data.get("topic")
    if not topic:
        return jsonify({"success": False, "message": "Topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, history FROM tutor_sessions WHERE user_id = %s AND topic = %s",
            (user_id, topic)
        )
        existing = cur.fetchone()

        if existing:
            history    = json.loads(existing["history"])
            session_id = existing["id"]
        else:
            system_instruction = (
                f"You are EduSpark AI, an intelligent tutor designed to teach secondary school students from Grade 7 to Grade 12. "
                f"You are currently helping a student learn about {topic}. "
                "EduSpark follows the Nigerian secondary school structure. "
                "Grade 7 = JSS1, Grade 8 = JSS2, Grade 9 = JSS3, Grade 10 = SS1, Grade 11 = SS2, Grade 12 = SS3. "
                "Always adjust your explanations to match the student's level. "
                "You teach subjects including Mathematics, English Language, Basic Science, Basic Technology, Physics, Chemistry, "
                "Biology, Agricultural Science, Economics, Government, Geography, Civic Education, Social Studies, and Literature in English. "
                "Your explanations must always be appropriate for secondary school students, clear, structured, and educational. "
                "Teach like a supportive teacher, not a search engine. Always prioritize understanding over just giving answers. "
                "Use simple language suitable for students aged 11-18. Break complex ideas into smaller steps. "
                "When explaining a concept: First explain simply, then give a real-life example, then provide a key takeaway. "
                "When solving problems: Identify the concept, show step-by-step solution, give the final answer, then give a practice question. "
                "For definitions: provide definition, short explanation, and an example. "
                "For summaries: give a short summary, bullet-point key points, and one review question. "
                "For practice/quiz: generate grade-appropriate questions, prefer multiple-choice, provide answers separately. "
                "Never give only the final answer without explanation. "
                "Keep tone friendly, encouraging, and educational. Avoid jargon. "
                "Break teaching into exactly 3 smaller messages per concept. Each message 6-7 lines max. "
                "Use '---MESSAGE_BREAK---' between each message. "
                "After every 3 messages, ask if the student understands. "
                "At end of lesson ask: quiz, re-explanation, or more examples. "
                "Avoid unnecessary symbols, emojis, or special characters. Keep all text plain and educational."
            )

            history = [
                {"role": "user",  "parts": [{"text": system_instruction}]},
                {"role": "model", "parts": [{"text": "Understood! I'll guide you step by step. Let's begin with the first section."}]},
            ]

            cur.execute(
                "INSERT INTO tutor_sessions (user_id, topic, history) VALUES (%s, %s, %s) RETURNING id",
                (user_id, topic, json.dumps(history))
            )
            session_id = cur.fetchone()["id"]

        conn.commit()

        ai_message = generate_with_fallback([
            *history,
            {"role": "user", "parts": [{"text": "Please start teaching the first section of the topic now."}]}
        ])

        messages = [m.strip() for m in ai_message.split("---MESSAGE_BREAK---") if m.strip()]
        if not messages:
            messages = [ai_message]

        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        cur.execute(
            "UPDATE tutor_sessions SET history = %s, updated_at = NOW() WHERE id = %s",
            (json.dumps(history), session_id)
        )
        conn.commit()
        cur.close(); conn.close()

        return jsonify({"success": True, "messages": messages, "topic": topic})
    except Exception as e:
        print(f"Tutor start error: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500


@app.route("/api/tutor/message", methods=["POST"])
def send_tutor_message():
    if "user" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data    = request.get_json()
    message = data.get("message")
    topic   = data.get("topic")

    if not message or not topic:
        return jsonify({"success": False, "message": "Message and topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT history FROM tutor_sessions WHERE user_id = %s AND topic = %s",
            (user_id, topic)
        )
        row = cur.fetchone()

        if not row:
            cur.close(); conn.close()
            return jsonify({"success": False, "message": "No active session for this topic"}), 404

        history = json.loads(row["history"])
        history.append({"role": "user", "parts": [{"text": message}]})

        ai_reply = generate_with_fallback(history)

        messages = [m.strip() for m in ai_reply.split("---MESSAGE_BREAK---") if m.strip()]
        if not messages:
            messages = [ai_reply]

        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        cur.execute(
            "UPDATE tutor_sessions SET history = %s, updated_at = NOW() WHERE user_id = %s AND topic = %s",
            (json.dumps(history), user_id, topic)
        )
        conn.commit()
        cur.close(); conn.close()

        return jsonify({"success": True, "messages": messages})
    except Exception as e:
        print(f"Tutor message error: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500


@app.route("/api/tutor/sessions")
def get_user_sessions():
    if "user" not in session:
        return jsonify([])

    user_id = get_current_user_id()
    if not user_id:
        return jsonify([])

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT topic, updated_at FROM tutor_sessions WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,)
        )
        sessions = cur.fetchall()
        cur.close(); conn.close()
        return jsonify([{"topic": s["topic"], "last_updated": str(s["updated_at"])} for s in sessions])
    except Exception as e:
        print(f"Get sessions error: {e}")
        return jsonify([])


@app.route("/api/tutor/sessions/<path:topic>", methods=["DELETE"])
def delete_tutor_session(topic):
    if "user" not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "DELETE FROM tutor_sessions WHERE user_id = %s AND topic = %s",
            (user_id, topic)
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Delete session error: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


if __name__ == "__main__":
    app.run(debug=True)
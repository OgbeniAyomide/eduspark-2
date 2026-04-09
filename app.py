from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import libsql_experimental as libsql
import json
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from datetime import datetime, timedelta
from flask_cors import CORS
from google import genai
from xai_sdk import Client
from dotenv import load_dotenv
import os

load_dotenv()


#===========BREVO===========
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
MAIL_FROM     = os.getenv("MAIL_FROM")
app = Flask(__name__)
app.secret_key = "fhdsshdhfskshfdskshffjjshhfsjwwjffhsahdhfeajoffkdmmvbvbsv"
CORS(app)

configuration = sib_api_v3_sdk.Configuration()
configuration.api_key['api-key'] = BREVO_API_KEY
api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

def send_email(to_email, subject, content):
    try:
        email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": to_email}],
            sender={"email": MAIL_FROM, "name": "Quevra"},
            subject=subject,
            html_content=content
        )

        response = api_instance.send_transac_email(email)
        print("Email sent:", response)

    except ApiException as e:
        print("Brevo error:", e)

# ==================== TURSO ====================
TURSO_URL       = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

def get_db():
    conn = libsql.connect(TURSO_URL, auth_token=TURSO_AUTH_TOKEN)
    return conn

# ==================== GEMINI ====================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is not set. Please check your .env file.")
client = genai.Client(api_key=GEMINI_API_KEY)

flash_model      = "gemini-2.5-flash"
flash_lite_model = "gemini-2.5-flash-lite"

def generate_with_fallback(contents):
    try:
        return client.models.generate_content(model=flash_model, contents=contents).text
    except Exception as e:
        print(f"Error with {flash_model}: {e}. Falling back to {flash_lite_model}.")
        try:
            return client.models.generate_content(model=flash_lite_model, contents=contents).text
        except Exception as e2:
            print("Flash LITE FAILED", e2)
            return "Service is currently unavailable. Please try again later."
        
#=============GROK=============
GROK_API_KEY =os.getenv("GROK_API_KEY")
if not GROK_API_KEY:
    raise ValueError("GROK_API_KEY environment variable is not set. Please check your .env file.")
grok_client = Client(
    api_key=GROK_API_KEY,
    timeout=3600,
)


# ==================== DB INIT ====================
def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT,
            email              TEXT UNIQUE,
            password           TEXT,
            level              TEXT,
            subjects           TEXT,
            reset_token        TEXT,
            reset_token_expiry TEXT
        )
    """)
    # Safe migrations for existing databases
    for col in ["reset_token TEXT", "reset_token_expiry TEXT"]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except Exception:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tutor_sessions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            topic      TEXT NOT NULL,
            history    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    conn.commit()

init_db()

# ==================== HELPERS ====================
def get_current_user_id():
    if 'user' not in session:
        return None
    conn = get_db()
    result = conn.execute("SELECT id FROM users WHERE email = ?", (session['user']['email'],)).fetchone()
    return result[0] if result else None

# ==================== PAGE ROUTES ====================

@app.route('/')
def index():
    return redirect(url_for('landing'))

@app.route('/landing')
def landing():
    return render_template('landing.html')


@app.route('/auth')
def auth():
    return render_template('auth.html')

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('auth'))
    return render_template('index.html', user=session['user'])

@app.route('/forgot-password')
def forgot_password_page():
    return render_template('forgot.html')

@app.route('/reset-password/<token>')
def reset_password_page(token):
    return render_template('reset.html', token=token)

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('auth'))

# ==================== AUTH API ====================

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request format"}), 400

    name     = data.get('name')
    email    = data.get('email')
    password = data.get('password')
    level    = data.get('level')
    subjects = ','.join(data.get('subjects', []))

    if not all([name, email, password, level]):
        return jsonify({"success": False, "message": "All fields are required"}), 400

    hashed = generate_password_hash(password)

    try:
        conn = get_db()
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            return jsonify({"success": False, "message": "User already exists"})
        conn.execute(
            "INSERT INTO users (name, email, password, level, subjects) VALUES (?, ?, ?, ?, ?)",
            (name, email, hashed, level, subjects)
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Signup error: {e}")
        return jsonify({"success": False, "message": "Server error during signup"}), 500


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request format"}), 400

    email    = data.get('email')
    password = data.get('password')

    try:
        conn = get_db()
        user = conn.execute(
            "SELECT name, email, password, level, subjects FROM users WHERE email = ?", (email,)
        ).fetchone()

        if user and check_password_hash(user[2], password):
            session['user'] = {
                "name":     user[0],
                "email":    user[1],
                "level":    user[3],
                "subjects": user[4].split(',') if user[4] else []
            }
            return jsonify({"success": True})
        return jsonify({"success": False, "message": "Invalid credentials"})
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({"success": False, "message": "Server error during login"}), 500


# ==================== FORGOT / RESET ====================

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    try:
        data = request.get_json(force=True, silent=True)
        if not data or 'email' not in data:
            return jsonify({"success": False, "message": "Email is required"})

        email = data.get('email')
        conn  = get_db()
        user  = conn.execute("SELECT email FROM users WHERE email = ?", (email,)).fetchone()

        if not user:
            return jsonify({"success": False, "message": "No account found with that email address."})

        token  = secrets.token_urlsafe(32)
        expiry = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_token_expiry = ? WHERE email = ?",
            (token, expiry, email)
        )
        conn.commit()

        reset_link = f"{request.url_root.rstrip('/')}/reset-password/{token}"
        html = f"""
        <h2>Password Reset</h2>
        <p>Click the link below to reset your password:</p>
        <a href="{reset_link}">Reset Password</a>
        <p>This link expires in 15 minutes.</p>
        """

        send_email(email, "Reset your Quevra password", html)

        return jsonify({
            "success": True,
            "message": "Password reset link sent to your email."
        })

    except Exception as e:
        print(f"Forgot password error: {e}")
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        })


@app.route('/api/reset-password/<token>', methods=['POST'])
def reset_password(token):
    try:
        data     = request.get_json()
        password = data.get('password')
        confirm  = data.get('confirm_password')

        if password != confirm:
            return jsonify({"success": False, "message": "Passwords do not match"})

        conn = get_db()
        user = conn.execute(
            "SELECT id, reset_token_expiry FROM users WHERE reset_token = ?", (token,)
        ).fetchone()

        if not user:
            return jsonify({"success": False, "message": "Invalid token"})

        expiry = user[1]
        if not expiry or datetime.fromisoformat(expiry) < datetime.utcnow():
            return jsonify({"success": False, "message": "Token expired. Please request a new link."})

        hashed = generate_password_hash(password)
        conn.execute(
            "UPDATE users SET password = ?, reset_token = NULL, reset_token_expiry = NULL WHERE id = ?",
            (hashed, user[0])
        )
        conn.commit()
        return jsonify({"success": True, "message": "Password reset successful"})
    except Exception as e:
        print(f"Reset error: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


#=============== ASK AI ====================
@app.route('/api/ask_ai/start', methods=['POST'])
def ask_ai_anything():
    if 'user' not in session:
        return jsonify({"success":False, "message":"Not loged in"}),401
    try:
        data = request.get_json()
        user_input = data.get('user_input')
        user_input = user_input.strip()
        if len(user_input) > 1000:
            return jsonify({"success": False, "message": "Input is too long. Please limit to 1000 characters."}), 400   
        if not user_input:
            return jsonify({"success": False, "message": "Input is required"}), 400
        response= grok_client.chat.completions.create(
            model="grok-4.20-reasoning",
            messages=[
                {"role": "system", "content": "You are an highly intelligent and helpful assistant named Quevra AI, designed to provide clear and concise answers to any questions asked by students. Always respond in a friendly, professional, and easy-to-understand manner, regardless of the topic. Your goal is to help students learn and understand concepts effectively."},
                {"role": "user", "content": user_input}
            ]
        )
        answer= response.choices[0].message.content
        return jsonify({"success": True, "answer":answer})
    except Exception as e :
        print("Grok Error:", e)
        return jsonify ({"success": False, "message": "Service is currently unavailable. Please try again later."}), 503

# ==================== AI TUTOR ====================

@app.route('/api/tutor/start', methods=['POST'])
def start_tutor_session():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data  = request.get_json()
    topic = data.get('topic')
    name = data.get('name')
    level = data.get('level')
    if not topic:
        return jsonify({"success": False, "message": "Topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn     = get_db()
        existing = conn.execute(
            "SELECT id, history FROM tutor_sessions WHERE user_id = ? AND topic = ?", (user_id, topic)
        ).fetchone()

        if existing:
            history    = json.loads(existing[1])
            session_id = existing[0]
        else:
            system_instruction = f"""
You are Quevra AI, an advanced academic assistant designed to teach students effectively.

You are to teach {topic} to {name}, who is currently at {level}.

IDENTITY & GREETING:
- Always introduce yourself as "Quevra AI"
- Always greet the student by name at the beginning of each session
- Make the greeting friendly, natural, and professional

EXAMPLE:
"Hello {name}, I am Quevra AI. Let us break down this topic together in a way that is clear and easy to understand."

TEACHING STYLE:
- Combine the clarity and conversational flow of ChatGPT with the depth and structure of an excellent lecturer
- Sound natural, human, and engaging
- Be clear and easy to follow, not robotic
- Maintain strong academic accuracy and authority
- Guide the student step-by-step like a teacher in class

NIGERIAN EDUCATION CONTEXT:
- Align explanations with WAEC, NECO, and GCE standards
- Focus on exam relevance and clarity
- Use familiar and relatable examples when possible

STRUCTURE (STRICTLY FOLLOW):
1. Definition
2. Key Concepts (with headings)
3. Examples
4. Table (if applicable)
5. Visual Explanation (if applicable)
6. Real-life Applications
7. Simple Summary

FORMATTING RULES:
- Use clear headings (##, ###)
- Use bullet points for clarity
- Avoid long paragraphs
- Make the response visually clean and easy to read

DEPTH CONTROL:
- Avoid being too shallow or too complex
- Explain difficult terms immediately after introducing them
- Build understanding progressively

VISUAL LEARNING:
- When diagrams or structures are involved:
  → Describe what the student should imagine
  → Use labels like: [Diagram: ...]
  → Keep explanations simple and visual

TABLE RULES:
- Use tables for comparisons, classifications, or summaries
- Keep tables clean and readable

TONE:
- Smart but simple
- Friendly but professional
- Confident, not overhyped

SESSION BEHAVIOR:
- Greet only at the beginning of a new session
- Continue naturally in follow-up responses without repeating full introduction

GOAL:
Deliver explanations that feel like a high-quality lesson—clear, structured, engaging, and tailored specifically for {name} to understand and succeed in exams.
"""

            history = [
                {"role": "user",  "parts": [{"text": system_instruction}]},
                {"role": "model", "parts": [{"text": "Understood! I'll guide you step by step. Let's begin with the first section."}]},
            ]

            conn.execute(
                "INSERT INTO tutor_sessions (user_id, topic, history) VALUES (?, ?, ?)",
                (user_id, topic, json.dumps(history))
            )
            conn.commit()
            session_id = conn.execute(
                "SELECT id FROM tutor_sessions WHERE user_id = ? AND topic = ?", (user_id, topic)
            ).fetchone()[0]

        conn.commit()

        ai_message = generate_with_fallback([
            *history,
            {"role": "user", "parts": [{"text": "Please start teaching the first section of the topic now."}]}
        ])

        messages = [m.strip() for m in ai_message.split('---MESSAGE_BREAK---') if m.strip()]
        if not messages:
            messages = [ai_message]

        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        conn.execute(
            "UPDATE tutor_sessions SET history = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(history), session_id)
        )
        conn.commit()

        return jsonify({"success": True, "messages": messages, "topic": topic})
    except Exception as e:
        print(f"Tutor start error: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500


@app.route('/api/tutor/message', methods=['POST'])
def send_tutor_message():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data    = request.get_json()
    message = data.get('message')
    topic   = data.get('topic')

    if not message or not topic:
        return jsonify({"success": False, "message": "Message and topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn = get_db()
        row  = conn.execute(
            "SELECT history FROM tutor_sessions WHERE user_id = ? AND topic = ?", (user_id, topic)
        ).fetchone()

        if not row:
            return jsonify({"success": False, "message": "No active session for this topic"}), 404

        history = json.loads(row[0])
        history.append({"role": "user", "parts": [{"text": message}]})

        ai_reply = generate_with_fallback(history)
        messages = [m.strip() for m in ai_reply.split('---MESSAGE_BREAK---') if m.strip()]
        if not messages:
            messages = [ai_reply]

        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        conn.execute(
            "UPDATE tutor_sessions SET history = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND topic = ?",
            (json.dumps(history), user_id, topic)
        )
        conn.commit()

        return jsonify({"success": True, "messages": messages})
    except Exception as e:
        print(f"Tutor message error: {e}")
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500


@app.route('/api/tutor/sessions')
def get_user_sessions():
    if 'user' not in session:
        return jsonify([])

    user_id = get_current_user_id()
    if not user_id:
        return jsonify([])

    try:
        conn     = get_db()
        sessions = conn.execute(
            "SELECT topic, updated_at FROM tutor_sessions WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
        return jsonify([{"topic": s[0], "last_updated": str(s[1])} for s in sessions])
    except Exception as e:
        print(f"Get sessions error: {e}")
        return jsonify([])


@app.route('/api/tutor/sessions/<path:topic>', methods=['DELETE'])
def delete_tutor_session(topic):
    if 'user' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    try:
        conn = get_db()
        conn.execute(
            "DELETE FROM tutor_sessions WHERE user_id = ? AND topic = ?", (user_id, topic)
        )
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"Delete session error: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


if __name__ == '__main__':
    app.run(debug=True)

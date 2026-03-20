from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import sqlite3
import json
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from flask_mail import Mail, Message
from datetime import datetime, timedelta
from flask_cors import CORS
from google import genai
from dotenv import load_dotenv
import os


load_dotenv()

app = Flask(__name__)
app.secret_key = "fhdsshdhfskshfdskshffjjshhfsjwwjffhsahdhfeajoffkdmmvbvbsv"
CORS(app)


app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = 'akinrolayoayo@gmail.com'
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')

mail=Mail(app)

# ==================== GEMINI CONFIGURATION ====================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable is not set. Please check your .env file.")
client = genai.Client(api_key=GEMINI_API_KEY)

flash_model= "gemini-2.5-flash"
flash_lite_model= "gemini-2.5-flash-lite"

def generate_with_fallback(contents):
    try:
        response = client.models.generate_content(
            model=flash_model,
            contents=contents,
        )
        return response.text
    except Exception as e:
        print(f"Error with {flash_model}: {str(e)}. Falling back to {flash_lite_model}.")
        try:
            response = client.models.generate_content(
                model=flash_lite_model,
                contents=contents,
            )
            return response.text
        except Exception as e2:
            print("Flash LITE FAILED", e2)
            return"Service is currently unavailable. Please try again later.", "none"
# ==============================================================

def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,
            level TEXT,
            subjects TEXT,
            reset_token TEXT,
            reset_token_expiry TEXT

        )
    ''')

    # Tutor sessions table (stores Gemini-compatible history)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tutor_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            topic TEXT NOT NULL,
            history TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    conn.commit()
    conn.close()


init_db()


def get_current_user_id():
    if 'user' not in session:
        return None
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (session['user']['email'],))
    user = cursor.fetchone()
    conn.close()
    return user[0] if user else None


@app.route('/')
def index():
    return redirect(url_for('auth'))


@app.route('/auth')
def auth():
    return render_template('auth.html')


@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('auth'))
    user = session['user']
    return render_template('index.html', user=user)


@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request format"}), 400
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    hashed_password = generate_password_hash(password)
    
    level = data.get('level')
    subjects = ','.join(data.get('subjects', []))

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email=?", (email,))
    if cursor.fetchone():
        conn.close()
        return jsonify({"success": False, "message": "User already exists"})

    cursor.execute(
        "INSERT INTO users (name, email, password, level, subjects) VALUES (?, ?, ?, ?, ?)",
        (name, email, hashed_password, level, subjects)
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"success": False, "message": "Invalid request format"}), 400
    email = data.get('email')
    password = data.get('password')

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, email, password, level, subjects FROM users WHERE email=?",
        (email,)
    )
    user = cursor.fetchone()
    conn.close()

    if user and check_password_hash(user[2], password):
        user_data = {
            "name": user[0],
            "email": user[1],
            "level": user[3],
            "subjects": user[4].split(',') if user[4] else []
        }
        session['user'] = user_data
        return jsonify({"success": True})
    else:
        return jsonify({"success": False, "message": "Invalid credentials"})


@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('auth'))


# ===================== GEMINI AI TUTOR =====================

@app.route('/api/tutor/start', methods=['POST'])
def start_tutor_session():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    topic = data.get('topic')
    if not topic:
        return jsonify({"success": False, "message": "Topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, history FROM tutor_sessions WHERE user_id=? AND topic=?", (user_id, topic))
    existing = cursor.fetchone()

    if existing:
        history = json.loads(existing[1])
        session_id = existing[0]
    else:
        # New session with correct Gemini format
        system_instruction = (
            f"You are EduSpark AI, an intelligent tutor designed to teach secondary school students from Grade 7 to Grade 12. "
            f"You are currently helping a student learn about {topic}. "
            "EduSpark follows the Nigerian secondary school structure. "
            "Understand the grade mapping: "
            "Grade 7 = JSS1, Grade 8 = JSS2, Grade 9 = JSS3, "
            "Grade 10 = SS1, Grade 11 = SS2, Grade 12 = SS3. "
            "Always adjust your explanations to match the student's level. "
            "You teach subjects commonly taught in secondary schools including Mathematics, English Language, "
            "Basic Science, Basic Technology, Physics, Chemistry, Biology, Agricultural Science, Economics, "
            "Government, Geography, Civic Education, Social Studies, and Literature in English. "
            "Your explanations must always be appropriate for secondary school students, clear, structured, and educational. "
            "Teach like a supportive teacher, not a search engine. "
            "Always prioritize understanding instead of just giving answers. "
            "Use simple language suitable for students aged 11–18. "
            "Break complex ideas into smaller steps and use relatable examples. "
            "Avoid unnecessary academic jargon. "
            "When explaining a concept, structure your response clearly: "
            "First explain the concept in simple language. "
            "Then give a relatable real-life example. "
            "Then provide a short key takeaway summarizing the most important idea. "
            "When solving problems, always follow this method: "
            "Identify the concept being tested, "
            "show the step-by-step solution, "
            "give the final answer clearly, "
            "and then provide a similar practice question for the student to try. "
            "If the student asks for a definition, provide the definition, a short explanation, and an example. "
            "If the student asks for a summary, give a short summary, key points in bullet format, "
            "and one quick review question to check understanding. "
            "If the student asks for practice questions or a quiz, generate questions appropriate for their grade level. "
            "Prefer multiple-choice questions when possible and provide the answers separately after the questions. "
            "Avoid extremely difficult or university-level questions. "
            "Never give only the final answer without explanation. "
            "Always show how the answer was obtained. "
            "Keep your tone friendly, encouraging, clear, and educational. "
            "Avoid overly technical explanations and long unnecessary paragraphs. "
            "Keep responses easy to read with clear structure such as Concept, Steps, Answer, Example, or Practice Question. "
            "Your main goal is to help the student truly understand the topic, not just finish homework. "
            # --- Response format instructions ---
            "Always break your teaching into exactly 3 smaller messages per concept or section. "
            "Each message should be 6-7 lines maximum. "
            "Format your response with clear separators between messages using '---MESSAGE_BREAK---' between each message. "
            "Send all 3 messages in one response, separated by these breaks. "
            "After every 3 messages, pause and ask the student if they understand the content so far. "
            "At the end of the lesson, ask useful questions such as: "
            "'Do you want me to give you a quiz?', "
            "'Should I explain any part again?', "
            "or 'Do you want more examples or practice questions?'. "
            "Avoid using any unnecessary symbols, emojis, or special characters in your responses. "
            "Keep all text plain and educational. "
        )

        history = [
            {"role": "user", "parts": [{"text": system_instruction}]},
            {
                "role": "model",
                "parts": [
                    {"text": "Understood! I'll guide you step by step. Let's begin with the first section."}
                ],
            },
        ]

        cursor.execute(
            "INSERT INTO tutor_sessions (user_id, topic, history) VALUES (?, ?, ?)",
            (user_id, topic, json.dumps(history)),
        )
        session_id = cursor.lastrowid

    conn.commit()
    conn.close()

    try:
        ai_message = generate_with_fallback([
            *history,
            {"role": "user", "parts": [{"text": "Please start teaching the first section of the topic now."}]}
            ])

        # Split the response into multiple messages if it contains breaks
        messages = [msg.strip() for msg in ai_message.split('---MESSAGE_BREAK---') if msg.strip()]

        # If no breaks found, treat as single message
        if len(messages) <= 1:
            messages = [ai_message]

        # Append all messages to history
        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        # Save updated history
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("UPDATE tutor_sessions SET history=? WHERE id=?", (json.dumps(history), session_id))
        conn.commit()
        conn.close()

        return jsonify({"success": True, "messages": messages, "topic": topic})

    except Exception as e:
        return jsonify({"success": False, "message": f"Gemini error: {str(e)}"}), 500


@app.route('/api/tutor/message', methods=['POST'])
def send_tutor_message():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Not logged in"}), 401

    data = request.get_json()
    message = data.get('message')
    topic = data.get('topic')
    if not message or not topic:
        return jsonify({"success": False, "message": "Message and topic required"}), 400

    user_id = get_current_user_id()
    if not user_id:
        return jsonify({"success": False, "message": "User not found"}), 404

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT history FROM tutor_sessions WHERE user_id=? AND topic=?", (user_id, topic))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({"success": False, "message": "No active session for this topic"}), 404

    history = json.loads(row[0])

    # Add user's message in correct Gemini format
    history.append({"role": "user", "parts": [{"text": message}]})

    try:
        ai_reply=generate_with_fallback(history)


        # Split the response into multiple messages if it contains breaks
        messages = [msg.strip() for msg in ai_reply.split('---MESSAGE_BREAK---') if msg.strip()]

        # If no breaks found, treat as single message
        if len(messages) <= 1:
            messages = [ai_reply]

        # Save all messages to history
        for msg in messages:
            history.append({"role": "model", "parts": [{"text": msg}]})

        # Update database
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tutor_sessions SET history=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND topic=?",
            (json.dumps(history), user_id, topic)
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "messages": messages})

    except Exception as e:
        return jsonify({"success": False, "message": f"Gemini error: {str(e)}"}), 500

@app.route('/api/tutor/sessions')
def get_user_sessions():
    if 'user' not in session:
        return jsonify([])

    user_id = get_current_user_id()
    if not user_id:
        return jsonify([])

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute("SELECT topic, updated_at FROM tutor_sessions WHERE user_id=? ORDER BY updated_at DESC", (user_id,))
    sessions = cursor.fetchall()
    conn.close()

    return jsonify([{"topic": s[0], "last_updated": str(s[1])} for s in sessions])


@app.route('/api/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    data=request.get_json(force=True,Silent=True)
    email = data.get('email')

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute("SELECT email FROM users WHERE email=?", (email,))
    user=cursor.fetchone()

    if user:
        token=secrets.token_urlsafe(32)
        expiry=(datetime.utcnow() + timedelta(minutes=15)).isoformat()
        cursor.execute(
            "UPDATE users SET reset_token=?, reset_token_expiry=? WHERE email=?",
            (token, expiry, email),
        )
        conn.commit()
        reset_link=f"eduspark.up.railway.app/reset-password/{token}"

        msg=Message(
            subject="Reset your password",
            sender=app.config['MAIL_USERNAME'],
            recipients=[email],
        )
        
        msg.body = f"Click the link to reset your password (valid for 15 minutes): {reset_link}"
        mail.send(msg)
        conn.close()

        return jsonify({"success": True, "message": "Password reset link sent to your email."})
    

@app.route('/api/reset-password/<token>', methods=['POST'])
def reset_password(token):
    data = request.get_json()
    password = data.get('password')
    confirm = data.get('confirm_password')

    if password != confirm:
        return jsonify({"success": False, "message": "Passwords do not match"})

    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, reset_token_expiry FROM users WHERE reset_token=?",
        (token,)
    )
    user = cursor.fetchone()

    if not user:
        conn.close()
        return jsonify({"success": False, "message": "Invalid token"})

    expiry = user[1]

    if not expiry or datetime.fromisoformat(expiry) < datetime.utcnow():
        conn.close()
        return jsonify({"success": False, "message": "Token expired"})

    hashed_password = generate_password_hash(password)

    cursor.execute("""
        UPDATE users 
        SET password=?, reset_token=NULL, reset_token_expiry=NULL 
        WHERE id=?
    """, (hashed_password, user[0]))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "Password reset successful"})


if __name__ == '__main__':
    app.run(debug=True)

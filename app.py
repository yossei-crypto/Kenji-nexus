import os
import sqlite3
import requests
import uuid
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kenji_ia_2026_key')

# ==========================================
# ⚙️ CONFIGURAÇÕES
# ==========================================
SENHA_MESTRA = "1234" 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

def get_db():
    db_path = os.path.join(os.getcwd(), 'kenji_memory.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS conversas (id TEXT PRIMARY KEY, titulo TEXT, data DATETIME DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('CREATE TABLE IF NOT EXISTS mensagens (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id TEXT, role TEXT, content TEXT)')
        conn.commit()
init_db()

@app.route("/")
def index():
    if not session.get("authorized"): return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    if request.get_json().get("senha") == SENHA_MESTRA:
        session["authorized"] = True
        return jsonify({"status": "success"})
    return jsonify({"status": "denied"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/chat", methods=["POST"])
def chat():
    if not session.get("authorized"): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user_msg, conv_id = data.get("mensagem", ""), data.get("conversa_id")

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    # MUDANÇA DE NOME AQUI
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "Você é a Kenji IA, uma inteligência de elite. Responda de forma curta, técnica e hacker. Sempre use blocos de código Markdown para scripts."},
            {"role": "user", "content": user_msg}
        ]
    }
    
    res = requests.post(url, headers=headers, json=payload).json()
    response_text = res['choices'][0]['message']['content']

    with get_db() as conn:
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conv_id, "user", user_msg))
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conv_id, "assistant", response_text))
        conn.commit()

    return jsonify({"resposta": response_text})

@app.route("/carregar_conversas")
def carregar_conversas():
    with get_db() as conn:
        return jsonify([dict(r) for r in conn.execute("SELECT id, titulo FROM conversas ORDER BY data DESC").fetchall()])

@app.route("/carregar_historico/<conversa_id>")
def carregar_historico(conversa_id):
    with get_db() as conn:
        return jsonify([dict(r) for r in conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (conversa_id,)).fetchall()])

@app.route("/nova_conversa", methods=["POST"])
def nova_conversa():
    nid = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (nid, f"Missão {nid}"))
        conn.commit()
    return jsonify({"id": nid})

if __name__ == "__main__":
    # O segredo está aqui: o Render define a porta, nós apenas a capturamos.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


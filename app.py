import os, sqlite3, requests, uuid, logging
from flask import Flask, render_template, request, jsonify, session, redirect, g
from datetime import timedelta
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "kenji_ia_ultra_secret_2026")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# Banco de dados em /tmp para o Render não dar erro de permissão
DB_PATH = "/tmp/kenji_memory.db"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None: db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversas (id TEXT PRIMARY KEY, titulo TEXT, atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS mensagens (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id TEXT, role TEXT, content TEXT);
        """)
init_db()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"): return jsonify({"error": "Não autorizado"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route("/")
def index():
    if not session.get("auth"): return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    if data.get("senha") == "32442356": # Sua senha definida
        session.permanent = True
        session["auth"] = True
        return jsonify({"status": "ok"})
    return jsonify({"status": "erro"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.json
    msg, cid = data.get("mensagem"), data.get("conversa_id")
    api_key = os.environ.get("GROQ_API_KEY")

    # Histórico para contexto
    db = get_db()
    rows = db.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT 10", (cid,)).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "system", "content": "Você é a Kenji IA, criada por @cybernmap. Seja técnico e hacker."}] + history + [{"role": "user", "content": msg}]
    }
    
    try:
        res = requests.post(GROQ_API_URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=30)
        resposta = res.json()['choices'][0]['message']['content']
    except:
        resposta = "❌ Falha na conexão neural."

    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'user', ?)", (cid, msg))
    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'assistant', ?)", (cid, resposta))
    db.execute("UPDATE conversas SET atualizado_em = CURRENT_TIMESTAMP WHERE id = ?", (cid,))
    db.commit()
    return jsonify({"resposta": resposta})

@app.route("/carregar_conversas")
@login_required
def carregar():
    db = get_db()
    rows = db.execute("SELECT id, titulo FROM conversas ORDER BY atualizado_em DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/carregar_historico/<cid>")
@login_required
def historico(cid):
    db = get_db()
    rows = db.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (cid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/nova_conversa", methods=["POST"])
@login_required
def nova():
    nid = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (nid, f"Missão {nid}"))
    db.commit()
    return jsonify({"id": nid})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

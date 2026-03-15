import os, sqlite3, requests, uuid
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
# Chave de segurança para sessões
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "kenji_ia_super_secret_2026")

# Banco de dados na pasta temporária do Render (evita erros de permissão)
DB_PATH = "/tmp/kenji_memory.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
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
    if not session.get("auth"): return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    if data.get("senha") == "1234":
        session["auth"] = True
        return jsonify({"status": "ok"})
    return jsonify({"status": "erro"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/chat", methods=["POST"])
def chat():
    if not session.get("auth"): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    msg, cid = data.get("mensagem"), data.get("conversa_id")
    
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return jsonify({"resposta": "⚠️ ERRO: Configure a GROQ_API_KEY no Render!"})

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "Você é a Kenji IA, uma inteligência de elite. Seja direto e técnico."},
            {"role": "user", "content": msg}
        ]
    }
    
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=15)
        resposta = res.json()['choices'][0]['message']['content']
    except Exception as e:
        resposta = f"❌ Falha Neural: {str(e)}"

    with get_db() as conn:
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (cid, "user", msg))
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (cid, "assistant", resposta))
        conn.commit()
    return jsonify({"resposta": resposta})

@app.route("/carregar_conversas")
def carregar():
    with get_db() as conn:
        return jsonify([dict(r) for r in conn.execute("SELECT id, titulo FROM conversas ORDER BY data DESC").fetchall()])

@app.route("/carregar_historico/<cid>")
def historico(cid):
    with get_db() as conn:
        return jsonify([dict(r) for r in conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (cid,)).fetchall()])

@app.route("/nova_conversa", methods=["POST"])
def nova():
    nid = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (nid, f"Missão {nid}"))
        conn.commit()
    return jsonify({"id": nid})

# --- O CORAÇÃO DO DEPLOY ---
if __name__ == "__main__":
    # Captura a porta do Render (ou usa 5000 se local)
    port = int(os.environ.get("PORT", 5000))
    # Importante: host deve ser 0.0.0.0
    app.run(host="0.0.0.0", port=port)

import os, sqlite3, requests, uuid
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = "kenji_ia_ultra_secret_key_2026"

# CONFIGURAÇÕES TÁTICAS
SENHA_MESTRA = "1234"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DB_PATH = "/tmp/kenji_memory.db" # Pasta temporária do Render para evitar erros de escrita

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
    if request.get_json().get("senha") == SENHA_MESTRA:
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
    
    # Motor de Resposta
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "Você é a Kenji IA, criada por @cybernmap. Use tom hacker, direto e técnico. Sempre use blocos de código markdown."},
            {"role": "user", "content": msg}
        ]
    }
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload).json()
        resposta = res['choices'][0]['message']['content']
    except:
        resposta = "❌ Erro na conexão neural. Verifique a API Key no Render."

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

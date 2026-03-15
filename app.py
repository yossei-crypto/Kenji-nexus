import os, sqlite3, requests, uuid, sys
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kenji_ia_secret_2026')

# --- LOG DE INICIALIZAÇÃO ---
print(">>> [SISTEMA] INICIANDO KENJI IA...")

# Banco de dados no /tmp para evitar erros de permissão no Render
DB_PATH = "/tmp/kenji_memory.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

try:
    with get_db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS conversas (id TEXT PRIMARY KEY, titulo TEXT, data DATETIME DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('CREATE TABLE IF NOT EXISTS mensagens (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id TEXT, role TEXT, content TEXT)')
        conn.commit()
    print(">>> [SISTEMA] BANCO DE DATOS VINCULADO.")
except Exception as e:
    print(f">>> [ERRO] FALHA NO BANCO: {e}")

@app.route("/")
def index():
    if not session.get("auth"):
        return render_template("login.html")
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
        return jsonify({"resposta": "⚠️ ERRO: Configure a GROQ_API_KEY no painel do Render!"})

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "Você é a Kenji IA. Seja técnico, hacker e direto. Use Markdown."},
            {"role": "user", "content": msg}
        ]
    }
    
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=10
        )
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f">>> [SISTEMA] KENJI IA ONLINE NA PORTA {port}")
    app.run(host="0.0.0.0", port=port)

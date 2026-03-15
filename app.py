import os
import sqlite3
import requests
import uuid
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kenji_nexus_final_boss')

# ==========================================
# ⚙️ CONFIGURAÇÃO DE AMBIENTE
# ==========================================
SENHA_MESTRA = "1234" 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

def get_db():
    # Isso garante que o banco funcione tanto no Render quanto no Kali
    db_path = os.path.join(os.getcwd(), 'memoria_nexus.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS conversas (id TEXT PRIMARY KEY, titulo TEXT, data DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS mensagens (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id TEXT, role TEXT, content TEXT)''')
        conn.commit()
init_db()

# ==========================================
# 🧠 MOTOR TÁTICO
# ==========================================
class KenjiEngine:
    @staticmethod
    def generate_text(prompt, history):
        if not GROQ_API_KEY:
            return "❌ ERRO: Chave API não configurada no Render (Environment Variables)."
            
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        
        system_msg = {"role": "system", "content": "Você é o Kenji Nexus, IA de elite de @cybernmap. Seja direto, técnico e use blocos de código."}
        
        payload = {
            "model": "llama-3.3-70b-versatile", 
            "messages": [system_msg] + history + [{"role": "user", "content": prompt}], 
            "temperature": 0.6
        }
        
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=25)
            return res.json()['choices'][0]['message']['content']
        except Exception as e:
            return f"❌ Falha na conexão com a Groq: {str(e)}"

# ==========================================
# 🛣️ ROTAS (FIX DE ATUALIZAÇÃO)
# ==========================================
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

@app.route("/chat", methods=["POST"])
def chat():
    if not session.get("authorized"): return jsonify({"error": "Unauthorized"}), 403
    data = request.json
    user_msg = data.get("mensagem", "").strip()
    conversa_id = data.get("conversa_id")

    with get_db() as conn:
        cur = conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT 6", (conversa_id,))
        history = [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()][::-1]

    response_text = KenjiEngine.generate_text(user_msg, history)

    with get_db() as conn:
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conversa_id, "user", user_msg))
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conversa_id, "assistant", response_text))
        conn.commit()

    return jsonify({"resposta": response_text})

@app.route("/carregar_conversas")
def carregar_conversas():
    with get_db() as conn:
        cur = conn.execute("SELECT id, titulo FROM conversas ORDER BY data DESC")
        return jsonify([dict(r) for r in cur.fetchall()])

@app.route("/carregar_historico/<conversa_id>")
def carregar_historico(conversa_id):
    with get_db() as conn:
        cur = conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (conversa_id,))
        return jsonify([dict(r) for r in cur.fetchall()])

@app.route("/nova_conversa", methods=["POST"])
def nova_conversa():
    new_id = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (new_id, "Nova Missão"))
        conn.commit()
    return jsonify({"id": new_id})

# CONFIGURAÇÃO DE PORTA DINÂMICA PARA O RENDER
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

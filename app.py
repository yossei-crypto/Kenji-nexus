import os
import sqlite3
import requests
import uuid
from flask import Flask, render_template, request, jsonify, session, redirect

app = Flask(__name__)
# O segredo agora vem do ambiente ou de um fallback seguro
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kenji_nexus_ultra_secret')

# ==========================================
# ⚙️ CONFIGURAÇÕES DE API (MODO SEGURO)
# ==========================================
SENHA_MESTRA = "32442356" 

# No GitHub/Render, você vai configurar uma variável chamada GROQ_API_KEY
# Se estiver testando local, ele tenta pegar a chave; se não achar, avisa no chat.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "COLOQUE_SUA_CHAVE_AQUI_PARA_TESTE_LOCAL")

# ==========================================
# 🗄️ BANCO DE DADOS
# ==========================================
def get_db():
    # Caminho adaptável para Nuvem ou Local
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

class KenjiEngine:
    @staticmethod
    def generate_text(prompt, history):
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        
        system_msg = {
            "role": "system", 
            "content": "Você é o Kenji Nexus, IA tática criada por @cybernmap. Responda de forma fria, técnica e objetiva. Use blocos de código markdown para comandos."
        }
        
        payload = {
            "model": "llama-3.3-70b-versatile", 
            "messages": [system_msg] + history + [{"role": "user", "content": prompt}], 
            "temperature": 0.5
        }
        
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=20)
            res.raise_for_status()
            return res.json()['choices'][0]['message']['content']
        except Exception as e:
            return f"❌ Erro de Sistema: {str(e)}. Verifique se a chave GROQ_API_KEY foi configurada nas variáveis de ambiente."

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
    user_msg = data.get("mensagem", "")
    conversa_id = data.get("conversa_id")

    with get_db() as conn:
        # Pega as últimas mensagens para contexto
        cur = conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT 6", (conversa_id,))
        history = [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()][::-1]

    response = KenjiEngine.generate_text(user_msg, history)

    with get_db() as conn:
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conversa_id, "user", user_msg))
        conn.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, ?, ?)", (conversa_id, "assistant", response))
        conn.commit()

    return jsonify({"resposta": response})

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

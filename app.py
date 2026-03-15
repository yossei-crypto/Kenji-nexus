import os
import sqlite3
import requests
import uuid
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

app = Flask(__name__)
# Chave secreta para manter as sessões seguras
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'kenji_nexus_ultra_secret_2026')

# ==========================================
# ⚙️ CONFIGURAÇÕES DE ELITE
# ==========================================
SENHA_MESTRA = "32442356" 
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

def get_db():
    # Define o caminho do banco de dados na raiz do projeto
    db_path = os.path.join(os.getcwd(), 'nexus_intelligence.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS conversas 
                        (id TEXT PRIMARY KEY, titulo TEXT, data DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS mensagens 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, conversa_id TEXT, role TEXT, content TEXT)''')
        conn.commit()

# Inicializa o banco de dados ao ligar o sistema
init_db()

# ==========================================
# 🧠 MOTOR DE INTELIGÊNCIA (GROQ)
# ==========================================
class KenjiEngine:
    @staticmethod
    def generate_response(prompt, history):
        if not GROQ_API_KEY:
            return "⚠️ ERRO CRÍTICO: GROQ_API_KEY não detectada nas variáveis de ambiente do Render."
            
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Personalidade do Kenji Nexus
        system_prompt = {
            "role": "system", 
            "content": "Você é o Kenji Nexus, uma IA de elite criada por @cybernmap. Responda de forma técnica, direta e use estilo hacker/terminal. Use blocos de código sempre que necessário."
        }
        
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [system_prompt] + history + [{"role": "user", "content": prompt}],
            "temperature": 0.7
        }
        
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=30)
            return res.json()['choices'][0]['message']['content']
        except Exception as e:
            return f"❌ Falha na conexão neural: {str(e)}"

# ==========================================
# 🛣️ ROTAS DE COMANDO
# ==========================================

@app.route("/")
def index():
    if not session.get("authorized"):
        return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    if data.get("senha") == SENHA_MESTRA:
        session["authorized"] = True
        return jsonify({"status": "success"})
    return jsonify({"status": "denied"}), 401

@app.route("/logout")
def logout():
    session.clear() # Limpa o acesso
    return redirect("/") # Volta para o login

@app.route("/chat", methods=["POST"])
def chat():
    if not session.get("authorized"):
        return jsonify({"error": "Acesso negado"}), 403
        
    data = request.json
    user_msg = data.get("mensagem", "").strip()
    conversa_id = data.get("conversa_id")

    # Recupera contexto recente da conversa
    with get_db() as conn:
        cur = conn.execute("SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT 10", (conversa_id,))
        history = [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()][::-1]

    # Gera resposta via IA
    response_text = KenjiEngine.generate_response(user_msg, history)

    # Salva no banco de dados
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
        conn.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (new_id, f"Missão {new_id}"))
        conn.commit()
    return jsonify({"id": new_id})

# ==========================================
# 🚀 ATIVAÇÃO DO SERVIDOR
# ==========================================
if __name__ == "__main__":
    # Ajuste automático para o Render ou Local
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

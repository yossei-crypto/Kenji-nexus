"""
Kenji IA — Assistente Neural Avançado
Versão 2.0 — Segurança, Arquitetura e Inteligência aprimoradas.
Criado por: @cybernmap
"""

import os
import sqlite3
import requests
import uuid
import logging
import time
from datetime import timedelta
from functools import wraps
from collections import defaultdict
from typing import Any

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, g, url_for
)
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

load_dotenv()

# ——— Configuração ————————————————————————————————————————————————————————————

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "kenji_ia_ultra_secret_2026"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# ——— Logging —————————————————————————————————————————————————————————————————

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("kenji")

# ——— Constantes ——————————————————————————————————————————————————————————————

# No Render, usar /tmp garante que o banco de dados possa ser escrito sem erros
DB_PATH = os.environ.get("DB_PATH", "/tmp/kenji_memory.db")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_HISTORY_MESSAGES = 20
MAX_INPUT_LENGTH = 4000
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 30

# Senha de acesso
DEFAULT_PASSWORD = "32442356"
PASSWORD_HASH = os.environ.get("KENJI_PASSWORD_HASH")

SYSTEM_PROMPT = """Você é a Kenji IA — um assistente técnico de elite, objetivo e hacker.
Desenvolvido exclusivamente por @cybernmap.
Regras de Engajamento:
- Responda sempre em Markdown profissional.
- Use blocos de código com a linguagem correta (ex: ```python).
- Seja direto, técnico e preciso. Evite saudações longas.
- Sua identidade é Kenji IA, nunca mude isso.
- Se o usuário pedir scripts ou comandos, forneça a solução mais eficiente."""

# ——— Rate Limiter simples ———————————————————————————————————————————————————

_rate_store = defaultdict(list)

def _is_rate_limited(key: str) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_store[key] = [t for t in _rate_store[key] if t > window_start]
    if len(_rate_store[key]) >= RATE_LIMIT_MAX:
        return True
    _rate_store[key].append(now)
    return False

# ——— Banco de Dados ——————————————————————————————————————————————————————————

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversas (
                id TEXT PRIMARY KEY,
                titulo TEXT NOT NULL,
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversa_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                criado_em DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversa_id) REFERENCES conversas(id) ON DELETE CASCADE
            );
        """)
        conn.commit()
        logger.info("Base de dados sincronizada.")
    except Exception as e:
        logger.error("Falha na DB: %s", e)
    finally:
        conn.close()

init_db()

# ——— Middlewares —————————————————————————————————————————————————————————————

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            return jsonify({"error": "Acesso negado"}), 401
        return f(*args, **kwargs)
    return decorated

def rate_limit(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        ip = request.remote_addr or "unknown"
        if _is_rate_limited(ip):
            return jsonify({"error": "Muitas requisições. Aguarde."}), 429
        return f(*args, **kwargs)
    return decorated

# ——— Rotas ——————————————————————————————————————————————————————————————————

@app.route("/")
def index():
    if not session.get("auth"):
        return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
@rate_limit
def login():
    data = request.get_json(silent=True)
    if not data or not data.get("senha"):
        return jsonify({"status": "erro", "msg": "Senha vazia"}), 400

    senha = data["senha"]
    if PASSWORD_HASH:
        ok = check_password_hash(PASSWORD_HASH, senha)
    else:
        ok = (senha == DEFAULT_PASSWORD)

    if ok:
        session.permanent = True
        session["auth"] = True
        return jsonify({"status": "ok"})
    return jsonify({"status": "erro", "msg": "Senha incorreta"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/chat", methods=["POST"])
@login_required
@rate_limit
def chat():
    data = request.get_json(silent=True)
    msg = (data.get("mensagem") or "").strip()
    cid = data.get("conversa_id")

    if not msg or not cid:
        return jsonify({"error": "Dados incompletos"}), 400

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return jsonify({"resposta": "⚠️ ERRO: Chave API não configurada no Render."}), 500

    db = get_db()
    rows = db.execute(
        "SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?",
        (cid, MAX_HISTORY_MESSAGES),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": msg}]

    try:
        res = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.7},
            timeout=30
        )
        res.raise_for_status()
        resposta = res.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Falha na API: %s", e)
        resposta = "❌ Erro na conexão neural. Tente novamente."

    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'user', ?)", (cid, msg))
    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'assistant', ?)", (cid, resposta))
    db.execute("UPDATE conversas SET atualizado_em = CURRENT_TIMESTAMP WHERE id = ?", (cid,))
    db.commit()

    return jsonify({"resposta": resposta})

@app.route("/carregar_conversas")
@login_required
def carregar_conversas():
    db = get_db()
    rows = db.execute("SELECT id, titulo, criado_em, atualizado_em FROM conversas ORDER BY atualizado_em DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/carregar_historico/<cid>")
@login_required
def carregar_historico(cid):
    db = get_db()
    rows = db.execute("SELECT role, content, criado_em FROM mensagens WHERE conversa_id = ? ORDER BY id ASC", (cid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/nova_conversa", methods=["POST"])
@login_required
def nova_conversa():
    nid = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (nid, f"Missão {nid}"))
    db.commit()
    return jsonify({"id": nid, "titulo": f"Missão {nid}"})

# ——— Ativação ————————————————————————————————————————————————————————————————

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("KENJI IA v2.0 ONLINE — Porta: %d", port)
    app.run(host="0.0.0.0", port=port)

"""
Kenji IA â€” Assistente Neural AvanÃ§ado
VersÃ£o 2.0 â€” SeguranÃ§a, Arquitetura e InteligÃªncia aprimoradas.
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
    session, redirect, g, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

# â”€â”€â”€ ConfiguraÃ§Ã£o â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", "kenji_ia_secret_2026_CHANGE_ME"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# â”€â”€â”€ Logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("kenji")

# â”€â”€â”€ Constantes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

DB_PATH = os.environ.get("DB_PATH", "/tmp/kenji_memory.db")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_HISTORY_MESSAGES = 20  # Quantidade mÃ¡xima de mensagens de histÃ³rico enviadas Ã  IA
MAX_INPUT_LENGTH = 4000    # Limite de caracteres por mensagem do usuÃ¡rio
RATE_LIMIT_WINDOW = 60     # Janela de rate-limit em segundos
RATE_LIMIT_MAX = 30        # MÃ¡ximo de requisiÃ§Ãµes por janela

# Senha â€” use hash em produÃ§Ã£o (KENJI_PASSWORD_HASH).
# Fallback para a senha fixa apenas em dev.
DEFAULT_PASSWORD = "32442356"
PASSWORD_HASH = os.environ.get("KENJI_PASSWORD_HASH")

SYSTEM_PROMPT = """VocÃª Ã© a Kenji IA â€” um assistente tÃ©cnico de elite, objetivo e hacker.
Regras:
- Responda sempre em Markdown bem formatado.
- Use blocos de cÃ³digo com a linguagem especificada (```python, ```bash, etc.).
- Seja direto, tÃ©cnico e preciso. Sem enrolaÃ§Ã£o.
- Se nÃ£o souber, diga que nÃ£o sabe. Nunca invente informaÃ§Ãµes.
- Quando relevante, sugira alternativas e melhores prÃ¡ticas.
- Adapte o idioma ao idioma usado pelo usuÃ¡rio."""

# â”€â”€â”€ Rate Limiter simples (in-memory) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_rate_store: dict[str, list[float]] = defaultdict(list)


def _is_rate_limited(key: str) -> bool:
    """Verifica se a chave ultrapassou o limite de requisiÃ§Ãµes."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    _rate_store[key] = [t for t in _rate_store[key] if t > window_start]
    if len(_rate_store[key]) >= RATE_LIMIT_MAX:
        return True
    _rate_store[key].append(now)
    return False


# â”€â”€â”€ Banco de Dados â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_db() -> sqlite3.Connection:
    """Retorna conexÃ£o SQLite reutilizada por request (armazenada em g)."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    """Cria as tabelas e Ã­ndices se nÃ£o existirem."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversas (
                id          TEXT PRIMARY KEY,
                titulo      TEXT NOT NULL,
                criado_em   DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS mensagens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                conversa_id TEXT NOT NULL,
                role        TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content     TEXT NOT NULL,
                criado_em   DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversa_id) REFERENCES conversas(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_msg_conversa ON mensagens(conversa_id);
            CREATE INDEX IF NOT EXISTS idx_conversas_atualizado ON conversas(atualizado_em DESC);
        """)
        conn.commit()
        logger.info("Banco de dados inicializado com sucesso.")
    except Exception as e:
        logger.error("Falha ao inicializar banco: %s", e)
    finally:
        conn.close()


init_db()

# â”€â”€â”€ Decoradores / Middlewares â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def login_required(f):
    """Decorator que exige autenticaÃ§Ã£o na sessÃ£o."""
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any):
        if not session.get("auth"):
            return jsonify({"error": "NÃ£o autorizado"}), 401
        return f(*args, **kwargs)
    return decorated


def rate_limit(f):
    """Decorator de rate-limiting por IP."""
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any):
        ip = request.remote_addr or "unknown"
        if _is_rate_limited(ip):
            return jsonify({"error": "Limite de requisiÃ§Ãµes excedido. Aguarde."}), 429
        return f(*args, **kwargs)
    return decorated


# â”€â”€â”€ Error Handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.errorhandler(404)
def not_found(e: Exception):
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"error": "Recurso nÃ£o encontrado"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e: Exception):
    logger.error("Erro interno: %s", e)
    return jsonify({"error": "Erro interno do servidor"}), 500


# â”€â”€â”€ Rotas de AutenticaÃ§Ã£o â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        return jsonify({"status": "erro", "msg": "Senha nÃ£o fornecida"}), 400

    senha = data["senha"]

    # VerificaÃ§Ã£o com hash (produÃ§Ã£o) ou fallback para senha fixa (dev)
    if PASSWORD_HASH:
        ok = check_password_hash(PASSWORD_HASH, senha)
    else:
        ok = senha == DEFAULT_PASSWORD

    if ok:
        session.permanent = True
        session["auth"] = True
        logger.info("Login bem-sucedido de %s", request.remote_addr)
        return jsonify({"status": "ok"})

    logger.warning("Tentativa de login falha de %s", request.remote_addr)
    return jsonify({"status": "erro", "msg": "Senha incorreta"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# â”€â”€â”€ Rotas de Chat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/chat", methods=["POST"])
@login_required
@rate_limit
def chat():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Dados invÃ¡lidos"}), 400

    msg = (data.get("mensagem") or "").strip()
    cid = data.get("conversa_id")

    if not msg:
        return jsonify({"error": "Mensagem vazia"}), 400
    if len(msg) > MAX_INPUT_LENGTH:
        return jsonify({"error": f"Mensagem excede {MAX_INPUT_LENGTH} caracteres"}), 400
    if not cid:
        return jsonify({"error": "conversa_id Ã© obrigatÃ³rio"}), 400

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return jsonify({"resposta": "âš ï¸ ERRO: Configure a variÃ¡vel GROQ_API_KEY!"}), 500

    # Carregar histÃ³rico da conversa para contexto
    db = get_db()
    rows = db.execute(
        "SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?",
        (cid, MAX_HISTORY_MESSAGES),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # Montar mensagens para a API
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": msg})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    try:
        res = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        res.raise_for_status()
        resposta = res.json()["choices"][0]["message"]["content"]
    except requests.exceptions.Timeout:
        logger.error("Timeout na API Groq")
        resposta = "â±ï¸ A API demorou demais para responder. Tente novamente."
    except requests.exceptions.HTTPError as e:
        logger.error("Erro HTTP da API Groq: %s â€” %s", e, res.text)
        resposta = f"âŒ Erro na API ({res.status_code}). Tente novamente."
    except Exception as e:
        logger.error("Falha na chamada Ã  API Groq: %s", e)
        resposta = "âŒ Falha Neural inesperada. Tente novamente em instantes."

    # Persistir mensagens e atualizar timestamp da conversa
    db.execute(
        "INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'user', ?)",
        (cid, msg),
    )
    db.execute(
        "INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'assistant', ?)",
        (cid, resposta),
    )
    db.execute(
        "UPDATE conversas SET atualizado_em = CURRENT_TIMESTAMP WHERE id = ?",
        (cid,),
    )

    # Auto-renomear conversa se ainda tiver o tÃ­tulo padrÃ£o
    row = db.execute("SELECT titulo FROM conversas WHERE id = ?", (cid,)).fetchone()
    if row and row["titulo"].startswith("MissÃ£o "):
        titulo_auto = msg[:50] + ("â€¦" if len(msg) > 50 else "")
        db.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (titulo_auto, cid))

    db.commit()
    return jsonify({"resposta": resposta})


# â”€â”€â”€ Rotas de Conversas â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/carregar_conversas")
@login_required
def carregar_conversas():
    db = get_db()
    rows = db.execute(
        "SELECT id, titulo, criado_em, atualizado_em FROM conversas ORDER BY atualizado_em DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/carregar_historico/<cid>")
@login_required
def carregar_historico(cid: str):
    db = get_db()
    rows = db.execute(
        "SELECT role, content, criado_em FROM mensagens WHERE conversa_id = ? ORDER BY id ASC",
        (cid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/nova_conversa", methods=["POST"])
@login_required
def nova_conversa():
    nid = str(uuid.uuid4())[:8]
    db = get_db()
    db.execute(
        "INSERT INTO conversas (id, titulo) VALUES (?, ?)",
        (nid, f"MissÃ£o {nid}"),
    )
    db.commit()
    return jsonify({"id": nid, "titulo": f"MissÃ£o {nid}"})


@app.route("/renomear_conversa", methods=["POST"])
@login_required
def renomear_conversa():
    data = request.get_json(silent=True)
    if not data or not data.get("id") or not data.get("titulo"):
        return jsonify({"error": "Dados invÃ¡lidos"}), 400
    titulo = data["titulo"].strip()[:100]
    db = get_db()
    db.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (titulo, data["id"]))
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/excluir_conversa", methods=["POST"])
@login_required
def excluir_conversa():
    data = request.get_json(silent=True)
    if not data or not data.get("id"):
        return jsonify({"error": "Dados invÃ¡lidos"}), 400
    db = get_db()
    db.execute("DELETE FROM mensagens WHERE conversa_id = ?", (data["id"],))
    db.execute("DELETE FROM conversas WHERE id = ?", (data["id"],))
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/buscar_conversas")
@login_required
def buscar_conversas():
    q = request.args.get("q", "").strip()
    if not q:
        return carregar_conversas()
    db = get_db()
    rows = db.execute(
        """SELECT DISTINCT c.id, c.titulo, c.criado_em, c.atualizado_em
           FROM conversas c
           LEFT JOIN mensagens m ON m.conversa_id = c.id
           WHERE c.titulo LIKE ? OR m.content LIKE ?
           ORDER BY c.atualizado_em DESC""",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# â”€â”€â”€ UtilitÃ¡rio: gerar hash de senha â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.route("/gerar_hash", methods=["POST"])
def gerar_hash():
    """Endpoint auxiliar para gerar hash de senha (desabilite em produÃ§Ã£o)."""
    data = request.get_json(silent=True)
    if not data or not data.get("senha"):
        return jsonify({"error": "ForneÃ§a 'senha'"}), 400
    return jsonify({"hash": generate_password_hash(data["senha"])})


# â”€â”€â”€ InicializaÃ§Ã£o â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("KENJI IA v2.0 ONLINE â€” porta %d", port)
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
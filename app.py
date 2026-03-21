import os
import sqlite3
import uuid
import logging
import hashlib
import hmac
import time
from collections import defaultdict
from datetime import timedelta
from functools import wraps

import requests
from flask import Flask, render_template, request, jsonify, session, redirect, g

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

secret_key = os.environ.get("FLASK_SECRET_KEY")
if not secret_key:
    log.warning("FLASK_SECRET_KEY não definida — usando chave temporária (não use em produção)")
    secret_key = os.urandom(32).hex()

app.secret_key = secret_key
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Em produção com HTTPS, ative: app.config["SESSION_COOKIE_SECURE"] = True

# ── Configurações ─────────────────────────────────────────────────────────────
DB_PATH       = os.environ.get("DB_PATH", "/tmp/kenji_memory.db")
GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", 20))   # mensagens de contexto
MAX_MSG_LEN   = int(os.environ.get("MAX_MSG_LEN", 4000))    # chars por mensagem

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "Você é a Kenji IA, assistente técnico criado por @cybernmap. "
    "Seja direto, técnico e preciso. Use markdown quando útil.",
)

# ── Rate limiting simples (em memória) ───────────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(key: str, max_calls: int = 30, window: int = 60) -> bool:
    """Retorna True se a chave excedeu max_calls no janela (segundos)."""
    now = time.time()
    bucket = _rate_buckets[key]
    # remove entradas antigas
    _rate_buckets[key] = [t for t in bucket if now - t < window]
    if len(_rate_buckets[key]) >= max_calls:
        return True
    _rate_buckets[key].append(now)
    return False

# ── Banco de dados ────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversas (
                id          TEXT PRIMARY KEY,
                titulo      TEXT NOT NULL,
                criado_em   DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mensagens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                conversa_id TEXT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
                role        TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content     TEXT NOT NULL,
                criado_em   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_mensagens_conversa
                ON mensagens(conversa_id, id);
        """)
    log.info("Banco de dados inicializado em %s", DB_PATH)

init_db()

# ── Autenticação ──────────────────────────────────────────────────────────────
def _hash_senha(senha: str) -> str:
    """Hash SHA-256 com HMAC para comparação segura."""
    key = app.secret_key.encode()
    return hmac.new(key, senha.encode(), hashlib.sha256).hexdigest()

def _check_senha(senha: str) -> bool:
    senha_hash     = os.environ.get("SENHA_HASH")
    senha_plaintext = os.environ.get("SENHA")

    # Preferência: comparar com hash
    if senha_hash:
        return hmac.compare_digest(_hash_senha(senha), senha_hash)
    # Fallback: senha em texto puro (menos seguro)
    if senha_plaintext:
        return hmac.compare_digest(senha, senha_plaintext)

    log.error("Nenhuma senha configurada! Defina SENHA ou SENHA_HASH no ambiente.")
    return False

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("auth"):
            if request.is_json:
                return jsonify({"error": "Não autorizado"}), 401
            return redirect("/")
        return f(*args, **kwargs)
    return decorated

# ── Rotas: páginas ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if not session.get("auth"):
        return render_template("login.html")
    return render_template("index.html")

@app.route("/login", methods=["POST"])
def login():
    ip = request.remote_addr

    # Rate limit: 10 tentativas por minuto por IP
    if is_rate_limited(f"login:{ip}", max_calls=10, window=60):
        log.warning("Rate limit de login atingido para IP %s", ip)
        return jsonify({"status": "erro", "msg": "Muitas tentativas. Aguarde um momento."}), 429

    data = request.get_json(silent=True) or {}
    senha = data.get("senha", "")

    if not senha:
        return jsonify({"status": "erro", "msg": "Senha não informada."}), 400

    if _check_senha(senha):
        session.permanent = True
        session["auth"] = True
        log.info("Login bem-sucedido para IP %s", ip)
        return jsonify({"status": "ok"})

    log.warning("Tentativa de login falhou para IP %s", ip)
    # Delay leve para dificultar brute-force
    time.sleep(0.5)
    return jsonify({"status": "erro", "msg": "Senha incorreta."}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ── Rotas: API ────────────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
@login_required
def chat():
    ip = request.remote_addr
    if is_rate_limited(f"chat:{ip}", max_calls=30, window=60):
        return jsonify({"error": "Limite de requisições atingido."}), 429

    data = request.get_json(silent=True) or {}
    msg  = (data.get("mensagem") or "").strip()
    cid  = (data.get("conversa_id") or "").strip()

    if not msg:
        return jsonify({"error": "Mensagem vazia."}), 400
    if len(msg) > MAX_MSG_LEN:
        return jsonify({"error": f"Mensagem muito longa (máx {MAX_MSG_LEN} caracteres)."}), 400
    if not cid:
        return jsonify({"error": "conversa_id ausente."}), 400

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log.error("GROQ_API_KEY não configurada")
        return jsonify({"error": "Serviço temporariamente indisponível."}), 503

    db = get_db()

    # Verifica se a conversa existe
    conversa = db.execute("SELECT id FROM conversas WHERE id = ?", (cid,)).fetchone()
    if not conversa:
        return jsonify({"error": "Conversa não encontrada."}), 404

    # Histórico para contexto
    rows = db.execute(
        "SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id DESC LIMIT ?",
        (cid, HISTORY_LIMIT),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # Gera título automático na primeira mensagem
    is_first = len(history) == 0
    titulo_novo = None
    if is_first:
        titulo_novo = msg[:48] + ("…" if len(msg) > 48 else "")

    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": msg}],
        "max_tokens": 2048,
        "temperature": 0.7,
    }

    try:
        res = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        res.raise_for_status()
        resposta = res.json()["choices"][0]["message"]["content"]
    except requests.Timeout:
        log.error("Timeout na API Groq")
        return jsonify({"error": "O modelo demorou demais para responder. Tente novamente."}), 504
    except requests.HTTPError as e:
        log.error("Erro HTTP na API Groq: %s", e)
        return jsonify({"error": "Erro ao comunicar com o modelo."}), 502
    except (KeyError, IndexError, ValueError) as e:
        log.error("Resposta inesperada da API Groq: %s", e)
        return jsonify({"error": "Resposta inválida do modelo."}), 502

    # Persiste mensagens
    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'user', ?)", (cid, msg))
    db.execute("INSERT INTO mensagens (conversa_id, role, content) VALUES (?, 'assistant', ?)", (cid, resposta))
    db.execute("UPDATE conversas SET atualizado_em = CURRENT_TIMESTAMP WHERE id = ?", (cid,))
    if titulo_novo:
        db.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (titulo_novo, cid))
    db.commit()

    return jsonify({"resposta": resposta})


@app.route("/nova_conversa", methods=["POST"])
@login_required
def nova():
    nid = str(uuid.uuid4())
    db  = get_db()
    db.execute("INSERT INTO conversas (id, titulo) VALUES (?, ?)", (nid, "Nova missão"))
    db.commit()
    log.info("Nova conversa criada: %s", nid)
    return jsonify({"id": nid}), 201


@app.route("/carregar_conversas")
@login_required
def carregar():
    db   = get_db()
    rows = db.execute(
        "SELECT id, titulo FROM conversas ORDER BY atualizado_em DESC LIMIT 50"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/carregar_historico/<cid>")
@login_required
def historico(cid):
    db = get_db()
    conversa = db.execute("SELECT id FROM conversas WHERE id = ?", (cid,)).fetchone()
    if not conversa:
        return jsonify({"error": "Conversa não encontrada."}), 404

    rows = db.execute(
        "SELECT role, content FROM mensagens WHERE conversa_id = ? ORDER BY id ASC",
        (cid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/deletar_conversa/<cid>", methods=["DELETE"])
@login_required
def deletar(cid):
    db = get_db()
    conversa = db.execute("SELECT id FROM conversas WHERE id = ?", (cid,)).fetchone()
    if not conversa:
        return jsonify({"error": "Conversa não encontrada."}), 404

    db.execute("DELETE FROM conversas WHERE id = ?", (cid,))
    db.commit()
    log.info("Conversa deletada: %s", cid)
    return jsonify({"status": "ok"})


@app.route("/renomear_conversa/<cid>", methods=["PATCH"])
@login_required
def renomear(cid):
    data  = request.get_json(silent=True) or {}
    titulo = (data.get("titulo") or "").strip()
    if not titulo:
        return jsonify({"error": "Título vazio."}), 400
    if len(titulo) > 80:
        return jsonify({"error": "Título muito longo."}), 400

    db = get_db()
    conversa = db.execute("SELECT id FROM conversas WHERE id = ?", (cid,)).fetchone()
    if not conversa:
        return jsonify({"error": "Conversa não encontrada."}), 404

    db.execute("UPDATE conversas SET titulo = ? WHERE id = ?", (titulo, cid))
    db.commit()
    return jsonify({"status": "ok"})


# ── Healthcheck (para Render / uptime bots) ───────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": GROQ_MODEL}), 200


# ── Erros globais ─────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Rota não encontrada."}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Método não permitido."}), 405

@app.errorhandler(500)
def internal_error(e):
    log.exception("Erro interno: %s", e)
    return jsonify({"error": "Erro interno do servidor."}), 500


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Kenji IA iniciando na porta %d (debug=%s)", port, debug)
    app.run(host="0.0.0.0", port=port, debug=debug)

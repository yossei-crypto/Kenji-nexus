import os
import sqlite3
import uuid
import logging
import hashlib
import hmac
import time
import base64
from collections import defaultdict
from datetime import timedelta, datetime
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
    secret_key = os.urandom(32).hex()

app.secret_key = secret_key
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
app.config["SESSION_COOKIE_HTTPONLY"]    = True
app.config["SESSION_COOKIE_SAMESITE"]   = "Lax"
app.config["MAX_CONTENT_LENGTH"]        = 10 * 1024 * 1024  # 10MB

# ── Configurações ─────────────────────────────────────────────────────────────
DB_PATH       = os.environ.get("DB_PATH", "/tmp/kenji_memory.db")
GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", 20))
MAX_MSG_LEN   = int(os.environ.get("MAX_MSG_LEN", 4000))
ACESSO_DIAS   = 60  # 2 meses
VALOR_PIX     = "7,99"
CHAVE_PIX     = os.environ.get("CHAVE_PIX", "82a91d75-2eba-4fcb-abc8-9be7c27764ac")
ADMIN_WHATS   = os.environ.get("ADMIN_WHATS", "5585989365523")
ADMIN_SENHA   = os.environ.get("ADMIN_SENHA", os.environ.get("SENHA", "admin123"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "Você é a Kenji IA, criada por @cybernmap. "
    "Responda qualquer pergunta de forma direta e completa. "
    "Use markdown quando útil. Se receber uma imagem, analise com detalhes.",
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_buckets: dict[str, list[float]] = defaultdict(list)

def is_rate_limited(key: str, max_calls: int = 30, window: int = 60) -> bool:
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < window]
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
    if db: db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                senha_hash    TEXT NOT NULL,
                nome          TEXT,
                status        TEXT NOT NULL DEFAULT 'pendente',
                acesso_ate    DATETIME,
                criado_em     DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pagamentos (
                id              TEXT PRIMARY KEY,
                usuario_id      TEXT NOT NULL REFERENCES usuarios(id),
                comprovante_b64 TEXT,
                comprovante_ext TEXT,
                status          TEXT NOT NULL DEFAULT 'aguardando',
                criado_em       DATETIME DEFAULT CURRENT_TIMESTAMP,
                aprovado_em     DATETIME
            );

            CREATE TABLE IF NOT EXISTS conversas (
                id            TEXT PRIMARY KEY,
                usuario_id    TEXT NOT NULL REFERENCES usuarios(id),
                titulo        TEXT NOT NULL,
                criado_em     DATETIME DEFAULT CURRENT_TIMESTAMP,
                atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS mensagens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                conversa_id TEXT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
                role        TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content     TEXT NOT NULL,
                criado_em   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_msgs ON mensagens(conversa_id, id);
        """)
    log.info("DB inicializado em %s", DB_PATH)

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────
def hash_senha(senha: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), app.secret_key.encode(), 100000).hex()

def check_senha(senha: str, stored: str) -> bool:
    return hmac.compare_digest(hash_senha(senha), stored)

def usuario_ativo(u) -> bool:
    if u["status"] != "ativo":
        return False
    if u["acesso_ate"]:
        ate = datetime.fromisoformat(u["acesso_ate"])
        if datetime.utcnow() > ate:
            return False
    return True

# ── Decorators ────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("usuario_id"):
            ct = request.content_type or ""
            if request.is_json or "multipart" in ct:
                return jsonify({"error": "Não autorizado"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

def acesso_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("usuario_id")
        if not uid:
            return redirect("/login")
        db = get_db()
        u  = db.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
        if not u or not usuario_ativo(u):
            return redirect("/pagamento")
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated

# ── Rotas: páginas ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if not session.get("usuario_id"):
        return redirect("/login")
    db = get_db()
    u  = db.execute("SELECT * FROM usuarios WHERE id=?", (session["usuario_id"],)).fetchone()
    if not u:
        session.clear()
        return redirect("/login")
    if not usuario_ativo(u):
        return redirect("/pagamento")
    return render_template("index.html")

# ── Cadastro ──────────────────────────────────────────────────────────────────
@app.route("/cadastro", methods=["GET","POST"])
def cadastro():
    if request.method == "GET":
        return render_template("cadastro.html")
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha", "")
    nome  = (data.get("nome") or "").strip()

    if not email or not senha:
        return jsonify({"erro": "Email e senha obrigatórios."}), 400
    if len(senha) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres."}), 400
    if "@" not in email:
        return jsonify({"erro": "Email inválido."}), 400

    db = get_db()
    if db.execute("SELECT id FROM usuarios WHERE email=?", (email,)).fetchone():
        return jsonify({"erro": "Email já cadastrado."}), 409

    uid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO usuarios (id,email,senha_hash,nome,status) VALUES (?,?,?,?,?)",
        (uid, email, hash_senha(senha), nome, "pendente")
    )
    db.commit()
    session.permanent = True
    session["usuario_id"] = uid
    session["email"]      = email
    log.info("Novo usuário: %s", email)
    return jsonify({"ok": True, "redirect": "/pagamento"})

# ── Login ─────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "GET":
        return render_template("login_usuario.html")
    ip   = request.remote_addr
    if is_rate_limited(f"login:{ip}", max_calls=10, window=60):
        return jsonify({"erro": "Muitas tentativas."}), 429
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha", "")
    db    = get_db()
    u     = db.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone()
    if not u or not check_senha(senha, u["senha_hash"]):
        time.sleep(0.5)
        return jsonify({"erro": "Email ou senha incorretos."}), 401
    session.permanent     = True
    session["usuario_id"] = u["id"]
    session["email"]      = u["email"]
    if not usuario_ativo(u):
        return jsonify({"ok": True, "redirect": "/pagamento"})
    return jsonify({"ok": True, "redirect": "/"})

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ── Pagamento ─────────────────────────────────────────────────────────────────
@app.route("/pagamento")
@login_required
def pagamento():
    db = get_db()
    u  = db.execute("SELECT * FROM usuarios WHERE id=?", (session["usuario_id"],)).fetchone()
    if u and usuario_ativo(u):
        return redirect("/")
    # Verifica se já tem pagamento aguardando
    pag = db.execute(
        "SELECT * FROM pagamentos WHERE usuario_id=? AND status='aguardando' ORDER BY criado_em DESC LIMIT 1",
        (session["usuario_id"],)
    ).fetchone()
    return render_template("pagamento.html",
        chave_pix=CHAVE_PIX,
        valor=VALOR_PIX,
        email=session.get("email",""),
        tem_pag_pendente=pag is not None
    )

@app.route("/enviar_comprovante", methods=["POST"])
@login_required
def enviar_comprovante():
    uid   = session["usuario_id"]
    arq   = request.files.get("comprovante")
    if not arq:
        return jsonify({"erro": "Comprovante não enviado."}), 400

    mime = arq.content_type or "image/jpeg"
    if mime not in ALLOWED_IMAGE_TYPES:
        return jsonify({"erro": "Arquivo inválido. Use imagem JPEG, PNG ou WebP."}), 400

    # Salva como base64 no banco
    img_b64 = base64.b64encode(arq.read()).decode("utf-8")
    ext     = mime.split("/")[-1]

    db  = get_db()
    pid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO pagamentos (id,usuario_id,comprovante_b64,comprovante_ext,status) VALUES (?,?,?,?,?)",
        (pid, uid, img_b64, ext, "aguardando")
    )
    db.commit()

    # Gera link WhatsApp para notificação
    email = session.get("email","")
    msg   = f"💰 Novo comprovante Kenji IA!\nUsuário: {email}\nAprovar: https://kenji-nexus.onrender.com/admin"
    log.info("Comprovante enviado por %s — pagamento %s", email, pid)

    return jsonify({
        "ok": True,
        "whatsapp_url": f"https://wa.me/{ADMIN_WHATS}?text={requests.utils.quote(msg)}",
        "pagamento_id": pid
    })

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.route("/admin/login", methods=["GET","POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.html")
    data  = request.get_json(silent=True) or {}
    senha = data.get("senha","")
    if hmac.compare_digest(senha, ADMIN_SENHA):
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"erro": "Senha incorreta."}), 401

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html")

@app.route("/admin/pendentes")
@admin_required
def admin_pendentes():
    db   = get_db()
    rows = db.execute("""
        SELECT p.id, p.criado_em, p.comprovante_ext,
               u.email, u.nome, u.id as uid
        FROM pagamentos p
        JOIN usuarios u ON u.id = p.usuario_id
        WHERE p.status = 'aguardando'
        ORDER BY p.criado_em DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/admin/comprovante/<pid>")
@admin_required
def admin_comprovante(pid):
    db  = get_db()
    pag = db.execute("SELECT comprovante_b64, comprovante_ext FROM pagamentos WHERE id=?", (pid,)).fetchone()
    if not pag:
        return jsonify({"erro": "Não encontrado"}), 404
    return jsonify({"img": f"data:image/{pag['comprovante_ext']};base64,{pag['comprovante_b64']}"})

@app.route("/admin/aprovar/<pid>", methods=["POST"])
@admin_required
def admin_aprovar(pid):
    db  = get_db()
    pag = db.execute("SELECT * FROM pagamentos WHERE id=?", (pid,)).fetchone()
    if not pag:
        return jsonify({"erro": "Pagamento não encontrado"}), 404

    agora     = datetime.utcnow()
    acesso_ate = agora + timedelta(days=ACESSO_DIAS)

    db.execute("UPDATE pagamentos SET status='aprovado', aprovado_em=? WHERE id=?",
               (agora.isoformat(), pid))
    db.execute("UPDATE usuarios SET status='ativo', acesso_ate=? WHERE id=?",
               (acesso_ate.isoformat(), pag["usuario_id"]))
    db.commit()
    log.info("Pagamento %s aprovado — acesso até %s", pid, acesso_ate.date())
    return jsonify({"ok": True, "acesso_ate": acesso_ate.strftime("%d/%m/%Y")})

@app.route("/admin/recusar/<pid>", methods=["POST"])
@admin_required
def admin_recusar(pid):
    db = get_db()
    if not db.execute("SELECT id FROM pagamentos WHERE id=?", (pid,)).fetchone():
        return jsonify({"erro": "Não encontrado"}), 404
    db.execute("UPDATE pagamentos SET status='recusado' WHERE id=?", (pid,))
    db.commit()
    log.info("Pagamento %s recusado", pid)
    return jsonify({"ok": True})

@app.route("/admin/usuarios")
@admin_required
def admin_usuarios():
    db   = get_db()
    rows = db.execute(
        "SELECT id,email,nome,status,acesso_ate,criado_em FROM usuarios ORDER BY criado_em DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ── Status do usuário ─────────────────────────────────────────────────────────
@app.route("/meu_status")
@login_required
def meu_status():
    db = get_db()
    u  = db.execute("SELECT * FROM usuarios WHERE id=?", (session["usuario_id"],)).fetchone()
    if not u:
        return jsonify({"erro": "Usuário não encontrado"}), 404
    pag = db.execute(
        "SELECT status,criado_em FROM pagamentos WHERE usuario_id=? ORDER BY criado_em DESC LIMIT 1",
        (u["id"],)
    ).fetchone()
    return jsonify({
        "status":     u["status"],
        "ativo":      usuario_ativo(u),
        "acesso_ate": u["acesso_ate"],
        "pagamento":  dict(pag) if pag else None,
    })

# ── Chat ──────────────────────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
@acesso_required
def chat():
    ip = request.remote_addr
    if is_rate_limited(f"chat:{ip}", max_calls=30, window=60):
        return jsonify({"error": "Limite de requisições atingido."}), 429

    uid = session["usuario_id"]
    ct  = request.content_type or ""

    if "multipart" in ct:
        msg         = (request.form.get("mensagem") or "").strip()
        cid         = (request.form.get("conversa_id") or "").strip()
        imagem_file = request.files.get("imagem")
    else:
        data        = request.get_json(silent=True) or {}
        msg         = (data.get("mensagem") or "").strip()
        cid         = (data.get("conversa_id") or "").strip()
        imagem_file = None

    if not msg and not imagem_file:
        return jsonify({"error": "Mensagem ou imagem obrigatória."}), 400
    if len(msg) > MAX_MSG_LEN:
        return jsonify({"error": "Mensagem muito longa."}), 400
    if not cid:
        return jsonify({"error": "conversa_id ausente."}), 400

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return jsonify({"error": "Serviço indisponível."}), 503

    db = get_db()
    # Verifica se conversa pertence ao usuário
    conv = db.execute("SELECT id FROM conversas WHERE id=? AND usuario_id=?", (cid, uid)).fetchone()
    if not conv:
        return jsonify({"error": "Conversa não encontrada."}), 404

    rows = db.execute(
        "SELECT role,content FROM mensagens WHERE conversa_id=? ORDER BY id DESC LIMIT ?",
        (cid, HISTORY_LIMIT),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    titulo_novo = None
    if len(history) == 0:
        titulo_novo = (msg or "📷 Imagem")[:48] + ("…" if len(msg) > 48 else "")

    if imagem_file:
        mime = imagem_file.content_type or "image/jpeg"
        if mime not in ALLOWED_IMAGE_TYPES:
            return jsonify({"error": "Tipo de imagem não suportado."}), 400
        img_b64      = base64.b64encode(imagem_file.read()).decode("utf-8")
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
            {"type": "text", "text": msg if msg else "Analise esta imagem detalhadamente."},
        ]
    else:
        user_content = msg

    payload = {
        "model":       GROQ_MODEL,
        "messages":    [{"role":"system","content":SYSTEM_PROMPT}, *history, {"role":"user","content":user_content}],
        "max_tokens":  2048,
        "temperature": 0.7,
    }

    try:
        res = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=60,
        )
        res.raise_for_status()
        resposta = res.json()["choices"][0]["message"]["content"]
    except requests.Timeout:
        return jsonify({"error": "Timeout. Tente novamente."}), 504
    except requests.HTTPError:
        return jsonify({"error": "Erro ao comunicar com o modelo."}), 502
    except (KeyError, IndexError, ValueError):
        return jsonify({"error": "Resposta inválida do modelo."}), 502

    msg_salva = msg if msg else "[imagem enviada]"
    db.execute("INSERT INTO mensagens (conversa_id,role,content) VALUES (?,'user',?)", (cid, msg_salva))
    db.execute("INSERT INTO mensagens (conversa_id,role,content) VALUES (?,'assistant',?)", (cid, resposta))
    db.execute("UPDATE conversas SET atualizado_em=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    if titulo_novo:
        db.execute("UPDATE conversas SET titulo=? WHERE id=?", (titulo_novo, cid))
    db.commit()
    return jsonify({"resposta": resposta})

# ── Transcrição áudio ─────────────────────────────────────────────────────────
@app.route("/transcrever", methods=["POST"])
@acesso_required
def transcrever():
    ip = request.remote_addr
    if is_rate_limited(f"audio:{ip}", max_calls=10, window=60):
        return jsonify({"error": "Limite atingido."}), 429
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "Áudio não enviado."}), 400
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return jsonify({"error": "Serviço indisponível."}), 503
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.webm", audio_file.read(), "audio/webm")},
            data={"model": "whisper-large-v3", "language": "pt", "response_format": "json"},
            timeout=30,
        )
        res.raise_for_status()
        texto = res.json().get("text", "").strip()
        if not texto:
            return jsonify({"error": "Não entendi o áudio."}), 422
        return jsonify({"texto": texto})
    except Exception as e:
        log.error("Erro Whisper: %s", e)
        return jsonify({"error": "Erro ao transcrever."}), 502

# ── Conversas ─────────────────────────────────────────────────────────────────
@app.route("/nova_conversa", methods=["POST"])
@acesso_required
def nova():
    uid = session["usuario_id"]
    nid = str(uuid.uuid4())
    db  = get_db()
    db.execute("INSERT INTO conversas (id,usuario_id,titulo) VALUES (?,?,?)", (nid, uid, "Nova missão"))
    db.commit()
    return jsonify({"id": nid}), 201

@app.route("/carregar_conversas")
@acesso_required
def carregar():
    uid  = session["usuario_id"]
    rows = get_db().execute(
        "SELECT id,titulo FROM conversas WHERE usuario_id=? ORDER BY atualizado_em DESC LIMIT 50", (uid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/carregar_historico/<cid>")
@acesso_required
def historico(cid):
    uid = session["usuario_id"]
    db  = get_db()
    if not db.execute("SELECT id FROM conversas WHERE id=? AND usuario_id=?", (cid, uid)).fetchone():
        return jsonify({"error": "Não encontrada"}), 404
    rows = db.execute(
        "SELECT role,content FROM mensagens WHERE conversa_id=? ORDER BY id ASC", (cid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/deletar_conversa/<cid>", methods=["DELETE"])
@acesso_required
def deletar(cid):
    uid = session["usuario_id"]
    db  = get_db()
    if not db.execute("SELECT id FROM conversas WHERE id=? AND usuario_id=?", (cid, uid)).fetchone():
        return jsonify({"error": "Não encontrada"}), 404
    db.execute("DELETE FROM conversas WHERE id=?", (cid,))
    db.commit()
    return jsonify({"status": "ok"})

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": GROQ_MODEL}), 200

@app.errorhandler(404)
def not_found(e): return jsonify({"error": "Não encontrado"}), 404

@app.errorhandler(413)
def too_large(e): return jsonify({"error": "Arquivo muito grande. Máx 10MB."}), 413

@app.errorhandler(500)
def internal(e):
    log.exception("Erro interno: %s", e)
    return jsonify({"error": "Erro interno"}), 500

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)

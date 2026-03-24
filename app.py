import os
import uuid
import logging
import hashlib
import hmac
import time
import base64
from collections import defaultdict
from datetime import timedelta, datetime
from functools import wraps
from contextlib import contextmanager

import requests
import psycopg2
import psycopg2.extras
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
app.config["MAX_CONTENT_LENGTH"]        = 10 * 1024 * 1024

# ── Configurações ─────────────────────────────────────────────────────────────
# CORREÇÃO DA URL: Ajusta prefixos para garantir compatibilidade com o SQLAlchemy/Psycopg2
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
elif DATABASE_URL.startswith("Postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("Postgresql://", "postgresql://", 1)

GROQ_API_URL  = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = os.environ.get("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", 20))
MAX_MSG_LEN   = int(os.environ.get("MAX_MSG_LEN", 4000))
ACESSO_DIAS   = 60
VALOR_PIX     = "7,99"
CHAVE_PIX     = os.environ.get("CHAVE_PIX", "82a91d75-2eba-4fcb-abc8-9be7c27764ac")
ADMIN_WHATS   = os.environ.get("ADMIN_WHATS", "5585893665523")
ADMIN_SENHA   = os.environ.get("ADMIN_SENHA", os.environ.get("SENHA", "admin123"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "Você é a Kenji IA, criada por @cybernmap. "
    "Responda qualquer pergunta de forma direta e completa. "
    "Use markdown quando útil. Se receber uma imagem, analise com detalhes.",
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_buckets: dict = defaultdict(list)

def is_rate_limited(key: str, max_calls: int = 30, window: int = 60) -> bool:
    now = time.time()
    _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < window]
    if len(_rate_buckets[key]) >= max_calls:
        return True
    _rate_buckets[key].append(now)
    return False

# ── Banco de dados (PostgreSQL) ───────────────────────────────────────────────
def get_db():
    if "db" not in g:
        # Usa a DATABASE_URL já corrigida para evitar erros de driver
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        g.db = conn
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        try: db.close()
        except: pass

def db_exec(sql, params=(), fetchone=False, fetchall=False):
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    if fetchone:
        return cur.fetchone()
    if fetchall:
        return cur.fetchall()
    return cur

def db_commit():
    get_db().commit()

def init_db():
    if not DATABASE_URL:
        log.error("DATABASE_URL não configurada!")
        return
    
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    # Cria as tabelas necessárias se o banco estiver vazio
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id            TEXT PRIMARY KEY,
            email         TEXT UNIQUE NOT NULL,
            senha_hash    TEXT NOT NULL,
            nome          TEXT,
            status        TEXT NOT NULL DEFAULT 'pendente',
            acesso_ate    TIMESTAMP,
            criado_em     TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS pagamentos (
            id              TEXT PRIMARY KEY,
            usuario_id      TEXT NOT NULL REFERENCES usuarios(id),
            comprovante_b64 TEXT,
            comprovante_ext TEXT,
            status          TEXT NOT NULL DEFAULT 'aguardando',
            criado_em       TIMESTAMP DEFAULT NOW(),
            aprovado_em     TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversas (
            id            TEXT PRIMARY KEY,
            usuario_id    TEXT NOT NULL REFERENCES usuarios(id),
            titulo        TEXT NOT NULL,
            criado_em     TIMESTAMP DEFAULT NOW(),
            atualizado_em TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS mensagens (
            id          SERIAL PRIMARY KEY,
            conversa_id TEXT NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            criado_em   TIMESTAMP DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_msgs ON mensagens(conversa_id, id);
    """)
    cur.close()
    conn.close()
    log.info("PostgreSQL inicializado e tabelas verificadas.")

# Inicializa o banco no início do app
try:
    init_db()
except Exception as e:
    log.error("Erro ao inicializar DB: %s", e)

# ── Helpers ───────────────────────────────────────────────────────────────────
def hash_senha(senha: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", senha.encode(), app.secret_key.encode(), 100000).hex()

def check_senha(senha: str, stored: str) -> bool:
    return hmac.compare_digest(hash_senha(senha), stored)

def usuario_ativo(u) -> bool:
    if not u or u["status"] != "ativo":
        return False
    if u["acesso_ate"]:
        ate = u["acesso_ate"]
        if isinstance(ate, str):
            ate = datetime.fromisoformat(ate)
        if datetime.utcnow() > ate.replace(tzinfo=None):
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
        u = db_exec("SELECT * FROM usuarios WHERE id=%s", (uid,), fetchone=True)
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
    u = db_exec("SELECT * FROM usuarios WHERE id=%s", (session["usuario_id"],), fetchone=True)
    if not u:
        session.clear()
        return redirect("/login")
    if not usuario_ativo(u):
        return redirect("/pagamento")
    return render_template("index.html")

# ── Cadastro ──────────────────────────────────────────────────────────────────
@app.route("/cadastro", methods=["GET", "POST"])
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

    if db_exec("SELECT id FROM usuarios WHERE email=%s", (email,), fetchone=True):
        return jsonify({"erro": "Email já cadastrado."}), 409

    uid = str(uuid.uuid4())
    db_exec("INSERT INTO usuarios (id,email,senha_hash,nome,status) VALUES (%s,%s,%s,%s,%s)",
            (uid, email, hash_senha(senha), nome, "pendente"))
    db_commit()
    session.permanent     = True
    session["usuario_id"] = uid
    session["email"]      = email
    log.info("Novo usuário: %s", email)
    return jsonify({"ok": True, "redirect": "/pagamento"})

# ── Login ─────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login_usuario.html")
    ip = request.remote_addr
    if is_rate_limited(f"login:{ip}", max_calls=10, window=60):
        return jsonify({"erro": "Muitas tentativas."}), 429
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    senha = data.get("senha", "")
    u     = db_exec("SELECT * FROM usuarios WHERE email=%s", (email,), fetchone=True)
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
    u = db_exec("SELECT * FROM usuarios WHERE id=%s", (session["usuario_id"],), fetchone=True)
    if u and usuario_ativo(u):
        return redirect("/")
    pag = db_exec(
        "SELECT * FROM pagamentos WHERE usuario_id=%s AND status='aguardando' ORDER BY criado_em DESC LIMIT 1",
        (session["usuario_id"],), fetchone=True
    )
    return render_template("pagamento.html",
        chave_pix=CHAVE_PIX,
        valor=VALOR_PIX,
        email=session.get("email", ""),
        tem_pag_pendente=pag is not None
    )

@app.route("/enviar_comprovante", methods=["POST"])
@login_required
def enviar_comprovante():
    uid = session["usuario_id"]
    arq = request.files.get("comprovante")
    if not arq:
        return jsonify({"erro": "Comprovante não enviado."}), 400
    mime = arq.content_type or "image/jpeg"
    if mime not in ALLOWED_IMAGE_TYPES:
        return jsonify({"erro": "Use imagem JPEG, PNG ou WebP."}), 400
    try:
        img_data = arq.read()
        if len(img_data) > 8 * 1024 * 1024:
            return jsonify({"erro": "Imagem muito grande. Use menos de 8MB."}), 400
        img_b64 = base64.b64encode(img_data).decode("utf-8")
    except Exception as e:
        log.error("Erro ao ler comprovante: %s", e)
        return jsonify({"erro": "Erro ao processar imagem."}), 500

    ext = mime.split("/")[-1]
    pid = str(uuid.uuid4())
    db_exec(
        "INSERT INTO pagamentos (id,usuario_id,comprovante_b64,comprovante_ext,status) VALUES (%s,%s,%s,%s,%s)",
        (pid, uid, img_b64, ext, "aguardando")
    )
    db_commit()
    email = session.get("email", "")
    msg   = f"💰 Novo comprovante Kenji IA!\nUsuário: {email}\nAprovar: https://kenji-nexus.onrender.com/admin"
    return jsonify({
        "ok": True,
        "whatsapp_url": f"https://wa.me/{ADMIN_WHATS}?text={requests.utils.quote(msg)}",
        "pagamento_id": pid
    })

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.html")
    data  = request.get_json(silent=True) or {}
    senha = data.get("senha", "")
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
    rows = db_exec("""
        SELECT p.id, p.criado_em, p.comprovante_ext,
               u.email, u.nome, u.id as uid
        FROM pagamentos p
        JOIN usuarios u ON u.id = p.usuario_id
        WHERE p.status = 'aguardando'
        ORDER BY p.criado_em DESC
    """, fetchall=True)
    return jsonify([dict(r) for r in rows])

@app.route("/admin/comprovante/<pid>")
@admin_required
def admin_comprovante(pid):
    pag = db_exec("SELECT comprovante_b64,comprovante_ext FROM pagamentos WHERE id=%s", (pid,), fetchone=True)
    if not pag:
        return jsonify({"erro": "Não encontrado"}), 404
    return jsonify({"img": f"data:image/{pag['comprovante_ext']};base64,{pag['comprovante_b64']}"})

@app.route("/admin/aprovar/<pid>", methods=["POST"])
@admin_required
def admin_aprovar(pid):
    pag = db_exec("SELECT * FROM pagamentos WHERE id=%s", (pid,), fetchone=True)
    if not pag:
        return jsonify({"erro": "Não encontrado"}), 404
    agora      = datetime.utcnow()
    acesso_ate = agora + timedelta(days=ACESSO_DIAS)
    db_exec("UPDATE pagamentos SET status='aprovado', aprovado_em=%s WHERE id=%s", (agora, pid))
    db_exec("UPDATE usuarios SET status='ativo', acesso_ate=%s WHERE id=%s", (acesso_ate, pag["usuario_id"]))
    db_commit()
    log.info("Pagamento %s aprovado", pid)
    return jsonify({"ok": True, "acesso_ate": acesso_ate.strftime("%d/%m/%Y")})

@app.route("/admin/recusar/<pid>", methods=["POST"])
@admin_required
def admin_recusar(pid):
    if not db_exec("SELECT id FROM pagamentos WHERE id=%s", (pid,), fetchone=True):
        return jsonify({"erro": "Não encontrado"}), 404
    db_exec("UPDATE pagamentos SET status='recusado' WHERE id=%s", (pid,))
    db_commit()
    return jsonify({"ok": True})

@app.route("/admin/liberar/<uid>", methods=["POST"])
@admin_required
def admin_liberar(uid):
    if not db_exec("SELECT id FROM usuarios WHERE id=%s", (uid,), fetchone=True):
        return jsonify({"erro": "Usuário não encontrado"}), 404
    agora      = datetime.utcnow()
    acesso_ate = agora + timedelta(days=ACESSO_DIAS)
    db_exec("UPDATE usuarios SET status='ativo', acesso_ate=%s WHERE id=%s", (acesso_ate, uid))
    db_commit()
    log.info("Acesso manual liberado para %s", uid)
    return jsonify({"ok": True, "acesso_ate": acesso_ate.strftime("%d/%m/%Y")})

@app.route("/admin/usuarios")
@admin_required
def admin_usuarios():
    rows = db_exec(
        "SELECT id,email,nome,status,acesso_ate,criado_em FROM usuarios ORDER BY criado_em DESC",
        fetchall=True
    )
    result = []
    for r in rows:
        d = dict(r)
        if d.get("acesso_ate"):
            d["acesso_ate"] = d["acesso_ate"].isoformat()
        if d.get("criado_em"):
            d["criado_em"] = d["criado_em"].isoformat()
        result.append(d)
    return jsonify(result)

# ── Status ────────────────────────────────────────────────────────────────────
@app.route("/meu_status")
@login_required
def meu_status():
    u = db_exec("SELECT * FROM usuarios WHERE id=%s", (session["usuario_id"],), fetchone=True)
    if not u:
        return jsonify({"erro": "Não encontrado"}), 404
    pag = db_exec(
        "SELECT status,criado_em FROM pagamentos WHERE usuario_id=%s ORDER BY criado_em DESC LIMIT 1",
        (u["id"],), fetchone=True
    )
    return jsonify({
        "status":     u["status"],
        "ativo":      usuario_ativo(u),
        "acesso_ate": u["acesso_ate"].isoformat() if u["acesso_ate"] else None,
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

    if not db_exec("SELECT id FROM conversas WHERE id=%s AND usuario_id=%s", (cid, uid), fetchone=True):
        return jsonify({"error": "Conversa não encontrada."}), 404

    rows = db_exec(
        "SELECT role,content FROM mensagens WHERE conversa_id=%s ORDER BY id DESC LIMIT %s",
        (cid, HISTORY_LIMIT), fetchall=True
    )
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
        "messages":    [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_content}],
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
        return jsonify({"error": "Resposta inválida."}), 502

    msg_salva = msg if msg else "[imagem enviada]"
    db_exec("INSERT INTO mensagens (conversa_id,role,content) VALUES (%s,'user',%s)", (cid, msg_salva))
    db_exec("INSERT INTO mensagens (conversa_id,role,content) VALUES (%s,'assistant',%s)", (cid, resposta))
    db_exec("UPDATE conversas SET atualizado_em=NOW() WHERE id=%s", (cid,))
    if titulo_novo:
        db_exec("UPDATE conversas SET titulo=%s WHERE id=%s", (titulo_novo, cid))
    db_commit()
    return jsonify({"resposta": resposta})

# ── Transcrição ───────────────────────────────────────────────────────────────
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
    db_exec("INSERT INTO conversas (id,usuario_id,titulo) VALUES (%s,%s,%s)", (nid, uid, "Nova missão"))
    db_commit()
    return jsonify({"id": nid}), 201

@app.route("/carregar_conversas")
@acesso_required
def carregar():
    uid  = session["usuario_id"]
    rows = db_exec(
        "SELECT id,titulo FROM conversas WHERE usuario_id=%s ORDER BY atualizado_em DESC LIMIT 50",
        (uid,), fetchall=True
    )
    return jsonify([dict(r) for r in rows])

@app.route("/carregar_historico/<cid>")
@acesso_required
def historico(cid):
    uid = session["usuario_id"]
    if not db_exec("SELECT id FROM conversas WHERE id=%s AND usuario_id=%s", (cid, uid), fetchone=True):
        return jsonify({"error": "Não encontrada"}), 404
    rows = db_exec(
        "SELECT role,content FROM mensagens WHERE conversa_id=%s ORDER BY id ASC", (cid,), fetchall=True
    )
    return jsonify([dict(r) for r in rows])

@app.route("/deletar_conversa/<cid>", methods=["DELETE"])
@acesso_required
def deletar(cid):
    uid = session["usuario_id"]
    if not db_exec("SELECT id FROM conversas WHERE id=%s AND usuario_id=%s", (cid, uid), fetchone=True):
        return jsonify({"error": "Não encontrada"}), 404
    db_exec("DELETE FROM conversas WHERE id=%s", (cid,))
    db_commit()
    return jsonify({"status": "ok"})

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": GROQ_MODEL}), 200

@app.errorhandler(404)
def not_found(e): return jsonify({"error": "Não encontrado"}), 404

@app.errorhandler(413)
def too_large(e): return jsonify({"error": "Arquivo muito grande."}), 413

@app.errorhandler(500)
def internal(e):
    log.exception("Erro interno: %s", e)
    return jsonify({"error": "Erro interno"}), 500

# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)

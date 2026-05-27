"""
PROSPECTOR IA — Backend FastAPI
"""
import os, json, csv, io, bcrypt
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
import jwt
import httpx

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "troca-isso-em-producao")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")           # sua chave padrão
BAILEYS_URL  = os.getenv("BAILEYS_URL", "http://baileys:3000")
ALGORITHM    = "HS256"
TOKEN_EXP    = 24  # horas

DB_URL = os.getenv("DATABASE_URL",
    "postgresql://leanttro:Fin@2021@wpp-wpp-a3wnej:5432/postgres")

app = FastAPI(title="Prospector IA", version="1.0.0")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

security = HTTPBearer()

# ─────────────────────────────────────────
#  BANCO
# ─────────────────────────────────────────
def get_db():
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

def db_one(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchone()

def db_all(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()

def db_exec(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    try:    return cur.fetchone()
    except: return None

# ─────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────
def criar_token(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_EXP)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(security),
    conn=Depends(get_db)
):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")
    user = db_one(conn, "SELECT * FROM usuarios WHERE id = %s AND ativo = TRUE", (user_id,))
    if not user:
        raise HTTPException(status_code=401, detail="Usuário não encontrado")
    return dict(user)

# ─────────────────────────────────────────
#  SCHEMAS
# ─────────────────────────────────────────
class LoginBody(BaseModel):
    email: str
    senha: str

class RegisterBody(BaseModel):
    nome: str
    email: str
    senha: str

class AIConfigBody(BaseModel):
    persona_nome: Optional[str] = "Assistente"
    prompt_sistema: Optional[str] = None
    temperatura: Optional[float] = 0.7
    modelo: Optional[str] = "llama3-8b-8192"
    produto_nome: Optional[str] = None
    produto_descricao: Optional[str] = None
    produto_preco: Optional[str] = None
    midia_abertura_url: Optional[str] = None
    midia_abertura_tipo: Optional[str] = None
    midia_abertura_caption: Optional[str] = None
    midia_fechamento_url: Optional[str] = None
    midia_fechamento_tipo: Optional[str] = None
    midia_fechamento_caption: Optional[str] = None
    gatilhos_parada: Optional[str] = None
    horario_inicio: Optional[str] = "08:00"
    horario_fim: Optional[str] = "18:00"
    delay_mensagens: Optional[int] = 3
    max_followups: Optional[int] = 2
    intervalo_followup: Optional[int] = 24

class ContactBody(BaseModel):
    nome: str
    telefone: str
    empresa: Optional[str] = None
    cargo: Optional[str] = None
    notas: Optional[str] = None

class ContactStatusBody(BaseModel):
    status: str

class CampaignBody(BaseModel):
    nome: str
    velocidade: Optional[int] = 60
    contact_ids: Optional[list[int]] = []

class GroqKeyBody(BaseModel):
    groq_key: Optional[str] = None
    usar_ia_propria: bool = False

class ConversationModoBody(BaseModel):
    modo: str  # 'ia' ou 'manual'

class SendMessageBody(BaseModel):
    conversation_id: int
    content: str
    midia_url: Optional[str] = None
    midia_tipo: Optional[str] = None

# ─────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────
@app.post("/auth/register")
def register(body: RegisterBody, conn=Depends(get_db)):
    existe = db_one(conn, "SELECT id FROM usuarios WHERE email = %s", (body.email,))
    if existe:
        raise HTTPException(400, "Email já cadastrado")
    plano = db_one(conn, "SELECT id FROM planos WHERE nome = 'Free' LIMIT 1")
    senha_hash = bcrypt.hashpw(body.senha.encode(), bcrypt.gensalt()).decode()
    user = db_exec(conn,
        "INSERT INTO usuarios (nome, email, senha_hash, plano_id) VALUES (%s,%s,%s,%s) RETURNING id, nome, email",
        (body.nome, body.email, senha_hash, plano["id"] if plano else None))
    token = criar_token(user["id"], user["email"])
    return {"token": token, "user": dict(user)}

@app.post("/auth/login")
def login(body: LoginBody, conn=Depends(get_db)):
    user = db_one(conn, "SELECT * FROM usuarios WHERE email = %s AND ativo = TRUE", (body.email,))
    if not user:
        raise HTTPException(401, "Credenciais inválidas")
    if not bcrypt.checkpw(body.senha.encode(), user["senha_hash"].encode()):
        raise HTTPException(401, "Credenciais inválidas")
    token = criar_token(user["id"], user["email"])
    safe = {k: v for k, v in dict(user).items() if k not in ("senha_hash",)}
    return {"token": token, "user": safe}

@app.get("/auth/me")
def me(user=Depends(get_current_user)):
    return {k: v for k, v in user.items() if k != "senha_hash"}

# ─────────────────────────────────────────
#  GROQ KEY
# ─────────────────────────────────────────
@app.put("/config/groq")
def salvar_groq_key(body: GroqKeyBody, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn,
        "UPDATE usuarios SET groq_key = %s, usar_ia_propria = %s WHERE id = %s",
        (body.groq_key, body.usar_ia_propria, user["id"]))
    return {"ok": True}

# ─────────────────────────────────────────
#  AI CONFIG
# ─────────────────────────────────────────
@app.get("/ai-config")
def get_ai_config(user=Depends(get_current_user), conn=Depends(get_db)):
    cfg = db_one(conn, "SELECT * FROM ai_config WHERE usuario_id = %s", (user["id"],))
    return dict(cfg) if cfg else {}

@app.post("/ai-config")
def salvar_ai_config(body: AIConfigBody, user=Depends(get_current_user), conn=Depends(get_db)):
    existe = db_one(conn, "SELECT id FROM ai_config WHERE usuario_id = %s", (user["id"],))
    fields = body.dict()
    if existe:
        sets = ", ".join(f"{k} = %s" for k in fields)
        vals = list(fields.values()) + [user["id"]]
        db_exec(conn, f"UPDATE ai_config SET {sets}, atualizado_em = NOW() WHERE usuario_id = %s", vals)
    else:
        cols = "usuario_id, " + ", ".join(fields.keys())
        phs  = "%s, " + ", ".join(["%s"] * len(fields))
        vals = [user["id"]] + list(fields.values())
        db_exec(conn, f"INSERT INTO ai_config ({cols}) VALUES ({phs})", vals)
    return {"ok": True}

# ─────────────────────────────────────────
#  CONTACTS
# ─────────────────────────────────────────
@app.get("/contacts")
def listar_contacts(
    status: Optional[str] = None,
    user=Depends(get_current_user),
    conn=Depends(get_db)
):
    if status:
        rows = db_all(conn,
            "SELECT * FROM contacts WHERE usuario_id = %s AND status = %s ORDER BY criado_em DESC",
            (user["id"], status))
    else:
        rows = db_all(conn,
            "SELECT * FROM contacts WHERE usuario_id = %s ORDER BY criado_em DESC",
            (user["id"],))
    return [dict(r) for r in rows]

@app.post("/contacts")
def criar_contact(body: ContactBody, user=Depends(get_current_user), conn=Depends(get_db)):
    row = db_exec(conn,
        "INSERT INTO contacts (usuario_id, nome, telefone, empresa, cargo, notas) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
        (user["id"], body.nome, body.telefone, body.empresa, body.cargo, body.notas))
    return dict(row)

@app.put("/contacts/{contact_id}")
def atualizar_contact(contact_id: int, body: ContactBody, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn,
        "UPDATE contacts SET nome=%s, telefone=%s, empresa=%s, cargo=%s, notas=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
        (body.nome, body.telefone, body.empresa, body.cargo, body.notas, contact_id, user["id"]))
    return {"ok": True}

@app.patch("/contacts/{contact_id}/status")
def atualizar_status_contact(contact_id: int, body: ContactStatusBody, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn,
        "UPDATE contacts SET status=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
        (body.status, contact_id, user["id"]))
    return {"ok": True}

@app.delete("/contacts/{contact_id}")
def deletar_contact(contact_id: int, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn, "DELETE FROM contacts WHERE id=%s AND usuario_id=%s", (contact_id, user["id"]))
    return {"ok": True}

@app.post("/contacts/import-csv")
async def importar_csv(file: UploadFile = File(...), user=Depends(get_current_user), conn=Depends(get_db)):
    content = await file.read()
    reader  = csv.DictReader(io.StringIO(content.decode("utf-8")))
    inseridos = 0
    erros = []
    for i, row in enumerate(reader):
        try:
            telefone = row.get("telefone") or row.get("phone") or row.get("numero")
            nome     = row.get("nome")     or row.get("name")  or "Sem nome"
            if not telefone:
                erros.append(f"Linha {i+2}: telefone ausente")
                continue
            db_exec(conn,
                "INSERT INTO contacts (usuario_id, nome, telefone, empresa, cargo) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user["id"], nome, telefone,
                 row.get("empresa") or row.get("company"),
                 row.get("cargo")   or row.get("role")))
            inseridos += 1
        except Exception as e:
            erros.append(f"Linha {i+2}: {str(e)}")
    return {"inseridos": inseridos, "erros": erros}

# ─────────────────────────────────────────
#  CAMPAIGNS
# ─────────────────────────────────────────
@app.get("/campaigns")
def listar_campaigns(user=Depends(get_current_user), conn=Depends(get_db)):
    rows = db_all(conn, "SELECT * FROM campaigns WHERE usuario_id=%s ORDER BY criado_em DESC", (user["id"],))
    return [dict(r) for r in rows]

@app.post("/campaigns")
def criar_campaign(body: CampaignBody, user=Depends(get_current_user), conn=Depends(get_db)):
    camp = db_exec(conn,
        "INSERT INTO campaigns (usuario_id, nome, velocidade, total_contatos) VALUES (%s,%s,%s,%s) RETURNING *",
        (user["id"], body.nome, body.velocidade, len(body.contact_ids)))
    for cid in body.contact_ids:
        try:
            db_exec(conn,
                "INSERT INTO campaign_contacts (campaign_id, contact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (camp["id"], cid))
        except: pass
    return dict(camp)

@app.patch("/campaigns/{campaign_id}/status")
async def atualizar_status_campaign(campaign_id: int, body: ContactStatusBody, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn,
        "UPDATE campaigns SET status=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
        (body.status, campaign_id, user["id"]))
    if body.status == 'ativa':
        from fila import enfileirar_campanha
        camp = db_one(conn, "SELECT * FROM campaigns WHERE id=%s AND usuario_id=%s", (campaign_id, user["id"]))
        if camp:
            await enfileirar_campanha(campaign_id, user["id"], camp["velocidade"], conn)
    return {"ok": True}

@app.delete("/campaigns/{campaign_id}")
def deletar_campaign(campaign_id: int, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn, "DELETE FROM campaigns WHERE id=%s AND usuario_id=%s", (campaign_id, user["id"]))
    return {"ok": True}

# ─────────────────────────────────────────
#  CONVERSATIONS
# ─────────────────────────────────────────
@app.get("/conversations")
def listar_conversations(user=Depends(get_current_user), conn=Depends(get_db)):
    rows = db_all(conn, """
        SELECT c.*, ct.nome as contact_nome, ct.telefone as contact_telefone
        FROM conversations c
        LEFT JOIN contacts ct ON ct.id = c.contact_id
        WHERE c.usuario_id = %s
        ORDER BY c.atualizado_em DESC
    """, (user["id"],))
    return [dict(r) for r in rows]

@app.get("/conversations/{conv_id}/messages")
def messages_da_conversa(conv_id: int, user=Depends(get_current_user), conn=Depends(get_db)):
    conv = db_one(conn, "SELECT id FROM conversations WHERE id=%s AND usuario_id=%s", (conv_id, user["id"]))
    if not conv:
        raise HTTPException(404, "Conversa não encontrada")
    rows = db_all(conn, "SELECT * FROM messages WHERE conversation_id=%s ORDER BY timestamp ASC", (conv_id,))
    return [dict(r) for r in rows]

@app.patch("/conversations/{conv_id}/modo")
def alterar_modo_conversa(conv_id: int, body: ConversationModoBody, user=Depends(get_current_user), conn=Depends(get_db)):
    db_exec(conn,
        "UPDATE conversations SET modo=%s, atualizado_em=NOW() WHERE id=%s AND usuario_id=%s",
        (body.modo, conv_id, user["id"]))
    return {"ok": True}

@app.post("/conversations/send")
async def enviar_mensagem_manual(body: SendMessageBody, user=Depends(get_current_user), conn=Depends(get_db)):
    conv = db_one(conn, "SELECT * FROM conversations WHERE id=%s AND usuario_id=%s", (body.conversation_id, user["id"]))
    if not conv:
        raise HTTPException(404, "Conversa não encontrada")
    db_exec(conn,
        "INSERT INTO messages (conversation_id, role, content, midia_url, midia_tipo) VALUES (%s,'assistant',%s,%s,%s)",
        (body.conversation_id, body.content, body.midia_url, body.midia_tipo))
    async with httpx.AsyncClient() as client:
        await client.post(f"{BAILEYS_URL}/ia-responder", json={
            "number": conv["jid"].replace("@s.whatsapp.net", ""),
            "message": body.content
        })
    return {"ok": True}

# ─────────────────────────────────────────
#  WHATSAPP STATUS
# ─────────────────────────────────────────
@app.get("/whatsapp/status")
def wpp_status(user=Depends(get_current_user)):
    try:
        import httpx
        r = httpx.get(f"{BAILEYS_URL}/status", timeout=5)
        return r.json()
    except:
        return {"connected": False, "number": ""}

@app.get("/whatsapp/qrcode")
def wpp_qrcode(user=Depends(get_current_user)):
    return {"url": f"{BAILEYS_URL}/qrcode"}

# ─────────────────────────────────────────
#  ANALYTICS
# ─────────────────────────────────────────
@app.get("/analytics")
def analytics(user=Depends(get_current_user), conn=Depends(get_db)):
    totais = db_one(conn, """
        SELECT
            COUNT(DISTINCT c.id)                                        AS total_contatos,
            COUNT(DISTINCT CASE WHEN c.status != 'pendente' THEN c.id END) AS abordados,
            COUNT(DISTINCT CASE WHEN c.status = 'qualificado' THEN c.id END) AS qualificados,
            COUNT(DISTINCT CASE WHEN c.status = 'convertido'  THEN c.id END) AS convertidos
        FROM contacts c WHERE c.usuario_id = %s
    """, (user["id"],))
    historico = db_all(conn, """
        SELECT data, abordados, responderam, qualificados, convertidos, msgs_enviadas, msgs_recebidas
        FROM analytics_daily WHERE usuario_id = %s ORDER BY data DESC LIMIT 30
    """, (user["id"],))
    return {
        "totais": dict(totais),
        "historico": [dict(r) for r in historico]
    }

# ─────────────────────────────────────────
#  WEBHOOK — recebe eventos do Baileys
# ─────────────────────────────────────────
@app.post("/webhook/mensagem")
async def webhook_mensagem(payload: dict, conn=Depends(get_db)):
    """
    Baileys chama esse endpoint quando chega mensagem nova.
    Aqui a IA decide o que responder.
    """
    # Importado aqui pra não circular
    from ia import processar_mensagem
    await processar_mensagem(payload, conn)
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "Prospector IA rodando"}

"""
PROSPECTOR IA — Backend unificado (app.py + ia.py + fila.py)
"""
import os, json, csv, io, bcrypt, re, asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date, time
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import jwt
import httpx
import redis.asyncio as aioredis

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
SECRET_KEY      = os.getenv("SECRET_KEY", "troca-isso-em-producao")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
BAILEYS_URL     = os.getenv("BAILEYS_URL", "http://baileys:3000")
ALGORITHM       = "HS256"
TOKEN_EXP       = 24  # horas
DB_URL          = os.getenv("DATABASE_URL",
    "postgresql://leanttro:Fin@2021@wpp-wpp-a3wnej:5432/postgres")
REDIS_URL       = os.getenv("REDIS_URL", "redis://redis:6379")
GROQ_API_URL    = "https://api.groq.com/openai/v1/chat/completions"
GATILHOS_PARADA_PADRAO = ["não quero", "nao quero", "para", "chega", "sai", "remove", "cancelar"]

# Modelos Groq em ordem de preferência (fallback automático)
GROQ_MODELOS_FALLBACK = [
    "llama-3.1-8b-instant",       # rápido, estável
    "llama-3.3-70b-versatile",    # mais capaz
    "gemma2-9b-it",               # fallback adicional
    # modelos abaixo removidos (decommissioned pela Groq):
    # "llama3-8b-8192"
    # "mixtral-8x7b-32768"
]

# ─────────────────────────────────────────
#  BANCO
# ─────────────────────────────────────────
def get_conn_raw():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def get_db():
    conn = get_conn_raw()
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
security = HTTPBearer()

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
    modelo: Optional[str] = "llama-3.1-8b-instant"
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
#  IA — helpers
# ─────────────────────────────────────────
def get_groq_key(usuario: dict) -> str:
    if usuario.get("usar_ia_propria") and usuario.get("groq_key"):
        return usuario["groq_key"]
    return GROQ_API_KEY

def checar_gatilho_parada(texto: str, gatilhos_str: str) -> bool:
    texto_lower = texto.lower()
    gatilhos = GATILHOS_PARADA_PADRAO[:]
    if gatilhos_str:
        gatilhos += [g.strip().lower() for g in gatilhos_str.split(",")]
    return any(g in texto_lower for g in gatilhos)

def checar_pedido_responsavel(texto: str) -> bool:
    sinais = ["responsável", "responsavel", "decisor", "quem decide", "falar com"]
    return any(s in texto.lower() for s in sinais)

def checar_telefone_na_resposta(texto: str):
    match = re.search(r"(\+?\d[\d\s\-\(\)]{8,15}\d)", texto)
    return match.group(1).strip() if match else None

def montar_system_prompt(cfg: dict, usuario: dict) -> str:
    persona   = cfg.get("persona_nome") or "Assistente"
    produto   = cfg.get("produto_nome") or "nosso serviço"
    descricao = cfg.get("produto_descricao") or ""
    preco     = cfg.get("produto_preco") or ""
    prompt    = cfg.get("prompt_sistema") or ""

    base = f"""Você é {persona}, assistente de vendas especialista.
Seu objetivo é prospectar clientes e vender: {produto}.
{f'Descrição: {descricao}' if descricao else ''}
{f'Preço: {preco}' if preco else ''}

Instruções importantes:
- Seja natural, humano e objetivo
- Se a pessoa não for o decisor/responsável, pergunte educadamente pelo número ou nome do responsável
- Se identificar interesse real, aprofunde a conversa e tente fechar
- Seja breve nas mensagens (máximo 3 parágrafos)
- Nunca mande listas longas ou textos enormes
- Se a pessoa pedir para parar, agradeça e encerre
- Use APENAS as informações que foram fornecidas acima. Se algum dado não estiver disponível, simplesmente não o mencione. NUNCA use colchetes, placeholders ou termos como [nome da empresa], [produto], [valor] — se não tiver a informação, ignore esse ponto na conversa.

{prompt}"""
    return base.strip()

async def chamar_groq(system_prompt: str, historico: list, groq_key: str, temperatura: float, modelo: str) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    for msg in historico[-10:]:
        role = "assistant" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "content": msg["content"] or ""})

    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }

    # Monta lista de modelos: tenta o configurado primeiro, depois os fallbacks
    modelos_para_tentar = []
    if modelo and modelo not in GROQ_MODELOS_FALLBACK:
        modelos_para_tentar.append(modelo)
    elif modelo:
        modelos_para_tentar.append(modelo)
    for m in GROQ_MODELOS_FALLBACK:
        if m not in modelos_para_tentar:
            modelos_para_tentar.append(m)

    ultimo_erro = None
    async with httpx.AsyncClient(timeout=30) as client:
        for m in modelos_para_tentar:
            payload = {
                "model": m,
                "temperature": float(temperatura or 0.7),
                "max_tokens": 500,
                "messages": messages
            }
            try:
                resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
                if resp.status_code == 400:
                    erro_body = resp.json()
                    print(f"⚠️ Modelo {m} retornou 400: {erro_body.get('error', {}).get('message', '')} — tentando próximo...")
                    ultimo_erro = erro_body
                    continue
                resp.raise_for_status()
                data = resp.json()
                if m != modelo:
                    print(f"✅ Usando modelo fallback: {m}")
                return data["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as e:
                print(f"⚠️ Erro HTTP com modelo {m}: {e} — tentando próximo...")
                ultimo_erro = str(e)
                continue
            except Exception as e:
                print(f"⚠️ Erro inesperado com modelo {m}: {e} — tentando próximo...")
                ultimo_erro = str(e)
                continue

    raise Exception(f"Todos os modelos Groq falharam. Último erro: {ultimo_erro}")

async def enviar_whatsapp(numero: str, texto: str = None, midia_url: str = None, midia_tipo: str = None, caption: str = None):
    payload = {"number": numero, "message": texto or caption or "", "useFullJid": True}
    if midia_url:
        if midia_tipo == "video":
            payload["videoUrl"] = midia_url
        else:
            payload["imageUrl"] = midia_url
    async with httpx.AsyncClient(timeout=45) as client:
        await client.post(f"{BAILEYS_URL}/disparar", json=payload)

# ─────────────────────────────────────────
#  IA — processador de mensagens (ex ia.py)
# ─────────────────────────────────────────
async def processar_mensagem(payload: dict, conn):
    print(f"📨 Webhook recebido: {json.dumps({k: v for k, v in payload.items() if k != 'image'})}")

    usuario_id = payload.get("usuario_id")
    jid        = payload.get("remoteJid", "")
    texto      = payload.get("text", "") or ""
    from_me    = payload.get("fromMe", False)

    if from_me:
        print("⏭️  Ignorando mensagem própria (fromMe=True)")
        return
    if not texto:
        print("⏭️  Ignorando mensagem sem texto")
        return
    if not usuario_id:
        print(f"🔴 ERRO CRÍTICO: usuario_id ausente no payload! Chaves recebidas: {list(payload.keys())}")
        return

    # Mantém o JID completo para envio correto (@lid ou @s.whatsapp.net)
    # O número puro é usado para buscas no banco (ignora sufixo @lid/@s.whatsapp.net)
    numero = jid  # envia o JID completo ao Baileys
    numero_puro = re.sub(r"@.*", "", jid)

    # Normaliza: remove prefixo "1" de números BR com 13 dígitos (ex: 5511... vs 55011...)
    # O WhatsApp às vezes envia "1" extra antes do DDD para números BR
    numero_puro_normalizado = numero_puro
    if len(numero_puro) == 13 and numero_puro.startswith("1"):
        numero_puro_normalizado = numero_puro[1:]  # 119516088529026 → 19516088529026

    usuario = db_one(conn, "SELECT * FROM usuarios WHERE id = %s", (usuario_id,))
    if not usuario:
        print(f"🔴 usuario_id={usuario_id} não encontrado no banco")
        return
    usuario = dict(usuario)

    cfg = db_one(conn, "SELECT * FROM ai_config WHERE usuario_id = %s", (usuario_id,))
    if not cfg:
        print(f"🔴 ai_config não encontrada para usuario_id={usuario_id}")
        return
    cfg = dict(cfg)

    # Verifica horário de funcionamento (usa TZ do sistema; configure TZ=America/Sao_Paulo no container)
    agora = datetime.now().time()
    try:
        h_ini_raw = cfg.get("horario_inicio")
        h_fim_raw = cfg.get("horario_fim")
        h_ini = h_ini_raw if isinstance(h_ini_raw, time) else time.fromisoformat(str(h_ini_raw or "08:00"))
        h_fim = h_fim_raw if isinstance(h_fim_raw, time) else time.fromisoformat(str(h_fim_raw or "18:00"))
        if not (h_ini <= agora <= h_fim):
            print(f"⏰ Fora do horário ({h_ini}–{h_fim}), agora={agora} — ignorando")
            return
    except Exception as e:
        print(f"⚠️  Erro ao verificar horário: {e} — continuando sem restrição")

    # Busca conversa existente pelo número puro (últimos 9 dígitos),
    # ignorando variações de sufixo (@lid vs @s.whatsapp.net) e prefixo "1" extra do WhatsApp.
    # Ex: "119516088529026@lid" e "5511951608852@s.whatsapp.net" têm os mesmos 9 dígitos finais.
    sufixo_busca = numero_puro_normalizado[-9:]
    conv = db_one(conn,
        "SELECT * FROM conversations WHERE usuario_id=%s AND regexp_replace(jid, '@.*', '') LIKE %s ORDER BY criado_em DESC LIMIT 1",
        (usuario_id, f"%{sufixo_busca}"))

    if not conv:
        print(f"🆕 Nova conversa para {numero_puro} (normalizado: {numero_puro_normalizado})")
        contact = db_one(conn, "SELECT id FROM contacts WHERE usuario_id=%s AND telefone LIKE %s", (usuario_id, f"%{sufixo_busca}%"))
        contact_id = contact["id"] if contact else None
        # Se o contato não existe na base, ignora completamente — IA não responde desconhecidos
        if not contact_id:
            print(f"🚫 Contato {numero_puro} não está na base do sistema — ignorando mensagem")
            return
        conv = db_exec(conn,
            "INSERT INTO conversations (usuario_id, contact_id, jid, status, modo) VALUES (%s,%s,%s,'ativa','ia') RETURNING *",
            (usuario_id, contact_id, jid))
    else:
        print(f"♻️  Conversa existente id={conv['id']} para {numero_puro}")
        # Atualiza o JID caso tenha mudado de @lid para @s.whatsapp.net ou vice-versa
        if conv["jid"] != jid:
            print(f"🔄 JID atualizado: {conv['jid']} → {jid}")
            db_exec(conn, "UPDATE conversations SET jid=%s WHERE id=%s", (jid, conv["id"]))
    conv = dict(conv)

    if conv.get("modo") == "manual":
        print(f"👤 Conversa {conv['id']} em modo manual — IA pausada")
        return

    db_exec(conn,
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'user',%s)",
        (conv["id"], texto))
    print(f"💬 Mensagem salva. conv={conv['id']} texto='{texto[:60]}'")

    if checar_gatilho_parada(texto, cfg.get("gatilhos_parada", "")):
        print(f"🛑 Gatilho de parada detectado para {numero_puro}")
        db_exec(conn,
            "UPDATE conversations SET status='encerrada', atualizado_em=NOW() WHERE id=%s",
            (conv["id"],))
        db_exec(conn,
            "UPDATE contacts SET status='perdido', atualizado_em=NOW() WHERE usuario_id=%s AND telefone LIKE %s",
            (usuario_id, f"%{numero_puro[-9:]}%"))
        return

    ultima_nossa = db_one(conn,
        "SELECT content FROM messages WHERE conversation_id=%s AND role='assistant' ORDER BY timestamp DESC LIMIT 1",
        (conv["id"],))
    if ultima_nossa and checar_pedido_responsavel(ultima_nossa["content"] or ""):
        tel_resp = checar_telefone_na_resposta(texto)
        if tel_resp:
            db_exec(conn,
                "UPDATE contacts SET responsavel_telefone=%s, atualizado_em=NOW() WHERE usuario_id=%s AND telefone LIKE %s",
                (tel_resp, usuario_id, f"%{numero_puro[-9:]}%"))
            db_exec(conn,
                "INSERT INTO contacts (usuario_id, nome, telefone, notas, status) VALUES (%s,%s,%s,%s,'pendente') ON CONFLICT DO NOTHING",
                (usuario_id, "Responsável via " + numero_puro, tel_resp, f"Indicado por {numero_puro}"))

    historico = db_all(conn,
        "SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY timestamp ASC",
        (conv["id"],))
    historico = [dict(h) for h in historico]

    groq_key      = get_groq_key(usuario)
    system_prompt = montar_system_prompt(cfg, usuario)
    temperatura   = float(cfg.get("temperatura") or 0.7)
    modelo        = cfg.get("modelo") or "llama-3.1-8b-instant"

    print(f"🤖 Chamando Groq modelo={modelo} conv={conv['id']}")
    try:
        resposta = await chamar_groq(system_prompt, historico, groq_key, temperatura, modelo)
        print(f"✅ Groq respondeu: '{resposta[:60]}'")
    except Exception as e:
        print(f"🔴 Erro Groq conv={conv['id']}: {e}")
        return

    delay = int(cfg.get("delay_mensagens") or 3)
    await asyncio.sleep(min(delay, 10))

    try:
        await enviar_whatsapp(numero, texto=resposta)
    except Exception as e:
        print(f"⚠️ Timeout/erro ao enviar resposta IA para {numero}: {e}")
        return

    db_exec(conn,
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
        (conv["id"], resposta))
    db_exec(conn,
        "UPDATE conversations SET atualizado_em=NOW() WHERE id=%s",
        (conv["id"],))

    sinais_interesse = ["quero", "interesse", "como funciona", "valor", "preço", "quanto custa", "me conta mais"]
    if any(s in texto.lower() for s in sinais_interesse):
        midia_url  = cfg.get("midia_fechamento_url")
        midia_tipo = cfg.get("midia_fechamento_tipo")
        caption    = cfg.get("midia_fechamento_caption")
        if midia_url:
            await asyncio.sleep(2)
            await enviar_whatsapp(numero, midia_url=midia_url, midia_tipo=midia_tipo, caption=caption)

    hoje = date.today()
    db_exec(conn, """
        INSERT INTO analytics_daily (usuario_id, data, msgs_enviadas, msgs_recebidas)
        VALUES (%s, %s, 1, 1)
        ON CONFLICT (usuario_id, data) DO UPDATE SET
            msgs_enviadas  = analytics_daily.msgs_enviadas  + 1,
            msgs_recebidas = analytics_daily.msgs_recebidas + 1
    """, (usuario_id, hoje))

# ─────────────────────────────────────────
#  FILA — workers (ex fila.py)
# ─────────────────────────────────────────
async def gerar_mensagem_abertura(cfg: dict, contato: dict, groq_key: str) -> str:
    nome    = contato.get("nome") or "prezado"
    empresa = contato.get("empresa") or ""
    produto = cfg.get("produto_nome") or "nosso serviço"
    persona = cfg.get("persona_nome") or "Assistente"
    prompt  = cfg.get("prompt_sistema") or ""

    system = f"""Você é {persona}, especialista em vendas.
{prompt}
Crie uma PRIMEIRA mensagem de prospecção no WhatsApp para {nome}{f' da empresa {empresa}' if empresa else ''}.
Ofereça: {produto}
Regras: seja natural, curto (máx 3 linhas), não pareça spam, personalize pelo nome/empresa."""

    modelo_cfg = cfg.get("modelo") or "llama-3.1-8b-instant"
    modelos_para_tentar = [modelo_cfg]
    for m in GROQ_MODELOS_FALLBACK:
        if m not in modelos_para_tentar:
            modelos_para_tentar.append(m)

    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    ultimo_erro = None

    async with httpx.AsyncClient(timeout=30) as client:
        for m in modelos_para_tentar:
            payload = {
                "model": m,
                "temperature": float(cfg.get("temperatura") or 0.7),
                "max_tokens": 200,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": "Gere a mensagem de abertura agora."}
                ]
            }
            try:
                resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
                if resp.status_code == 400:
                    erro_body = resp.json()
                    print(f"⚠️ [abertura] Modelo {m} retornou 400: {erro_body.get('error', {}).get('message', '')} — tentando próximo...")
                    ultimo_erro = erro_body
                    continue
                resp.raise_for_status()
                if m != modelo_cfg:
                    print(f"✅ [abertura] Usando modelo fallback: {m}")
                return resp.json()["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as e:
                print(f"⚠️ [abertura] Erro HTTP com modelo {m}: {e} — tentando próximo...")
                ultimo_erro = str(e)
                continue
            except Exception as e:
                print(f"⚠️ [abertura] Erro inesperado com modelo {m}: {e} — tentando próximo...")
                ultimo_erro = str(e)
                continue

    raise Exception(f"[abertura] Todos os modelos Groq falharam. Último erro: {ultimo_erro}")


async def processar_contato_campanha(campaign_id: int, contact_id: int, usuario_id: int):
    print(f"🚀 Processando campaign={campaign_id} contact={contact_id} usuario={usuario_id}")
    conn = get_conn_raw()
    try:
        usuario = dict(db_one(conn, "SELECT * FROM usuarios WHERE id=%s", (usuario_id,)))
        cfg     = dict(db_one(conn, "SELECT * FROM ai_config WHERE usuario_id=%s", (usuario_id,)) or {})
        contato = dict(db_one(conn, "SELECT * FROM contacts WHERE id=%s", (contact_id,)) or {})

        print(f"📋 contato={contato.get('nome')} cfg={bool(cfg)}")

        if not contato or not cfg:
            print("⚠️ Contato ou config vazio — abortando")
            return

        agora = datetime.now().time()
        try:
            h_ini = time.fromisoformat(str(cfg.get("horario_inicio", "08:00")))
            h_fim = time.fromisoformat(str(cfg.get("horario_fim", "18:00")))
            print(f"🕐 agora={agora} janela={h_ini}-{h_fim}")
            if not (h_ini <= agora <= h_fim):
                print("⏰ Fora do horário — aguardando")
                await asyncio.sleep(1800)
        except Exception as e:
            print(f"⚠️ Erro horário: {e}")

        numero = contato["telefone"].strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "").replace("+", "")
        if not numero.startswith("55"):
            numero = "55" + numero
        print(f"📱 Número: {numero}")

        groq_key = usuario.get("groq_key") if usuario.get("usar_ia_propria") else GROQ_API_KEY
        print(f"🔑 Groq key ok: {bool(groq_key)}")

        jid = f"{numero}@s.whatsapp.net"
        sufixo = numero[-9:]

        # Verifica se já existe conversa ativa com este contato (evita reapresentação)
        conv_existente = db_one(conn,
            "SELECT * FROM conversations WHERE usuario_id=%s AND contact_id=%s AND status='ativa' ORDER BY criado_em DESC LIMIT 1",
            (usuario_id, contact_id))

        if conv_existente:
            conv_existente = dict(conv_existente)
            print(f"♻️ Conversa ativa já existe (id={conv_existente['id']}) — gerando continuação ao invés de apresentação")

            # Busca histórico e gera mensagem de continuação (não apresentação)
            historico = db_all(conn,
                "SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY timestamp ASC",
                (conv_existente["id"],))
            historico = [dict(h) for h in historico]

            historico_cont = historico + [{"role": "user", "content": "[INSTRUÇÃO INTERNA: Retome o contato de forma natural e breve, sem se reapresentar. A pessoa já conhece você. Apenas dê continuidade à conversa.]"}]

            system_prompt = montar_system_prompt(cfg, usuario)
            temperatura   = float(cfg.get("temperatura") or 0.7)
            modelo        = cfg.get("modelo") or "llama-3.1-8b-instant"

            try:
                msg_cont = await chamar_groq(system_prompt, historico_cont, groq_key, temperatura, modelo)
            except Exception as e:
                print(f"⚠️ Erro ao gerar continuação: {e} — abortando envio")
                return

            await enviar_whatsapp(numero, texto=msg_cont)
            db_exec(conn,
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
                (conv_existente["id"], msg_cont))
            db_exec(conn,
                "UPDATE campaigns SET enviados=enviados+1, atualizado_em=NOW() WHERE id=%s",
                (campaign_id,))
            db_exec(conn,
                "UPDATE campaign_contacts SET status='enviado', enviado_em=NOW() WHERE campaign_id=%s AND contact_id=%s",
                (campaign_id, contact_id))
            return

        # Sem conversa prévia — gera mensagem de abertura normal
        print("🤖 Gerando mensagem abertura...")
        msg_abertura = await gerar_mensagem_abertura(cfg, contato, groq_key)
        print(f"✅ Mensagem: {msg_abertura[:60]}")

        print(f"📤 Enviando via Baileys...")
        await enviar_whatsapp(numero, texto=msg_abertura)
        print("✅ Enviado!")

        if cfg.get("midia_abertura_url"):
            await asyncio.sleep(2)
            await enviar_whatsapp(
                numero,
                midia_url=cfg["midia_abertura_url"],
                midia_tipo=cfg.get("midia_abertura_tipo"),
                caption=cfg.get("midia_abertura_caption")
            )

        conv = db_exec(conn,
            "INSERT INTO conversations (usuario_id, contact_id, campaign_id, jid, status, modo) VALUES (%s,%s,%s,%s,'ativa','ia') RETURNING *",
            (usuario_id, contact_id, campaign_id, jid))

        db_exec(conn,
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
            (conv["id"], msg_abertura))

        db_exec(conn,
            "UPDATE contacts SET status='em_contato', atualizado_em=NOW() WHERE id=%s",
            (contact_id,))
        db_exec(conn,
            "UPDATE campaign_contacts SET status='enviado', enviado_em=NOW() WHERE campaign_id=%s AND contact_id=%s",
            (campaign_id, contact_id))
        db_exec(conn,
            "UPDATE campaigns SET enviados=enviados+1, atualizado_em=NOW() WHERE id=%s",
            (campaign_id,))

        max_fu     = int(cfg.get("max_followups") or 2)
        intervalo  = int(cfg.get("intervalo_followup") or 24)
        for i in range(1, max_fu + 1):
            agendado = datetime.now() + timedelta(hours=intervalo * i)
            db_exec(conn,
                "INSERT INTO followups (conversation_id, agendado_para) VALUES (%s,%s)",
                (conv["id"], agendado))

        hoje = date.today()
        db_exec(conn, """
            INSERT INTO analytics_daily (usuario_id, data, abordados, msgs_enviadas)
            VALUES (%s, %s, 1, 1)
            ON CONFLICT (usuario_id, data) DO UPDATE SET
                abordados     = analytics_daily.abordados     + 1,
                msgs_enviadas = analytics_daily.msgs_enviadas + 1
        """, (usuario_id, hoje))

    except Exception as e:
        print(f"Erro ao processar contato {contact_id}: {e}")
    finally:
        conn.close()


async def enfileirar_campanha(campaign_id: int, usuario_id: int, velocidade: int, conn):
    redis = await aioredis.from_url(REDIS_URL)
    contatos = db_all(conn, """
        SELECT cc.contact_id FROM campaign_contacts cc
        WHERE cc.campaign_id=%s AND cc.status='pendente'
    """, (campaign_id,))

    for c in contatos:
        job = json.dumps({
            "campaign_id": campaign_id,
            "contact_id":  c["contact_id"],
            "usuario_id":  usuario_id,
            "velocidade":  velocidade
        })
        await redis.rpush("fila_campanha", job)

    await redis.aclose()
    return len(contatos)


async def worker_campanhas():
    redis = await aioredis.from_url(REDIS_URL)
    print("🔄 Worker de campanhas iniciado")
    while True:
        try:
            job = await redis.blpop("fila_campanha", timeout=5)
            if not job:
                continue
            data        = json.loads(job[1])
            campaign_id = data["campaign_id"]
            contact_id  = data["contact_id"]
            usuario_id  = data["usuario_id"]
            velocidade  = data.get("velocidade", 60)

            await processar_contato_campanha(campaign_id, contact_id, usuario_id)
            await asyncio.sleep(velocidade)

        except Exception as e:
            print(f"Erro no worker: {e}")
            await asyncio.sleep(5)


async def worker_followups():
    print("🔔 Worker de follow-ups iniciado")
    while True:
        conn = get_conn_raw()
        try:
            pendentes = db_all(conn, """
                SELECT f.*, c.jid, c.usuario_id, c.status as conv_status
                FROM followups f
                JOIN conversations c ON c.id = f.conversation_id
                WHERE f.enviado = FALSE AND f.agendado_para <= NOW()
                ORDER BY f.agendado_para ASC
                LIMIT 20
            """)

            for fu in pendentes:
                fu      = dict(fu)

                # Se a conversa foi encerrada, cancela o follow-up silenciosamente
                if fu.get("conv_status") == "encerrada":
                    db_exec(conn, "UPDATE followups SET enviado=TRUE, enviado_em=NOW() WHERE id=%s", (fu["id"],))
                    print(f"⏭️ Follow-up {fu['id']} cancelado — conversa encerrada")
                    continue

                # Verifica se a pessoa já respondeu APÓS o último envio da IA
                ultima_msg_user = db_one(conn, """
                    SELECT id FROM messages
                    WHERE conversation_id=%s AND role='user'
                    ORDER BY timestamp DESC LIMIT 1
                """, (fu["conversation_id"],))

                ultima_msg_ia = db_one(conn, """
                    SELECT timestamp FROM messages
                    WHERE conversation_id=%s AND role='assistant'
                    ORDER BY timestamp DESC LIMIT 1
                """, (fu["conversation_id"],))

                # Se a pessoa respondeu depois da última mensagem da IA, não manda follow-up
                if ultima_msg_user and ultima_msg_ia:
                    msg_user_row = db_one(conn, """
                        SELECT timestamp FROM messages
                        WHERE conversation_id=%s AND role='user'
                        ORDER BY timestamp DESC LIMIT 1
                    """, (fu["conversation_id"],))
                    if msg_user_row and msg_user_row["timestamp"] > ultima_msg_ia["timestamp"]:
                        db_exec(conn, "UPDATE followups SET enviado=TRUE, enviado_em=NOW() WHERE id=%s", (fu["id"],))
                        print(f"⏭️ Follow-up {fu['id']} cancelado — pessoa já respondeu")
                        continue

                usuario = dict(db_one(conn, "SELECT * FROM usuarios WHERE id=%s", (fu["usuario_id"],)) or {})
                cfg     = dict(db_one(conn, "SELECT * FROM ai_config WHERE usuario_id=%s", (fu["usuario_id"],)) or {})

                if not cfg:
                    continue

                numero   = fu["jid"].replace("@s.whatsapp.net", "").replace("@lid", "")
                groq_key = usuario.get("groq_key") if usuario.get("usar_ia_propria") else GROQ_API_KEY

                # Busca histórico da conversa para gerar follow-up contextualizado
                historico = db_all(conn,
                    "SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY timestamp ASC",
                    (fu["conversation_id"],))
                historico = [dict(h) for h in historico]

                # Gera follow-up com IA usando o histórico real da conversa
                try:
                    system_prompt = montar_system_prompt(cfg, usuario)
                    temperatura   = float(cfg.get("temperatura") or 0.7)
                    modelo        = cfg.get("modelo") or "llama-3.1-8b-instant"

                    # Adiciona instrução de follow-up no histórico sem alterar o system prompt
                    historico_fu = historico + [{"role": "user", "content": "[INSTRUÇÃO INTERNA: A pessoa não respondeu ainda. Mande um follow-up curto e natural, sem repetir a apresentação. Apenas retome o contato de forma leve, como se fosse uma mensagem humana.]"}]

                    msg_fu = await chamar_groq(system_prompt, historico_fu, groq_key, temperatura, modelo)
                    print(f"✅ Follow-up gerado para conv={fu['conversation_id']}: {msg_fu[:60]}")
                except Exception as e:
                    print(f"⚠️ Erro ao gerar follow-up com IA, usando mensagem padrão: {e}")
                    persona = cfg.get("persona_nome") or "Assistente"
                    produto = cfg.get("produto_nome") or "nosso serviço"
                    msg_fu  = f"Oi! Queria saber se você teve chance de pensar no que conversamos sobre {produto}. Posso te ajudar com alguma dúvida? 😊"

                try:
                    await enviar_whatsapp(numero, texto=msg_fu)
                    db_exec(conn,
                        "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
                        (fu["conversation_id"], msg_fu))
                    db_exec(conn,
                        "UPDATE followups SET enviado=TRUE, enviado_em=NOW() WHERE id=%s",
                        (fu["id"],))
                except Exception as e:
                    print(f"Erro no follow-up {fu['id']}: {e}")

        except Exception as e:
            print(f"Erro no worker followup: {e}")
        finally:
            conn.close()

        await asyncio.sleep(60)

# ─────────────────────────────────────────
#  LIFESPAN — inicia workers junto com o app
# ─────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sobe os dois workers em background ao iniciar o servidor
    task_camp = asyncio.create_task(worker_campanhas())
    task_fu   = asyncio.create_task(worker_followups())
    yield
    # Cancela ao encerrar
    task_camp.cancel()
    task_fu.cancel()

# ─────────────────────────────────────────
#  APP
# ─────────────────────────────────────────
app = FastAPI(title="Prospector IA", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────
#  AUTH ROUTES
# ─────────────────────────────────────────
@app.post("/auth/register")
def register(body: RegisterBody, conn=Depends(get_db)):
    existe = db_one(conn, "SELECT id FROM usuarios WHERE email = %s", (body.email,))
    if existe:
        raise HTTPException(400, "Email já cadastrado")
    plano      = db_one(conn, "SELECT id FROM planos WHERE nome = 'Free' LIMIT 1")
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
    safe  = {k: v for k, v in dict(user).items() if k != "senha_hash"}
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
@app.get("/ai-config/modelos")
def listar_modelos(user=Depends(get_current_user)):
    return {"modelos": GROQ_MODELOS_FALLBACK}

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
def listar_contacts(status: Optional[str] = None, user=Depends(get_current_user), conn=Depends(get_db)):
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
    content  = await file.read()
    reader   = csv.DictReader(io.StringIO(content.decode("utf-8")))
    inseridos = 0
    erros     = []
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
    if body.status == "ativa":
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
            "number":  conv["jid"].replace("@s.whatsapp.net", "").replace("@lid", ""),
            "message": body.content
        })
    return {"ok": True}

# ─────────────────────────────────────────
#  WHATSAPP STATUS
# ─────────────────────────────────────────
@app.get("/whatsapp/status")
def wpp_status(user=Depends(get_current_user)):
    try:
        r = httpx.get(f"{BAILEYS_URL}/status", timeout=5)
        return r.json()
    except:
        return {"connected": False, "number": ""}

@app.get("/whatsapp/qrcode")
async def wpp_qrcode(user=Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r    = await client.get(f"{BAILEYS_URL}/qrcode")
            html = r.text
            match = re.search(r'src="(data:image[^"]+)"', html)
            if match:
                return {"qr": match.group(1)}
            return {"qr": None}
    except:
        return {"qr": None}

# ─────────────────────────────────────────
#  ANALYTICS
# ─────────────────────────────────────────
@app.get("/analytics")
def analytics(user=Depends(get_current_user), conn=Depends(get_db)):
    totais = db_one(conn, """
        SELECT
            COUNT(DISTINCT c.id)                                           AS total_contatos,
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
        "totais":    dict(totais),
        "historico": [dict(r) for r in historico]
    }

# ─────────────────────────────────────────
#  WEBHOOK — recebe eventos do Baileys
# ─────────────────────────────────────────
@app.post("/webhook/mensagem")
async def webhook_mensagem(payload: dict, conn=Depends(get_db)):
    await processar_mensagem(payload, conn)
    return {"ok": True}

# ─────────────────────────────────────────
#  ROOT
# ─────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "Prospector IA rodando"}

# ─────────────────────────────────────────
#  ADMIN — gerenciamento de usuários
# ─────────────────────────────────────────
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin-token-troque-em-producao")

def check_admin(x_admin_token: str = ""):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Acesso negado")

class AdminCreateUserBody(BaseModel):
    nome: str
    email: str
    senha: str
    plano_nome: Optional[str] = "Free"

class AdminAtivoBody(BaseModel):
    ativo: bool

class AdminPlanoBody(BaseModel):
    plano_nome: str

@app.get("/admin/usuarios")
def admin_listar_usuarios(x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    rows = db_all(conn, """
        SELECT u.id, u.nome, u.email, u.ativo, u.criado_em,
               p.nome as plano_nome
        FROM usuarios u
        LEFT JOIN planos p ON p.id = u.plano_id
        ORDER BY u.criado_em DESC
    """)
    return [dict(r) for r in rows]

@app.post("/admin/usuarios")
def admin_criar_usuario(body: AdminCreateUserBody, x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    existe = db_one(conn, "SELECT id FROM usuarios WHERE email = %s", (body.email,))
    if existe:
        raise HTTPException(400, "Email já cadastrado")
    plano = db_one(conn, "SELECT id FROM planos WHERE nome = %s LIMIT 1", (body.plano_nome,))
    senha_hash = bcrypt.hashpw(body.senha.encode(), bcrypt.gensalt()).decode()
    user = db_exec(conn,
        "INSERT INTO usuarios (nome, email, senha_hash, plano_id, ativo) VALUES (%s,%s,%s,%s,TRUE) RETURNING id, nome, email",
        (body.nome, body.email, senha_hash, plano["id"] if plano else None))
    return dict(user)

@app.patch("/admin/usuarios/{user_id}/ativo")
def admin_toggle_ativo(user_id: int, body: AdminAtivoBody, x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    db_exec(conn, "UPDATE usuarios SET ativo=%s WHERE id=%s", (body.ativo, user_id))
    return {"ok": True}

@app.patch("/admin/usuarios/{user_id}/plano")
def admin_trocar_plano(user_id: int, body: AdminPlanoBody, x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    plano = db_one(conn, "SELECT id FROM planos WHERE nome = %s LIMIT 1", (body.plano_nome,))
    if not plano:
        raise HTTPException(404, "Plano não encontrado")
    db_exec(conn, "UPDATE usuarios SET plano_id=%s WHERE id=%s", (plano["id"], user_id))
    return {"ok": True}

@app.delete("/admin/usuarios/{user_id}")
def admin_deletar_usuario(user_id: int, x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    db_exec(conn, "DELETE FROM usuarios WHERE id=%s", (user_id,))
    return {"ok": True}

@app.get("/admin/planos")
def admin_listar_planos(x_admin_token: str = "", conn=Depends(get_db)):
    check_admin(x_admin_token)
    rows = db_all(conn, "SELECT * FROM planos ORDER BY id")
    return [dict(r) for r in rows]

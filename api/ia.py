"""
IA — Processa mensagens recebidas e decide resposta
"""
import os, json
import psycopg2.extras
import httpx

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY_PADRAO = os.getenv("GROQ_API_KEY", "")
BAILEYS_URL = os.getenv("BAILEYS_URL", "http://baileys:3000")

GATILHOS_PARADA_PADRAO = ["não quero", "nao quero", "para", "chega", "sai", "remove", "cancelar"]


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


def get_groq_key(usuario: dict) -> str:
    if usuario.get("usar_ia_propria") and usuario.get("groq_key"):
        return usuario["groq_key"]
    return GROQ_KEY_PADRAO


def checar_gatilho_parada(texto: str, gatilhos_str: str) -> bool:
    texto_lower = texto.lower()
    gatilhos = GATILHOS_PARADA_PADRAO[:]
    if gatilhos_str:
        gatilhos += [g.strip().lower() for g in gatilhos_str.split(",")]
    return any(g in texto_lower for g in gatilhos)


def checar_pedido_responsavel(texto: str) -> bool:
    """Detecta se a IA pediu o número do responsável na última mensagem."""
    sinais = ["responsável", "responsavel", "decisor", "quem decide", "falar com"]
    return any(s in texto.lower() for s in sinais)


def checar_telefone_na_resposta(texto: str):
    """Tenta extrair um número de telefone da resposta do contato."""
    import re
    match = re.search(r"(\+?\d[\d\s\-\(\)]{8,15}\d)", texto)
    return match.group(1).strip() if match else None


def montar_system_prompt(cfg: dict, usuario: dict) -> str:
    persona  = cfg.get("persona_nome") or "Assistente"
    produto  = cfg.get("produto_nome") or "nosso serviço"
    descricao = cfg.get("produto_descricao") or ""
    preco    = cfg.get("produto_preco") or ""
    prompt   = cfg.get("prompt_sistema") or ""

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

{prompt}"""
    return base.strip()


async def chamar_groq(system_prompt: str, historico: list, groq_key: str, temperatura: float, modelo: str) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    for msg in historico[-10:]:  # últimas 10 mensagens
        role = "assistant" if msg["role"] == "assistant" else "user"
        messages.append({"role": role, "content": msg["content"] or ""})

    headers = {
        "Authorization": f"Bearer {groq_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": modelo or "llama3-8b-8192",
        "temperature": float(temperatura or 0.7),
        "max_tokens": 500,
        "messages": messages
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


async def enviar_whatsapp(numero: str, texto: str = None, midia_url: str = None, midia_tipo: str = None, caption: str = None):
    payload = {"number": numero, "message": texto or caption or ""}
    if midia_url:
        if midia_tipo == "video":
            payload["videoUrl"] = midia_url
        else:
            payload["image"] = midia_url
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{BAILEYS_URL}/disparar", json=payload)


async def processar_mensagem(payload: dict, conn):
    """
    Chamado pelo webhook quando chega mensagem nova.
    payload: { usuario_id, remoteJid, pushName, text, fromMe }
    """
    usuario_id = payload.get("usuario_id")
    jid        = payload.get("remoteJid", "")
    texto      = payload.get("text", "") or ""
    from_me    = payload.get("fromMe", False)

    if from_me or not texto or not usuario_id:
        return

    numero = jid.replace("@s.whatsapp.net", "")

    # Busca usuário e config
    usuario = db_one(conn, "SELECT * FROM usuarios WHERE id = %s", (usuario_id,))
    if not usuario:
        return
    usuario = dict(usuario)

    cfg = db_one(conn, "SELECT * FROM ai_config WHERE usuario_id = %s", (usuario_id,))
    if not cfg:
        return
    cfg = dict(cfg)

    # Verifica horário de funcionamento
    from datetime import datetime, time
    agora = datetime.now().time()
    try:
        h_ini = time.fromisoformat(str(cfg.get("horario_inicio", "08:00")))
        h_fim = time.fromisoformat(str(cfg.get("horario_fim", "18:00")))
        if not (h_ini <= agora <= h_fim):
            return  # fora do horário
    except:
        pass

    # Busca ou cria conversa
    conv = db_one(conn, "SELECT * FROM conversations WHERE usuario_id=%s AND jid=%s", (usuario_id, jid))
    if not conv:
        contact = db_one(conn, "SELECT id FROM contacts WHERE usuario_id=%s AND telefone=%s", (usuario_id, numero))
        contact_id = contact["id"] if contact else None
        conv = db_exec(conn,
            "INSERT INTO conversations (usuario_id, contact_id, jid, status, modo) VALUES (%s,%s,%s,'ativa','ia') RETURNING *",
            (usuario_id, contact_id, jid))
    conv = dict(conv)

    # Se modo manual, não responde
    if conv.get("modo") == "manual":
        return

    # Salva mensagem recebida
    db_exec(conn,
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'user',%s)",
        (conv["id"], texto))

    # Checa gatilho de parada
    if checar_gatilho_parada(texto, cfg.get("gatilhos_parada", "")):
        db_exec(conn,
            "UPDATE conversations SET status='encerrada', atualizado_em=NOW() WHERE id=%s",
            (conv["id"],))
        db_exec(conn,
            "UPDATE contacts SET status='perdido', atualizado_em=NOW() WHERE usuario_id=%s AND telefone=%s",
            (usuario_id, numero))
        return

    # Checa se a última mensagem nossa pedia responsável e esse texto tem telefone
    ultima_nossa = db_one(conn,
        "SELECT content FROM messages WHERE conversation_id=%s AND role='assistant' ORDER BY timestamp DESC LIMIT 1",
        (conv["id"],))
    if ultima_nossa and checar_pedido_responsavel(ultima_nossa["content"] or ""):
        tel_resp = checar_telefone_na_resposta(texto)
        if tel_resp:
            db_exec(conn,
                "UPDATE contacts SET responsavel_telefone=%s, atualizado_em=NOW() WHERE usuario_id=%s AND telefone=%s",
                (tel_resp, usuario_id, numero))
            # Agenda contato com o responsável (fila)
            db_exec(conn,
                "INSERT INTO contacts (usuario_id, nome, telefone, notas, status) VALUES (%s,%s,%s,%s,'pendente') ON CONFLICT DO NOTHING",
                (usuario_id, "Responsável via " + numero, tel_resp, f"Indicado por {numero}"))

    # Busca histórico
    historico = db_all(conn,
        "SELECT role, content FROM messages WHERE conversation_id=%s ORDER BY timestamp ASC",
        (conv["id"],))
    historico = [dict(h) for h in historico]

    # Chama Groq
    groq_key     = get_groq_key(usuario)
    system_prompt = montar_system_prompt(cfg, usuario)
    temperatura  = float(cfg.get("temperatura") or 0.7)
    modelo       = cfg.get("modelo") or "llama3-8b-8192"

    try:
        resposta = await chamar_groq(system_prompt, historico, groq_key, temperatura, modelo)
    except Exception as e:
        print(f"Erro Groq: {e}")
        return

    # Delay simulando digitação (feito no Baileys, aqui só aguarda um pouco)
    import asyncio
    delay = int(cfg.get("delay_mensagens") or 3)
    await asyncio.sleep(min(delay, 10))

    # Envia resposta
    await enviar_whatsapp(numero, texto=resposta)

    # Salva resposta
    db_exec(conn,
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
        (conv["id"], resposta))
    db_exec(conn,
        "UPDATE conversations SET atualizado_em=NOW() WHERE id=%s",
        (conv["id"],))

    # Verifica se deve enviar mídia de fechamento
    sinais_interesse = ["quero", "interesse", "como funciona", "valor", "preço", "quanto custa", "me conta mais"]
    if any(s in texto.lower() for s in sinais_interesse):
        midia_url  = cfg.get("midia_fechamento_url")
        midia_tipo = cfg.get("midia_fechamento_tipo")
        caption    = cfg.get("midia_fechamento_caption")
        if midia_url:
            await asyncio.sleep(2)
            await enviar_whatsapp(numero, midia_url=midia_url, midia_tipo=midia_tipo, caption=caption)

    # Atualiza analytics
    from datetime import date
    hoje = date.today()
    db_exec(conn, """
        INSERT INTO analytics_daily (usuario_id, data, msgs_enviadas, msgs_recebidas)
        VALUES (%s, %s, 1, 1)
        ON CONFLICT (usuario_id, data) DO UPDATE SET
            msgs_enviadas  = analytics_daily.msgs_enviadas  + 1,
            msgs_recebidas = analytics_daily.msgs_recebidas + 1
    """, (usuario_id, hoje))

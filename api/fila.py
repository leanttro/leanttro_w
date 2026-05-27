"""
FILA — Gerencia disparo de campanhas via Redis
Roda como worker separado em background
"""
import os, asyncio, json
from datetime import datetime, date, time
import psycopg2
import psycopg2.extras
import httpx
import redis.asyncio as aioredis

DATABASE_URL = os.getenv("DATABASE_URL",
    "postgresql://leanttro:Fin@2021@wpp-wpp-a3wnej:5432/postgres")
REDIS_URL    = os.getenv("REDIS_URL", "redis://redis:6379")
BAILEYS_URL  = os.getenv("BAILEYS_URL", "http://baileys:3000")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY_PADRAO = os.getenv("GROQ_API_KEY", "")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

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


async def gerar_mensagem_abertura(cfg: dict, contato: dict, groq_key: str) -> str:
    """Gera primeira mensagem personalizada pra cada contato."""
    nome     = contato.get("nome") or "prezado"
    empresa  = contato.get("empresa") or ""
    produto  = cfg.get("produto_nome") or "nosso serviço"
    persona  = cfg.get("persona_nome") or "Assistente"
    prompt   = cfg.get("prompt_sistema") or ""

    system = f"""Você é {persona}, especialista em vendas.
{prompt}
Crie uma PRIMEIRA mensagem de prospecção no WhatsApp para {nome}{f' da empresa {empresa}' if empresa else ''}.
Ofereça: {produto}
Regras: seja natural, curto (máx 3 linhas), não pareça spam, personalize pelo nome/empresa."""

    headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
    payload = {
        "model": cfg.get("modelo") or "llama3-8b-8192",
        "temperature": float(cfg.get("temperatura") or 0.7),
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": "Gere a mensagem de abertura agora."}
        ]
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def enviar_whatsapp(numero: str, texto: str = None, midia_url: str = None, midia_tipo: str = None, caption: str = None):
    payload = {"number": numero, "message": texto or caption or ""}
    if midia_url:
        if midia_tipo == "video":
            payload["videoUrl"] = midia_url
        else:
            payload["imageUrl"] = midia_url
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{BAILEYS_URL}/disparar", json=payload)


async def processar_contato_campanha(campaign_id: int, contact_id: int, usuario_id: int):
    conn = get_conn()
    try:
        usuario  = dict(db_one(conn, "SELECT * FROM usuarios WHERE id=%s", (usuario_id,)))
        cfg      = dict(db_one(conn, "SELECT * FROM ai_config WHERE usuario_id=%s", (usuario_id,)) or {})
        contato  = dict(db_one(conn, "SELECT * FROM contacts WHERE id=%s", (contact_id,)) or {})

        if not contato or not cfg:
            return

        # Verifica horário
        agora = datetime.now().time()
        try:
            h_ini = time.fromisoformat(str(cfg.get("horario_inicio", "08:00")))
            h_fim = time.fromisoformat(str(cfg.get("horario_fim", "18:00")))
            if not (h_ini <= agora <= h_fim):
                # Reagenda pra depois
                await asyncio.sleep(1800)
        except:
            pass

        numero = contato["telefone"].strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not numero.startswith("+"):
            numero = "55" + numero  # assume Brasil

        groq_key = usuario.get("groq_key") if usuario.get("usar_ia_propria") else GROQ_KEY_PADRAO

        # Gera mensagem personalizada
        msg_abertura = await gerar_mensagem_abertura(cfg, contato, groq_key)

        # Envia mensagem
        await enviar_whatsapp(numero, texto=msg_abertura)

        # Envia mídia de abertura se configurada
        if cfg.get("midia_abertura_url"):
            await asyncio.sleep(2)
            await enviar_whatsapp(
                numero,
                midia_url=cfg["midia_abertura_url"],
                midia_tipo=cfg.get("midia_abertura_tipo"),
                caption=cfg.get("midia_abertura_caption")
            )

        # Cria conversa
        jid = f"{numero}@s.whatsapp.net"
        conv = db_exec(conn,
            "INSERT INTO conversations (usuario_id, contact_id, campaign_id, jid, status, modo) VALUES (%s,%s,%s,%s,'ativa','ia') RETURNING *",
            (usuario_id, contact_id, campaign_id, jid))

        # Salva mensagem enviada
        db_exec(conn,
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s,'assistant',%s)",
            (conv["id"], msg_abertura))

        # Atualiza status do contato e campaign_contact
        db_exec(conn,
            "UPDATE contacts SET status='em_contato', atualizado_em=NOW() WHERE id=%s",
            (contact_id,))
        db_exec(conn,
            "UPDATE campaign_contacts SET status='enviado', enviado_em=NOW() WHERE campaign_id=%s AND contact_id=%s",
            (campaign_id, contact_id))
        db_exec(conn,
            "UPDATE campaigns SET enviados=enviados+1, atualizado_em=NOW() WHERE id=%s",
            (campaign_id,))

        # Agenda follow-up
        max_fu = int(cfg.get("max_followups") or 2)
        intervalo = int(cfg.get("intervalo_followup") or 24)
        for i in range(1, max_fu + 1):
            from datetime import timedelta
            agendado = datetime.now() + timedelta(hours=intervalo * i)
            db_exec(conn,
                "INSERT INTO followups (conversation_id, agendado_para) VALUES (%s,%s)",
                (conv["id"], agendado))

        # Analytics
        hoje = date.today()
        db_exec(conn, """
            INSERT INTO analytics_daily (usuario_id, data, abordados, msgs_enviadas)
            VALUES (%s, %s, 1, 1)
            ON CONFLICT (usuario_id, data) DO UPDATE SET
                abordados    = analytics_daily.abordados    + 1,
                msgs_enviadas = analytics_daily.msgs_enviadas + 1
        """, (usuario_id, hoje))

    except Exception as e:
        print(f"Erro ao processar contato {contact_id}: {e}")
    finally:
        conn.close()


async def worker_campanhas():
    """Pega jobs da fila Redis e processa."""
    redis = await aioredis.from_url(REDIS_URL)
    print("🔄 Worker de campanhas iniciado")

    while True:
        try:
            job = await redis.blpop("fila_campanha", timeout=5)
            if not job:
                continue
            data = json.loads(job[1])
            campaign_id = data["campaign_id"]
            contact_id  = data["contact_id"]
            usuario_id  = data["usuario_id"]
            velocidade  = data.get("velocidade", 60)

            await processar_contato_campanha(campaign_id, contact_id, usuario_id)
            await asyncio.sleep(velocidade)  # respeita velocidade configurada

        except Exception as e:
            print(f"Erro no worker: {e}")
            await asyncio.sleep(5)


async def worker_followups():
    """Verifica follow-ups agendados e envia."""
    redis = await aioredis.from_url(REDIS_URL)
    print("🔔 Worker de follow-ups iniciado")

    while True:
        conn = get_conn()
        try:
            pendentes = db_all(conn, """
                SELECT f.*, c.jid, c.usuario_id
                FROM followups f
                JOIN conversations c ON c.id = f.conversation_id
                WHERE f.enviado = FALSE AND f.agendado_para <= NOW()
                ORDER BY f.agendado_para ASC
                LIMIT 20
            """)

            for fu in pendentes:
                fu = dict(fu)
                usuario = dict(db_one(conn, "SELECT * FROM usuarios WHERE id=%s", (fu["usuario_id"],)) or {})
                cfg     = dict(db_one(conn, "SELECT * FROM ai_config WHERE usuario_id=%s", (fu["usuario_id"],)) or {})

                if not cfg:
                    continue

                numero = fu["jid"].replace("@s.whatsapp.net", "")
                groq_key = usuario.get("groq_key") if usuario.get("usar_ia_propria") else GROQ_KEY_PADRAO

                # Gera follow-up
                persona = cfg.get("persona_nome") or "Assistente"
                produto = cfg.get("produto_nome") or "nosso serviço"
                msg_fu  = f"Oi! Sou {persona}. Queria saber se você teve chance de pensar na proposta que te enviei sobre {produto}. Posso te ajudar com alguma dúvida? 😊"

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

        await asyncio.sleep(60)  # checa a cada minuto


async def enfileirar_campanha(campaign_id: int, usuario_id: int, velocidade: int, conn):
    """Adiciona todos os contatos pendentes da campanha na fila Redis."""
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


async def main():
    await asyncio.gather(
        worker_campanhas(),
        worker_followups()
    )


if __name__ == "__main__":
    asyncio.run(main())

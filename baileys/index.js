import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import axios from 'axios'
import qrcode from 'qrcode-terminal'
import QRCode from 'qrcode'           // ← NOVO: gera QR como imagem
import pino from 'pino'
import express from 'express'
import cors from 'cors'
import { createServer } from 'http'
import { Server } from 'socket.io'

const app = express()
app.use(cors())
app.use(express.json({ limit: '50mb' }))

const httpServer = createServer(app)
const io = new Server(httpServer, { 
    cors: { origin: '*', methods: ["GET", "POST"] } 
})

const TYPEBOT_URL = process.env.TYPEBOT_URL
let sockGlobal
let lastQR = null   // ← guarda o último QR gerado
const lidMap = {}   // mapa LID → JID real (ex: "123...@lid" → "5511...@s.whatsapp.net")

// =====================================================================
// ROTA QR CODE — acesse no navegador: http://SEU_IP:3001/qrcode
// =====================================================================
app.get('/qrcode', async (req, res) => {
    if (!lastQR) {
        return res.send(`
            <html><body style="font-family:sans-serif;text-align:center;padding:40px">
                <h2>QR Code ainda não disponível</h2>
                <p>Aguarde alguns segundos e recarregue a página.</p>
                <script>setTimeout(()=>location.reload(), 3000)</script>
            </body></html>
        `)
    }
    try {
        const qrImageUrl = await QRCode.toDataURL(lastQR)
        res.send(`
            <html><body style="font-family:sans-serif;text-align:center;padding:40px;background:#f0f0f0">
                <h2>📱 Escaneie o QR Code no WhatsApp</h2>
                <img src="${qrImageUrl}" style="width:300px;height:300px;border:4px solid #25D366;border-radius:8px"/>
                <p>WhatsApp → Dispositivos Conectados → Conectar dispositivo</p>
                <p style="color:#999;font-size:12px">Esta página atualiza sozinha a cada 5s</p>
                <script>setTimeout(()=>location.reload(), 5000)</script>
            </body></html>
        `)
    } catch (e) {
        res.status(500).send('Erro ao gerar QR: ' + e.message)
    }
})

async function connectToWhatsApp() {
    const { version, isLatest } = await fetchLatestBaileysVersion()
    console.log(`Versão do WhatsApp Web: v${version.join('.')}`)

    const { state, saveCreds } = await useMultiFileAuthState('auth_info_final')
    
    const sock = makeWASocket({
        version,
        auth: state,
        logger: pino({ level: 'silent' }), 
        printQRInTerminal: false,
        browser: ["Ubuntu", "Chrome", "122.0.0"],   // ← versão corrigida
        generateHighQualityLinkPreview: true,
        syncFullHistory: false
    })

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update
        
        if (qr) {
            lastQR = qr  // ← salva para a rota /qrcode
            console.log('\n👇 QR disponível em: http://localhost:3000/qrcode')
            qrcode.generate(qr, { small: true })
            // também emite pelo WebSocket para quem estiver conectado
            try {
                const qrImageUrl = await QRCode.toDataURL(qr)
                io.emit('qr_code', { qr: qrImageUrl })
            } catch(e) {}
        }

        if (connection === 'close') {
            lastQR = null
            const shouldReconnect = (lastDisconnect?.error instanceof Boom)
                ? lastDisconnect.error.output?.statusCode !== DisconnectReason.loggedOut
                : true
            console.log('Conexão fechada. Tentando reconectar:', shouldReconnect)
            if (shouldReconnect) connectToWhatsApp()
        } else if (connection === 'open') {
            lastQR = null
            console.log('✅ WHATSAPP CONECTADO - AGUARDANDO COMANDOS')
            sockGlobal = sock
            io.emit('wpp_conectado', { status: true })
        }
    })

    sock.ev.on('creds.update', saveCreds)

    // Constrói mapa LID → número real para resolver @lid nas mensagens recebidas
    sock.ev.on('contacts.upsert', (contacts) => {
        for (const c of contacts) {
            if (c.lid && c.id) {
                lidMap[c.lid] = c.id
            }
            // também indexa o próprio JID caso venha como lid no futuro
            if (c.id && c.id.endsWith('@lid') && c.notify) {
                lidMap[c.id] = c.id
            }
        }
    })

    sock.ev.on('contacts.update', (updates) => {
        for (const c of updates) {
            if (c.lid && c.id) {
                lidMap[c.lid] = c.id
            }
        }
    })

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return
        const msg = messages[0]

        const remoteJid = msg.key.remoteJid

        // Ignora grupos (@g.us) — responde APENAS no privado
        if (remoteJid.endsWith('@g.us')) return

        const pushName  = msg.pushName || ''
        const fromMe    = msg.key.fromMe || false

        const text      = msg.message?.conversation
                       || msg.message?.extendedTextMessage?.text
                       || null

        const imageMsg   = msg.message?.imageMessage
        const videoMsg   = msg.message?.videoMessage
        const audioMsg   = msg.message?.audioMessage
        const stickerMsg = msg.message?.stickerMessage

        if (text) {
            io.emit('nova_mensagem', { remoteJid, pushName, text, fromMe })
        } else if (imageMsg) {
            try {
                const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                const base64 = buffer.toString('base64')
                const mime   = imageMsg.mimetype || 'image/jpeg'
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, mediaType: 'image', mediaBase64: `data:${mime};base64,${base64}`, caption: imageMsg.caption || '' })
            } catch(e) {
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, text: '📷 Imagem (erro ao baixar)' })
            }
        } else if (videoMsg) {
            try {
                const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                const base64 = buffer.toString('base64')
                const mime   = videoMsg.mimetype || 'video/mp4'
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, mediaType: 'video', mediaBase64: `data:${mime};base64,${base64}`, caption: videoMsg.caption || '' })
            } catch(e) {
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, text: '🎥 Vídeo (erro ao baixar)' })
            }
        } else if (audioMsg) {
            try {
                const buffer = await downloadMediaMessage(msg, 'buffer', {}, { logger: pino({ level: 'silent' }), reuploadRequest: sock.updateMediaMessage })
                const base64 = buffer.toString('base64')
                const mime   = audioMsg.mimetype || 'audio/ogg; codecs=opus'
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, mediaType: 'audio', mediaBase64: `data:${mime};base64,${base64}` })
            } catch(e) {
                io.emit('nova_mensagem', { remoteJid, pushName, fromMe, text: '🎵 Áudio (erro ao baixar)' })
            }
        } else if (stickerMsg) {
            io.emit('nova_mensagem', { remoteJid, pushName, fromMe, text: '🎨 Sticker' })
        }

        if (!fromMe && text) {
            // ── Envia para a IA do Prospector ──────────────────────────────────
            const usuarioId = parseInt(process.env.USUARIO_ID || '1')
            try {
                // Resolve @lid para número real (@s.whatsapp.net)
                // Tenta primeiro o mapa local (contacts.upsert), depois onWhatsApp()
                let jidResolvido = remoteJid
                if (remoteJid.endsWith('@lid')) {
                    if (lidMap[remoteJid]) {
                        jidResolvido = lidMap[remoteJid]
                        console.log(`🔄 LID resolvido (mapa): ${remoteJid} → ${jidResolvido}`)
                    } else {
                        try {
                            const resultado = await sock.onWhatsApp(remoteJid)
                            if (resultado && resultado[0]?.jid) {
                                jidResolvido = resultado[0].jid
                                lidMap[remoteJid] = jidResolvido  // cacheia pra próxima
                                console.log(`🔄 LID resolvido (onWhatsApp): ${remoteJid} → ${jidResolvido}`)
                            } else {
                                console.log(`⚠️ LID não resolvido ${remoteJid} — usando LID mesmo`)
                            }
                        } catch (e) {
                            console.log(`⚠️ Erro ao resolver LID ${remoteJid}: ${e.message} — usando LID mesmo`)
                        }
                    }
                }

                await axios.post(process.env.API_WEBHOOK_URL || 'http://api:8000/webhook/mensagem', {
                    usuario_id: usuarioId,
                    remoteJid: jidResolvido,
                    pushName,
                    text,
                    fromMe
                })
                console.log(`📨 Webhook enviado para IA (usuario_id=${usuarioId}) | ${jidResolvido}`)
            } catch (e) {
                console.error('Erro ao chamar webhook IA:', e.message)
            }

            // ── Typebot (só roda se TYPEBOT_URL estiver configurado E USAR_TYPEBOT=true) ──
            if (TYPEBOT_URL && process.env.USAR_TYPEBOT === 'true') {
                try {
                    const { data } = await axios.post(TYPEBOT_URL, { message: text, remoteJid })
                    if (data.messages && data.messages.length > 0) {
                        for (const message of data.messages) {
                            await sock.sendPresenceUpdate('composing', remoteJid)
                            await new Promise(r => setTimeout(r, 800))
                            if (message.type === 'text') {
                                const responseText = message.content.richText.map(n => n.children.map(c => c.text).join('')).join('\n')
                                await sock.sendMessage(remoteJid, { text: responseText })
                                io.emit('nova_mensagem', { remoteJid, text: responseText, fromMe: true })
                            } else if (message.type === 'image') {
                                await sock.sendMessage(remoteJid, { image: { url: message.content.url } })
                                io.emit('nova_mensagem', { remoteJid, text: 'Imagem enviada', fromMe: true })
                            } else if (message.type === 'audio') {
                                await sock.sendMessage(remoteJid, { audio: { url: message.content.url }, mimetype: 'audio/mp4', ptt: true })
                                io.emit('nova_mensagem', { remoteJid, text: 'Áudio enviado', fromMe: true })
                            }
                        }
                    }
                } catch (error) {
                    console.error('❌ ERRO NA INTEGRAÇÃO COM TYPEBOT:', error.response?.data || error.message)
                }
            }
        }
    })
}

io.on('connection', (socket) => {
    console.log('Interface web conectada no WebSocket')

    // Se já tem QR pendente, envia imediatamente para quem acabou de conectar
    if (lastQR) {
        QRCode.toDataURL(lastQR).then(url => socket.emit('qr_code', { qr: url })).catch(() => {})
    }
    
    socket.on('enviar_resposta', async (data) => {
        if (!sockGlobal || !data.jid) return
        try {
            if (data.mediaBase64) {
                const matches = data.mediaBase64.match(/^data:([^;]+);base64,(.+)$/)
                if (!matches) return
                const mime   = matches[1]
                const buffer = Buffer.from(matches[2], 'base64')
                const caption = data.caption || ''
                if (data.mediaType === 'video') {
                    await sockGlobal.sendMessage(data.jid, { video: buffer, caption, mimetype: mime })
                } else {
                    await sockGlobal.sendMessage(data.jid, { image: buffer, caption, mimetype: mime })
                }
                io.emit('nova_mensagem', { remoteJid: data.jid, fromMe: true, mediaType: data.mediaType, mediaBase64: data.mediaBase64, caption })
            } else if (data.text) {
                await sockGlobal.sendMessage(data.jid, { text: data.text })
                io.emit('nova_mensagem', { remoteJid: data.jid, text: data.text, fromMe: true })
            }
        } catch (err) {
            console.error('Erro ao responder via painel:', err)
        }
    })
})

app.get('/status', (req, res) => {
    if (sockGlobal && sockGlobal.user) {
        res.json({ connected: true, number: sockGlobal.user.id.split(':')[0] })
    } else {
        res.json({ connected: false, number: "", qrPending: !!lastQR })
    }
})

app.post('/disparar', async (req, res) => {
    try {
        const { number, message, image, videoUrl } = req.body
        if (!sockGlobal) return res.status(503).json({ error: "WhatsApp não conectado." })
        if (!number || !message) return res.status(400).json({ error: "Número e mensagem são obrigatórios." })
        // Se vier JID completo (com @), usa direto; senão monta com @s.whatsapp.net
        const jid = number.includes('@') ? number : `${number}@s.whatsapp.net`
        await sockGlobal.sendPresenceUpdate('composing', jid)
        await new Promise(r => setTimeout(r, 1500))
        if (videoUrl) {
            await sockGlobal.sendMessage(jid, { video: { url: videoUrl }, caption: message })
        } else if (image) {
            await sockGlobal.sendMessage(jid, { image: Buffer.from(image, 'base64'), caption: message })
        } else {
            await sockGlobal.sendMessage(jid, { text: message })
        }
        io.emit('nova_mensagem', { remoteJid: jid, text: message, fromMe: true })
        console.log(`🚀 Mensagem enviada via API para: ${number}`)
        res.status(200).json({ status: "success", message: "Disparo efetuado" })
    } catch (error) {
        console.error("Falha no disparo via API:", error)
        res.status(500).json({ error: error.message })
    }
})

app.post('/ia-responder', async (req, res) => {
    try {
        const { number, message } = req.body
        if (!sockGlobal) return res.status(503).json({ error: "WhatsApp não conectado." })
        if (!number || !message) return res.status(400).json({ error: "number e message são obrigatórios." })
        const jid = number.includes('@') ? number : `${number}@s.whatsapp.net`
        await sockGlobal.sendPresenceUpdate('composing', jid)
        await new Promise(r => setTimeout(r, 1200))
        await sockGlobal.sendMessage(jid, { text: message })
        io.emit('nova_mensagem', { remoteJid: jid, text: message, fromMe: true })
        console.log(`🤖 IA Vendedora respondeu para: ${number}`)
        res.status(200).json({ status: "success" })
    } catch (error) {
        console.error("Erro no /ia-responder:", error)
        res.status(500).json({ error: error.message })
    }
})

httpServer.listen(3000, () => {
    console.log('🚀 SERVIDOR RODANDO NA PORTA 3000')
    console.log('📱 QR Code disponível em: /qrcode')
    connectToWhatsApp()
})

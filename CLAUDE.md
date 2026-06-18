# CLAUDE.md

Guia para o Claude Code trabalhar neste repositório.

## Visão geral

**Quiz CFC** — quiz ao vivo no estilo Kahoot!, com as cores do Conselho Federal de
Contabilidade. Backend assíncrono FastAPI + WebSockets, estado do jogo em memória,
front-end estático animado. Duas telas: apresentador (telão) e jogador (celular).

## Comandos

```bash
pip install -r requirements.txt
python iniciar.py                # MODO EVENTO: servidor + túnel público + abre /host na URL pública
python server.py                 # só o servidor, em 0.0.0.0:8000
PORT=8123 python server.py       # porta/host configuráveis via env (HOST, PORT)
python test_e2e.py               # teste e2e — exige o servidor rodando na porta 8123
```

`iniciar.py` é o lançador para eventos: sobe `server.py` como subprocesso, abre o
`cloudflared tunnel`, faz parse da URL `*.trycloudflare.com`, abre o navegador em
`{url}/host` (QR aponta para o endereço público) e encerra servidor+túnel juntos no
Ctrl+C. Localiza o cloudflared via PATH ou
`%LOCALAPPDATA%/Microsoft/WinGet/Links/cloudflared.exe`.

**Seleção de túnel (`TUNEL`, padrão `auto`).** Em `auto`, o lançador testa se a edge do
Cloudflare (`region1.v2.argotunnel.com:7844`) sai pela rede atual: se sim usa o
cloudflared; se **não** (rede que bloqueia a 7844, ex.: Wi-Fi corporativo `CFC-VISITANTE`
→ erro **1033**) cai para o **localhost.run via SSH** (`ssh -R 80:localhost:PORT
localhost.run`), que sai pela 443/22 e gera URL `*.lhr.life`. Force com
`TUNEL=cloudflared`, `TUNEL=ssh` (localhost.run) ou `TUNEL=local` (sem túnel).
O localhost.run **exige uma chave SSH** — `garantir_chave_ssh()` gera uma `id_ed25519`
sem passphrase em `~/.ssh` se não houver nenhuma. Sem cloudflared e sem ssh, cai para
rede local.

O `test_e2e.py` conecta em `ws://127.0.0.1:8123`, por isso o teste precisa do servidor
na porta **8123** (`PORT=8123 python server.py`). Ele simula apresentador + 3 jogadores
e valida pontuação, ranking e pódio (`assert rounds == total_perguntas`).

## Arquitetura

| Arquivo | Papel |
|---------|-------|
| `iniciar.py` | Lançador (modo evento): servidor + túnel + abre `/host` na URL pública |
| `server.py` | Backend: estado do jogo (`Game`/`Player`), rotas HTTP/WS, geração de QR em SVG, pontuação, admin |
| `questions.json` | Banco de perguntas (`titulo`, `tempo_padrao`, `perguntas[]`) |
| `static/host.html` | Tela do apresentador (telão): QR, timer destacado, ranking, pódio + modal de pontuação completa, botão ⚙ Perguntas → `/admin` |
| `static/player.html` | Tela do jogador (celular): entra com **nome**, vê enunciado + textos das respostas, timer destacado |
| `static/admin.html` | Tela de administração de perguntas (CRUD + reordenar), protegida por senha |
| `static/logo-cfc.png` | Logo do CFC (recortada de `logo CFC.png`), usada em host/player/admin num selo branco (`.logo-badge`) |
| `test_e2e.py` | Teste ponta-a-ponta via WebSocket |
| `shot.py` | Screenshot do lobby via Playwright (gera `host_lobby.png`) |
| `Dockerfile` / `docker-compose.yml` / `Caddyfile` | Deploy em VPS: app (uvicorn 1 processo) + Caddy com HTTPS automático |
| `.env.example` | Modelo de variáveis (`DOMAIN`, `ADMIN_PASSWORD`) — copie para `.env` (gitignored) |
| `deploy/quiz-cfc.service` / `deploy/nginx-quiz.conf` | Alternativa sem Docker: systemd + nginx + certbot |
| `DEPLOY.md` | Guia de deploy (GitHub + VPS): caminho Docker+Caddy e caminho systemd+nginx |

**Rotas:** `GET /` (player), `GET /host` (apresentador), `WS /ws/host`, `WS /ws/play`.
A comunicação é por mensagens JSON com campo `type`/`action` (ex.: `qr`, `start`, `next`,
`skip`, `reset`, `join`, `answer`; servidor responde `lobby`, `question`, `reveal`,
`game_over`, etc.).

**Admin de perguntas:** `GET /admin` (página, também acessível pelo botão ⚙ Perguntas no
cabeçalho do host), `GET/PUT /admin/api/questions` (JSON). Protegido por senha via header
`X-Admin-Password`, conferida com `secrets.compare_digest` contra `ADMIN_PASSWORD` (env,
padrão `Wand123`). O `PUT` passa por `validar_quiz()`, grava com `salvar_quiz()` e chama
`game.reload_quiz()` — que só recarrega o jogo ao vivo fora de partida
(`phase == "lobby"`); `reset()` também relê o disco. `validar_quiz()` impõe os limites
`MAX_LEN_PERGUNTA = 200` e `MAX_LEN_OPCAO = 100` caracteres (também aplicados via
`maxlength` no front).

**Mensagem `question` aos jogadores** inclui `pergunta` e `opcoes` (o celular exibe o
enunciado e o texto de cada resposta, não só as cores). **`game_over` ao host** envia o
`ranking` completo (não só top 12) — o pódio mostra top 3 + prévia top 8, e o botão
"Ver pontuação completa" abre um modal rolável com todos os jogadores.

## Pontos não óbvios (importante)

- **Estado em memória, sala global única.** Rode com **um único processo** uvicorn. Não
  use múltiplos workers sem um backend de estado compartilhado — quebraria o jogo.
- **QR Code gerado a partir da URL que o navegador do host envia** (`/ws/host`, ação
  `qr`). O front (`host.html`/`player.html`) monta a URL do WebSocket com
  `location.host` e escolhe `wss://` quando a página é https. Consequência: **abra a tela
  do apresentador pelo endereço final que os jogadores vão usar** — nunca por `localhost`,
  senão o QR aponta para um endereço inacessível aos celulares.
- **Pontuação** (apenas em acerto): `pontos = round(100 * (1 - (tempo_gasto / tempo_limite) / 2))`.
  Instantâneo = 100; no estouro = 50; erro/sem resposta = 0. Ranking é acumulado.
- **Perguntas:** 2 a 4 opções; `correta` é índice base 0; `tempo` opcional (usa
  `tempo_padrao`). Reinicie o servidor após editar `questions.json`.

## Implantação em rede

- **VPS / produção (acesso permanente):** veja `DEPLOY.md`. Roda **sem túnel** (IP/domínio
  próprios) atrás de Caddy (HTTPS automático) ou nginx+certbot. O front já usa
  `location.host` + `wss://` sob https, então funciona atrás de proxy sem alterações.
  **Sempre 1 processo/contêiner** (estado em memória).

- **Rede local comum:** todos no mesmo Wi-Fi; abra `/host` pelo IP da rede. O Firewall
  do Windows bloqueia a porta 8000 em redes com perfil **Público** — é preciso liberar a
  entrada (regra inbound TCP 8000, requer admin).
- **Rede corporativa / acesso pela internet:** costuma ter isolamento de clientes (AP
  isolation) que impede os aparelhos de se enxergarem. Solução padrão: **`python iniciar.py`**
  → URL pública https; abrir `/host` por essa URL faz o QR e os WebSockets (`wss://`)
  funcionarem de qualquer rede, inclusive dados móveis.
- **Rede que bloqueia a porta 7844 (ex.: `CFC-VISITANTE`) → erro 1033 do Cloudflare.** O
  cloudflared gera a URL mas nunca conecta à edge (porta 7844 filtrada). O `iniciar.py` em
  `TUNEL=auto` **detecta isso e usa o localhost.run via SSH** (sai pela 443/22) sem
  intervenção. Diagnóstico rápido: servidor local responde em `127.0.0.1:8000` mas a URL
  pública dá 1033/530, e a porta 7844 dá timeout enquanto a 443 sai normal.
- **DNS:** se o `curl`/`nslookup` local falhar (000/timeout) mas o túnel responder via IP
  forçado (`curl --resolve host:443:<ip>`), o problema é o DNS da rede local — navegadores
  (DoH) e celulares em dados móveis resolvem assim mesmo. Workaround p/ aparelhos: DNS
  `1.1.1.1`/`8.8.8.8`.

## Idioma

Código, comentários e textos de UI em **Português (BR)**, com acentuação correta.

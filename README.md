# Quiz CFC 🎯

Quiz animado no estilo **Kahoot!**, com as cores do **Conselho Federal de Contabilidade**
(azul-marinho `#003b71`, verde `#00995d` e dourado `#f2a900`).

- ✅ **100 pontos por pergunta** por acerto.
- ⏱️ Pontuação **reduzida pelo tempo de resposta** (resposta instantânea = 100 pts; no estouro do tempo = 50 pts; erro/sem resposta = 0).
- 🏅 **Ranking acumulado** reapresentado após cada pergunta; no **pódio**, botão para ver a **pontuação completa** de todos os jogadores.
- 👥 Suporta **até 500 jogadores simultâneos** (backend assíncrono FastAPI + WebSockets).
- ✨ Front-end **animado** — duas telas: apresentador (telão) e jogador (celular). No celular o jogador vê o **enunciado** e o **texto de cada resposta**, além das cores.
- 📱 **QR Code** na tela do apresentador — entrada por câmera, sem digitar endereço, funciona offline (gerado no servidor).
- 🛠️ **Tela de administração** (`/admin`, protegida por senha) para criar/editar perguntas sem reiniciar.

## Como rodar

### Modo evento (recomendado) — um comando, acesso pela internet

```bash
pip install -r requirements.txt
python iniciar.py
```

O `iniciar.py` sobe o servidor, abre um **túnel público** (cloudflared) e **abre a tela
do apresentador já na URL pública** — assim o QR Code aponta para o endereço certo e os
jogadores entram de **qualquer rede**, inclusive dados móveis (ideal para redes
corporativas com isolamento de clientes). Encerre tudo com **Ctrl+C**.

> Requer o cloudflared (`winget install --id Cloudflare.cloudflared`). Sem ele, o app
> sobe só em rede local. A URL `trycloudflare` é temporária e muda a cada execução —
> use sempre a que o lançador imprime no terminal.

### Modo rede local — só o servidor

```bash
python server.py
```

O servidor sobe em `http://0.0.0.0:8000`. Descubra o IP da máquina na rede local
(ex.: `ipconfig` no Windows → algo como `192.168.0.10`).

A porta e o host podem ser ajustados pelas variáveis de ambiente `PORT` e `HOST`
(padrão `HOST=0.0.0.0`, `PORT=8000`). Ex.: `PORT=8123 python server.py`.

| Quem | Onde abrir |
|------|-----------|
| **Apresentador** (telão/projetor) | `http://SEU_IP:8000/host` |
| **Jogadores** (celular) | `http://SEU_IP:8000/` |

> Todos precisam estar na **mesma rede**. A tela do apresentador mostra o QR Code e o endereço.

> ⚠️ **Abra a tela do apresentador pelo IP da rede** (ex.: `http://192.168.0.10:8000/host`),
> **não** por `localhost`. O QR Code aponta exatamente para o endereço que o navegador do
> apresentador está usando — se for `localhost`, os celulares não conseguirão acessar.

### Produção (VPS) — acesso permanente, com domínio/HTTPS

Para hospedar de forma fixa (sem túnel), há um setup completo de deploy e o guia
**[`DEPLOY.md`](DEPLOY.md)**. Resumo do caminho recomendado (Docker + Caddy):

```bash
git clone https://github.com/SEU_USUARIO/quiz-cfc.git && cd quiz-cfc
cp .env.example .env       # ajuste DOMAIN e ADMIN_PASSWORD
docker compose up -d --build
```

Com `DOMAIN=seudominio.com.br`, o Caddy emite o **certificado HTTPS automaticamente**
(e os WebSockets passam a usar `wss://`). Sem domínio, use `DOMAIN=:80` para servir em
HTTP no IP. Há também o caminho **systemd + nginx + certbot** documentado no `DEPLOY.md`.

## Fluxo

1. Abra a tela do **apresentador**. Ela exibe um **QR Code** + o endereço de acesso e os jogadores entrando.
2. Cada jogador **aponta a câmera para o QR** (ou digita o endereço), escolhe um **nome** e entra.
3. O apresentador clica **▶ Iniciar**.
4. A cada pergunta: contagem regressiva (timer destacado no canto superior direito),
   jogadores tocam na resposta. A pergunta é revelada quando o tempo acaba **ou** quando
   todos respondem.
5. O **ranking acumulado** aparece. Clique **Próxima ▶** até o **pódio** final 🏆 — onde o
   botão **📋 Ver pontuação completa** abre a lista de todos os jogadores.
6. **↺ Reiniciar** zera tudo para uma nova rodada.

## Personalizar as perguntas

### Pela tela de administração (recomendado)

Pelo botão **⚙ Perguntas** no cabeçalho da tela do apresentador (abre em nova aba), ou
direto em **`/admin`** (ex.: `http://SEU_IP:8000/admin`). Informe a senha. Lá dá para
adicionar, editar, remover e reordenar perguntas, marcar a opção correta e definir o
tempo — tudo salvo direto no `questions.json`, **sem reiniciar o servidor** (as mudanças
valem imediatamente se não houver partida em andamento; caso contrário, ao clicar em
Reiniciar).

A senha vem da variável de ambiente `ADMIN_PASSWORD` (padrão: `Wand123`). **Troque-a**
antes de expor o servidor publicamente:

```bash
ADMIN_PASSWORD="suaSenhaForte" python server.py
```

> ⚠️ Se você expôs o jogo por um túnel público (ngrok/cloudflared), o `/admin` também
> fica acessível pela internet — use uma senha forte.

### Editando o arquivo direto

Como alternativa, edite `questions.json`:

```json
{
  "titulo": "Meu Quiz",
  "tempo_padrao": 20,
  "perguntas": [
    {
      "pergunta": "Texto da pergunta?",
      "opcoes": ["A", "B", "C", "D"],
      "correta": 1,          // índice (começa em 0) da opção correta
      "tempo": 20            // segundos (opcional; usa tempo_padrao se omitido)
    }
  ]
}
```

Aceita de 2 a 4 opções por pergunta. Limites: **enunciado até 200 caracteres**, **cada
opção até 100**. Reinicie o servidor após editar o arquivo direto (a tela `/admin` recarrega
sozinha).

## Fórmula da pontuação

```
pontos = round(100 * (1 - (tempo_gasto / tempo_limite) / 2))   # apenas se acertou
```

## Escala para 500 pessoas

Um processo `uvicorn` sustenta com folga 500 conexões WebSocket. Para eventos maiores
ou mais robustez, rode atrás de um proxy (nginx) com afinidade de sessão. Como o jogo
mantém estado em memória (uma sala global), use **um único processo** (não use múltiplos
workers sem um backend de estado compartilhado).

## Teste automatizado

Com o servidor rodando na porta 8123 (`PORT=8123 python server.py`), execute
`python test_e2e.py` — ele simula apresentador + 3 jogadores e valida pontuação,
ranking e pódio.

## Arquitetura

```
iniciar.py          lançador modo evento (servidor + túnel cloudflared + abre /host)
server.py           backend FastAPI + WebSockets (estado do jogo, pontuação, admin)
questions.json      banco de perguntas
static/host.html    tela do apresentador (telão)
static/player.html  tela do jogador (celular)
static/admin.html   tela de administração de perguntas (protegida por senha)
static/logo-cfc.png logo do CFC usada nas telas
test_e2e.py         teste ponta-a-ponta
Dockerfile          imagem de produção (uvicorn, 1 processo)
docker-compose.yml  stack de deploy: app + Caddy (HTTPS automático)
Caddyfile           proxy reverso com TLS automático
.env.example        modelo de variáveis (DOMAIN, ADMIN_PASSWORD)
deploy/             alternativa sem Docker (systemd + nginx)
DEPLOY.md           guia de deploy (GitHub + VPS)
```

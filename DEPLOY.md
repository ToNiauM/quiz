# Deploy do Quiz CFC

Guia para publicar o Quiz CFC numa VPS (Ubuntu/Debian). Numa VPS o servidor tem
IP público próprio, então **não há túnel** (`iniciar.py`/cloudflared são só para
rodar em rede local/eventos) — a aplicação roda direto atrás de um proxy com HTTPS.

> **Importante:** o estado do jogo fica **em memória**, numa sala global única.
> Rode **sempre um único processo/contêiner**. Nunca use `--workers > 1` nem mais
> de uma réplica — isso quebraria a partida.

---

## 1. Subir para o GitHub

O repositório já vem com Git inicializado e o primeiro commit feito. Crie um
repositório vazio no GitHub e conecte:

```bash
git remote add origin https://github.com/SEU_USUARIO/quiz-cfc.git
git branch -M main
git push -u origin main
```

O `.env` (com a senha) **não** é enviado: está no `.gitignore`. Versione apenas o
`.env.example`.

---

## 2. Pré-requisitos na VPS

- Ubuntu/Debian com acesso `root`/`sudo`.
- Portas **80** e **443** liberadas no firewall do provedor e no `ufw`:
  ```bash
  sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
  ```
- **(Opcional, recomendado) Domínio:** crie um registro **A** apontando
  `quiz.seudominio.com.br` para o IP da VPS. Com domínio, o HTTPS é automático.

---

## 3. Caminho A — Docker + Caddy (recomendado)

HTTPS automático e menos passos. Instale o Docker (se ainda não tiver):

```bash
curl -fsSL https://get.docker.com | sh
```

Clone o projeto e configure o `.env`:

```bash
git clone https://github.com/SEU_USUARIO/quiz-cfc.git
cd quiz-cfc
cp .env.example .env
nano .env        # ajuste DOMAIN e ADMIN_PASSWORD
```

No `.env`:

- **Com domínio (HTTPS automático):**
  ```env
  DOMAIN=quiz.seudominio.com.br
  ADMIN_PASSWORD=uma-senha-forte
  ```
- **Só pelo IP (HTTP, sem cadeado):**
  ```env
  DOMAIN=:80
  ADMIN_PASSWORD=uma-senha-forte
  ```

Suba:

```bash
docker compose up -d --build
docker compose logs -f      # acompanhar (Ctrl+C sai do log, não derruba)
```

Pronto:
- **Apresentador:** `https://quiz.seudominio.com.br/host` (ou `http://IP/host`)
- **Jogadores:** `https://quiz.seudominio.com.br/`   (ou `http://IP/`)

Comandos úteis:

```bash
docker compose down              # parar
docker compose up -d --build     # aplicar atualização de código
docker compose restart app       # reiniciar só a aplicação
```

---

## 4. Caminho B — systemd + nginx (sem Docker)

Use se preferir rodar direto no sistema. Arquivos prontos em `deploy/`.

```bash
# usuário de serviço + código + venv
sudo adduser --system --group --home /opt/quiz-cfc quiz
sudo git clone https://github.com/SEU_USUARIO/quiz-cfc.git /opt/quiz-cfc/app
cd /opt/quiz-cfc/app
sudo python3 -m venv /opt/quiz-cfc/.venv
sudo /opt/quiz-cfc/.venv/bin/pip install -r requirements.txt
sudo chown -R quiz:quiz /opt/quiz-cfc

# serviço (edite a ADMIN_PASSWORD dentro do arquivo antes)
sudo cp deploy/quiz-cfc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quiz-cfc
journalctl -u quiz-cfc -f        # logs

# nginx como proxy (faz o WebSocket funcionar)
sudo apt install -y nginx
sudo cp deploy/nginx-quiz.conf /etc/nginx/sites-available/quiz-cfc
sudo ln -s /etc/nginx/sites-available/quiz-cfc /etc/nginx/sites-enabled/
sudo nano /etc/nginx/sites-available/quiz-cfc   # troque o server_name
sudo nginx -t && sudo systemctl reload nginx

# HTTPS (precisa de domínio apontando para a VPS)
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d quiz.seudominio.com.br
```

---

## 5. Depois de no ar

- **Abra a tela do apresentador pelo endereço público final** (domínio ou IP),
  nunca por `localhost`: o QR Code é gerado a partir da URL que o navegador do host
  informa. Abrindo pelo endereço certo, o QR e os WebSockets (`wss://`) funcionam
  nos celulares de qualquer rede.
- **Troque a senha do `/admin`** (`ADMIN_PASSWORD`) — o padrão `Wand123` não deve ir
  para produção.
- **Editar perguntas:** acesse `/admin` (botão ⚙ Perguntas no host) ou edite o
  `questions.json`. No Docker, o `questions.json` é um volume — as edições persistem
  entre reinícios.

## 6. Atualizar a aplicação

```bash
git pull
# Docker:
docker compose up -d --build
# systemd:
sudo /opt/quiz-cfc/.venv/bin/pip install -r requirements.txt
sudo systemctl restart quiz-cfc
```

## 7. Resolução de problemas

- **WebSocket não conecta / jogador trava no "conectando":** o proxy precisa
  encaminhar o `Upgrade`. Com Caddy é automático; com nginx, confira os blocos
  `Upgrade`/`Connection` em `deploy/nginx-quiz.conf`.
- **HTTPS não emite certificado:** o domínio precisa apontar para o IP da VPS e as
  portas 80/443 abertas antes de subir. Veja `docker compose logs caddy`.
- **Mudei perguntas e não refletiu:** durante uma partida o quiz só recarrega ao
  **Reiniciar**; fora de partida (lobby) aplica na hora.

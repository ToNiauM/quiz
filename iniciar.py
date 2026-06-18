"""
Lançador do Quiz CFC
====================

Faz tudo em um comando, toda vez que você inicia o app:

  1. Sobe o servidor (FastAPI/uvicorn).
  2. Abre um túnel público com o cloudflared (acesso de qualquer rede, até dados móveis).
  3. Captura a URL pública e **abre a tela do apresentador já nesse endereço**
     — assim o QR Code aponta para o lugar certo e os jogadores conseguem entrar.

Uso:
    python iniciar.py            # porta padrão 8000
    PORT=8123 python iniciar.py  # outra porta

Para encerrar: Ctrl+C (derruba servidor e túnel juntos).

Sem cloudflared instalado, o app sobe só em rede local (use o IP da máquina).
Instale com:  winget install --id Cloudflare.cloudflared
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# garante saída sem erro de encoding em consoles legados (cp1252)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[union-attr]
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8000"))
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# URL do túnel alternativo (localhost.run via SSH), usado quando a edge do
# Cloudflare (porta 7844) está bloqueada — caso típico de Wi-Fi corporativo.
SSH_URL_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
# Endereço:porta que o cloudflared precisa alcançar para manter o túnel vivo.
CF_EDGE = ("region1.v2.argotunnel.com", 7844)


def achar_cloudflared() -> str | None:
    """Localiza o executável do cloudflared (PATH ou instalação via winget)."""
    from shutil import which

    exe = which("cloudflared")
    if exe:
        return exe
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links/cloudflared.exe"
    return str(cand) if cand.exists() else None


def edge_cloudflare_acessivel() -> bool:
    """Testa se a porta 7844 da edge do Cloudflare sai pela rede atual.

    Redes corporativas (ex.: CFC-VISITANTE) costumam bloquear a 7844 — nesses
    casos o cloudflared gera a URL mas nunca conecta (erro 1033), então é melhor
    cair para o túnel SSH (localhost.run), que sai pela porta 443/22.
    """
    try:
        with socket.create_connection(CF_EDGE, timeout=4):
            return True
    except OSError:
        return False


def garantir_chave_ssh() -> bool:
    """Garante que exista uma chave SSH (localhost.run exige uma para o túnel)."""
    from shutil import which

    ssh_dir = Path.home() / ".ssh"
    if any((ssh_dir / nome).exists() for nome in ("id_ed25519", "id_rsa", "id_ecdsa")):
        return True
    keygen = which("ssh-keygen")
    if not keygen:
        return False
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [keygen, "-t", "ed25519", "-N", "", "-f", str(ssh_dir / "id_ed25519"), "-q"],
            check=True,
        )
        return True
    except Exception:
        return False


def abrir_tunel_ssh(port: int) -> tuple[subprocess.Popen | None, str | None]:
    """Sobe o túnel localhost.run via SSH e devolve (processo, url_publica).

    Funciona em redes que liberam só a 443/22 (onde o cloudflared falha).
    """
    from shutil import which

    print("> Abrindo túnel público (localhost.run via SSH)...")
    ssh = which("ssh")
    if not ssh:
        print("! ssh não encontrado — instale o OpenSSH Client do Windows.")
        return None, None
    if not garantir_chave_ssh():
        print("! Não foi possível criar/encontrar uma chave SSH (localhost.run exige uma).")
        return None, None
    tunnel = subprocess.Popen(
        [
            ssh, "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
            "-R", f"80:localhost:{port}", "localhost.run",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    url_publica = None
    fim = time.time() + 40
    for line in tunnel.stdout:  # type: ignore[union-attr]
        m = SSH_URL_RE.search(line)
        if m:
            url_publica = m.group(0)
            break
        if time.time() > fim:
            break
    threading.Thread(
        target=lambda: [None for _ in tunnel.stdout], daemon=True  # type: ignore[union-attr]
    ).start()
    return tunnel, url_publica


def porta_em_uso(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def esperar_porta(port: int, timeout: float = 20) -> bool:
    fim = time.time() + timeout
    while time.time() < fim:
        if porta_em_uso(port):
            return True
        time.sleep(0.4)
    return False


def abrir_tunel(cf: str, url_local: str) -> tuple[subprocess.Popen, str | None]:
    """Sobe o cloudflared e devolve (processo, url_publica)."""
    print("> Abrindo túnel público (cloudflared)...")
    tunnel = subprocess.Popen(
        [cf, "tunnel", "--url", url_local, "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    url_publica = None
    fim = time.time() + 30
    for line in tunnel.stdout:  # type: ignore[union-attr]
        m = URL_RE.search(line)
        if m:
            url_publica = m.group(0)
            break
        if time.time() > fim:
            break
    # continua drenando a saída para o processo não travar
    threading.Thread(
        target=lambda: [None for _ in tunnel.stdout], daemon=True  # type: ignore[union-attr]
    ).start()
    return tunnel, url_publica


def main() -> int:
    if porta_em_uso(PORT):
        print(
            f"x A porta {PORT} já está em uso. Encerre o servidor anterior antes de "
            f"iniciar (ou defina outra porta: PORT=8123 python iniciar.py)."
        )
        return 1

    # 1) servidor
    print(f"> Subindo o servidor na porta {PORT}...")
    env = {**os.environ, "PORT": str(PORT)}
    server = subprocess.Popen([sys.executable, str(BASE_DIR / "server.py")], env=env)
    if not esperar_porta(PORT):
        print("x O servidor não respondeu a tempo. Abortando.")
        server.terminate()
        return 1

    url_local = f"http://127.0.0.1:{PORT}"

    # 2) túnel público (opcional)
    # TUNEL=auto (padrão): usa cloudflared se a edge (7844) sair pela rede;
    # senão cai para localhost.run (SSH/443). Force com TUNEL=cloudflared|ssh|local.
    tunnel: subprocess.Popen | None = None
    url_publica: str | None = None
    modo = os.environ.get("TUNEL", "auto").lower()
    cf = achar_cloudflared()

    if modo in ("local", "none"):
        usar = "local"
    elif modo in ("cloudflared", "cf"):
        usar = "cloudflared"
    elif modo in ("ssh", "localhostrun", "lhr"):
        usar = "ssh"
    else:  # auto
        if cf and edge_cloudflare_acessivel():
            usar = "cloudflared"
        else:
            if cf:
                print("! Edge do Cloudflare (porta 7844) inacessível nesta rede — usando localhost.run.")
            else:
                print("! cloudflared não encontrado — usando localhost.run.")
            usar = "ssh"

    if usar == "cloudflared" and cf:
        tunnel, url_publica = abrir_tunel(cf, url_local)
    elif usar == "ssh":
        tunnel, url_publica = abrir_tunel_ssh(PORT)

    if usar != "local" and not url_publica:
        print("! Não consegui obter a URL pública do túnel; seguindo só em rede local.")

    destino = url_publica or url_local

    print("\n" + "=" * 56)
    print("  QUIZ CFC no ar!")
    print(f"  Apresentador : {destino}/host")
    print(f"  Jogadores    : {destino}/")
    if not url_publica:
        print("  (sem túnel: abra pelo IP da máquina na rede p/ os celulares)")
    print("=" * 56 + "\n")

    # 3) abre a tela do apresentador JÁ na URL pública (QR correto para os celulares)
    print("> Abrindo a tela do apresentador para os jogadores acessarem...")
    webbrowser.open(f"{destino}/host")

    # 4) mantém vivo até Ctrl+C
    try:
        server.wait()
    except KeyboardInterrupt:
        print("\n> Encerrando servidor e túnel...")
    finally:
        for proc in (tunnel, server):
            if proc and proc.poll() is None:
                proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

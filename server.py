"""
Quiz CFC — backend FastAPI + WebSockets
=======================================

Reproduz a dinâmica do Kahoot:
  * Tela do apresentador (host)  -> exibe perguntas, contagem regressiva e ranking.
  * Tela do jogador (player)     -> entra com apelido e responde pelo celular.

Pontuação
---------
  * Cada pergunta vale no máximo 100 pontos por acerto.
  * O valor cai conforme o tempo de resposta:
        pontos = round(100 * (1 - (tempo_gasto / tempo_limite) / 2))
    -> resposta instantânea  -> 100 pontos
    -> resposta no estouro    ->  50 pontos
    -> resposta errada / sem resposta -> 0 pontos
  * O ranking é acumulado e reapresentado após cada pergunta.

Capacidade
----------
  Servidor totalmente assíncrono (asyncio + WebSockets). Um único processo
  uvicorn sustenta com folga 500 conexões simultâneas. Para subir a barra,
  rode com mais workers atrás de um proxy com afinidade de sessão.

Execução
--------
  pip install -r requirements.txt
  python server.py
  Apresentador:  http://SEU_IP:8000/host
  Jogadores:     http://SEU_IP:8000/  (ou leiam o QR/PIN na tela do host)

  Porta e host são configuráveis pelas variáveis de ambiente PORT e HOST
  (padrão: HOST=0.0.0.0, PORT=8000). Ex.: PORT=8123 python server.py
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import qrcode
from fastapi import (
    Body,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
QUESTIONS_FILE = BASE_DIR / "questions.json"

MAX_PLAYERS = 500
PONTOS_MAXIMOS = 100  # por pergunta, por acerto

# senha da tela de administração (/admin). Configurável por variável de ambiente.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Wand123")


# --------------------------------------------------------------------------- #
# Carga das perguntas
# --------------------------------------------------------------------------- #
def carregar_quiz() -> dict:
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


MAX_LEN_PERGUNTA = 200   # caracteres no enunciado
MAX_LEN_OPCAO = 100      # caracteres por opção de resposta


class QuizInvalido(ValueError):
    """Quiz recebido pela tela de administração não passou na validação."""


def validar_quiz(dados: dict) -> dict:
    """Valida e normaliza um quiz vindo do /admin. Lança QuizInvalido se inválido."""
    if not isinstance(dados, dict):
        raise QuizInvalido("Formato inválido: esperado um objeto.")

    titulo = str(dados.get("titulo", "Quiz")).strip() or "Quiz"

    try:
        tempo_padrao = int(dados.get("tempo_padrao", 20))
    except (TypeError, ValueError):
        raise QuizInvalido("tempo_padrao deve ser um número inteiro.")
    if tempo_padrao <= 0:
        raise QuizInvalido("tempo_padrao deve ser maior que zero.")

    perguntas_in = dados.get("perguntas")
    if not isinstance(perguntas_in, list) or not perguntas_in:
        raise QuizInvalido("Inclua ao menos uma pergunta.")

    perguntas: list[dict] = []
    for i, q in enumerate(perguntas_in, start=1):
        if not isinstance(q, dict):
            raise QuizInvalido(f"Pergunta {i}: formato inválido.")
        texto = str(q.get("pergunta", "")).strip()
        if not texto:
            raise QuizInvalido(f"Pergunta {i}: o enunciado não pode ficar vazio.")
        if len(texto) > MAX_LEN_PERGUNTA:
            raise QuizInvalido(
                f"Pergunta {i}: o enunciado excede {MAX_LEN_PERGUNTA} caracteres."
            )

        opcoes_raw = q.get("opcoes", [])
        if not isinstance(opcoes_raw, list):
            raise QuizInvalido(f"Pergunta {i}: opções inválidas.")
        opcoes = [str(o).strip() for o in opcoes_raw if str(o).strip()]
        if not (2 <= len(opcoes) <= 4):
            raise QuizInvalido(f"Pergunta {i}: informe de 2 a 4 opções preenchidas.")
        for opcao in opcoes:
            if len(opcao) > MAX_LEN_OPCAO:
                raise QuizInvalido(
                    f"Pergunta {i}: uma opção excede {MAX_LEN_OPCAO} caracteres."
                )

        try:
            correta = int(q.get("correta", -1))
        except (TypeError, ValueError):
            raise QuizInvalido(f"Pergunta {i}: marque a opção correta.")
        if not (0 <= correta < len(opcoes)):
            raise QuizInvalido(f"Pergunta {i}: a opção correta é inválida.")

        nova = {"pergunta": texto, "opcoes": opcoes, "correta": correta}

        if q.get("tempo") not in (None, ""):
            try:
                tempo = int(q["tempo"])
            except (TypeError, ValueError):
                raise QuizInvalido(f"Pergunta {i}: tempo deve ser um número inteiro.")
            if tempo <= 0:
                raise QuizInvalido(f"Pergunta {i}: tempo deve ser maior que zero.")
            nova["tempo"] = tempo

        perguntas.append(nova)

    return {"titulo": titulo, "tempo_padrao": tempo_padrao, "perguntas": perguntas}


def salvar_quiz(dados: dict) -> None:
    """Persiste o quiz validado em questions.json (UTF-8, legível)."""
    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# QR Code -> SVG (cores do CFC, sem dependência de Pillow/internet)
# --------------------------------------------------------------------------- #
_QR_CACHE: dict[str, str] = {}


def gerar_qr_svg(url: str, cor: str = "#003b71", fundo: str = "#ffffff") -> str:
    """Gera um SVG escalável do QR Code apontando para `url`."""
    if url in _QR_CACHE:
        return _QR_CACHE[url]
    qr = qrcode.QRCode(
        border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    # um único <path> com todos os módulos pretos -> SVG compacto
    partes = []
    for y, linha in enumerate(matrix):
        for x, modulo in enumerate(linha):
            if modulo:
                partes.append(f"M{x} {y}h1v1h-1z")
    path = "".join(partes)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
        f'shape-rendering="crispEdges" width="100%" height="100%">'
        f'<rect width="{n}" height="{n}" fill="{fundo}"/>'
        f'<path d="{path}" fill="{cor}"/></svg>'
    )
    _QR_CACHE[url] = svg
    return svg


# --------------------------------------------------------------------------- #
# Modelo de jogador
# --------------------------------------------------------------------------- #
@dataclass
class Player:
    pid: str
    nickname: str
    ws: WebSocket
    score: int = 0
    streak: int = 0  # acertos seguidos
    # estado da pergunta corrente
    answer_index: Optional[int] = None
    answer_time: Optional[float] = None


# --------------------------------------------------------------------------- #
# Estado do jogo (uma sala global — suficiente para uma turma/evento)
# --------------------------------------------------------------------------- #
class Game:
    # fases: lobby -> question -> reveal -> ... -> finished
    def __init__(self) -> None:
        self.quiz = carregar_quiz()
        self.pin = f"{secrets.randbelow(900000) + 100000}"
        self.players: dict[str, Player] = {}
        self.hosts: set[WebSocket] = set()
        self.phase = "lobby"
        self.current = -1                  # índice da pergunta atual
        self.question_start: float = 0.0
        self.deadline: float = 0.0
        self._timer_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ---- utilidades de envio -------------------------------------------- #
    async def _safe_send(self, ws: WebSocket, payload: dict) -> bool:
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def broadcast_players(self, payload: dict) -> None:
        if not self.players:
            return
        await asyncio.gather(
            *(self._safe_send(p.ws, payload) for p in list(self.players.values())),
            return_exceptions=True,
        )

    async def broadcast_hosts(self, payload: dict) -> None:
        if not self.hosts:
            return
        await asyncio.gather(
            *(self._safe_send(ws, payload) for ws in list(self.hosts)),
            return_exceptions=True,
        )

    # ---- lobby ----------------------------------------------------------- #
    def lobby_payload(self) -> dict:
        nomes = [p.nickname for p in self.players.values()]
        return {
            "type": "lobby",
            "pin": self.pin,
            "titulo": self.quiz.get("titulo", "Quiz"),
            "count": len(self.players),
            "players": nomes[-60:],  # mostra os últimos que entraram
            "total_perguntas": len(self.quiz["perguntas"]),
        }

    async def push_lobby(self) -> None:
        await self.broadcast_hosts(self.lobby_payload())

    # ---- ranking --------------------------------------------------------- #
    def ranking(self) -> list[dict]:
        ordenado = sorted(
            self.players.values(), key=lambda p: p.score, reverse=True
        )
        return [
            {"rank": i + 1, "nickname": p.nickname, "score": p.score, "pid": p.pid}
            for i, p in enumerate(ordenado)
        ]

    # ---- ciclo de pergunta ---------------------------------------------- #
    async def start(self) -> None:
        async with self._lock:
            if self.phase not in ("lobby", "finished"):
                return
            if not self.players:
                await self.broadcast_hosts(
                    {"type": "info", "message": "Nenhum jogador conectado ainda."}
                )
                return
            for p in self.players.values():
                p.score = 0
                p.streak = 0
            self.current = -1
        await self.next_question()

    async def next_question(self) -> None:
        async with self._lock:
            self._cancel_timer()
            self.current += 1
            if self.current >= len(self.quiz["perguntas"]):
                await self._finish()
                return

            q = self.quiz["perguntas"][self.current]
            tempo = int(q.get("tempo", self.quiz.get("tempo_padrao", 20)))
            self.phase = "question"
            self.question_start = time.monotonic()
            self.deadline = self.question_start + tempo
            for p in self.players.values():
                p.answer_index = None
                p.answer_time = None

            base = {
                "index": self.current,
                "total": len(self.quiz["perguntas"]),
                "tempo": tempo,
                "pergunta": q["pergunta"],
            }
            # host e jogador recebem o texto das opções (jogador também lê no celular)
            await self.broadcast_hosts(
                {**base, "type": "question", "opcoes": q["opcoes"]}
            )
            await self.broadcast_players(
                {
                    **base,
                    "type": "question",
                    "opcoes": q["opcoes"],
                    "n_opcoes": len(q["opcoes"]),
                }
            )
            self._timer_task = asyncio.create_task(self._question_timer(tempo))

    async def _question_timer(self, tempo: int) -> None:
        try:
            await asyncio.sleep(tempo)
            await self.reveal()
        except asyncio.CancelledError:
            pass

    def _cancel_timer(self) -> None:
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    async def _maybe_reveal_early(self) -> None:
        # se todos responderam, revela imediatamente
        if self.phase != "question" or not self.players:
            return
        if all(p.answer_index is not None for p in self.players.values()):
            await self.reveal()

    async def reveal(self) -> None:
        async with self._lock:
            if self.phase != "question":
                return
            self._cancel_timer()
            self.phase = "reveal"
            q = self.quiz["perguntas"][self.current]
            correta = q["correta"]
            tempo = int(q.get("tempo", self.quiz.get("tempo_padrao", 20)))

            counts = [0] * len(q["opcoes"])
            acertos = 0
            for p in self.players.values():
                pontos = 0
                acertou = p.answer_index == correta
                if p.answer_index is not None:
                    counts[p.answer_index] += 1
                if acertou:
                    elapsed = max(0.0, (p.answer_time or self.deadline) - self.question_start)
                    ratio = min(1.0, elapsed / tempo) if tempo else 1.0
                    pontos = round(PONTOS_MAXIMOS * (1 - ratio / 2))
                    p.score += pontos
                    p.streak += 1
                    acertos += 1
                else:
                    p.streak = 0
                # feedback individual ao jogador
                await self._safe_send(
                    p.ws,
                    {
                        "type": "reveal",
                        "correta": correta,
                        "sua_resposta": p.answer_index,
                        "acertou": acertou,
                        "pontos": pontos,
                        "total": p.score,
                        "streak": p.streak,
                    },
                )

            ranking = self.ranking()
            # avisa a cada jogador sua posição
            for entry in ranking:
                p = self.players.get(entry["pid"])
                if p:
                    await self._safe_send(
                        p.ws,
                        {"type": "rank", "rank": entry["rank"], "total_jogadores": len(ranking)},
                    )

            # host recebe distribuição + ranking acumulado (top 12)
            await self.broadcast_hosts(
                {
                    "type": "reveal",
                    "index": self.current,
                    "correta": correta,
                    "opcoes": q["opcoes"],
                    "counts": counts,
                    "acertos": acertos,
                    "respostas": sum(counts),
                    "ranking": ranking[:12],
                    "ultima": self.current + 1 >= len(self.quiz["perguntas"]),
                }
            )

    async def _finish(self) -> None:
        self.phase = "finished"
        ranking = self.ranking()
        await self.broadcast_hosts(
            {"type": "game_over", "podium": ranking[:3], "ranking": ranking}
        )
        for entry in ranking:
            p = self.players.get(entry["pid"])
            if p:
                await self._safe_send(
                    p.ws,
                    {
                        "type": "game_over",
                        "rank": entry["rank"],
                        "score": entry["score"],
                        "total_jogadores": len(ranking),
                    },
                )

    async def reset(self) -> None:
        async with self._lock:
            self._cancel_timer()
            self.quiz = carregar_quiz()  # recarrega eventuais perguntas editadas
            self.phase = "lobby"
            self.current = -1
            for p in self.players.values():
                p.score = 0
                p.streak = 0
                p.answer_index = None
                p.answer_time = None
        await self.push_lobby()
        await self.broadcast_players({"type": "lobby"})

    async def reload_quiz(self) -> bool:
        """Recarrega o quiz do disco para o jogo ao vivo. Só age fora de partida.

        Retorna True se aplicou imediatamente; False se há uma partida em curso
        (nesse caso o arquivo já foi salvo e valerá no próximo Reiniciar)."""
        async with self._lock:
            if self.phase != "lobby":
                return False
            self.quiz = carregar_quiz()
        await self.push_lobby()
        return True

    # ---- entrada de jogadores ------------------------------------------- #
    async def add_player(self, nickname: str, ws: WebSocket) -> Optional[Player]:
        nickname = (nickname or "").strip()[:18] or "Jogador"
        if len(self.players) >= MAX_PLAYERS:
            return None
        # evita apelidos duplicados
        existentes = {p.nickname.lower() for p in self.players.values()}
        base, n = nickname, 2
        while nickname.lower() in existentes:
            nickname = f"{base} {n}"
            n += 1
        pid = secrets.token_hex(6)
        player = Player(pid=pid, nickname=nickname, ws=ws)
        self.players[pid] = player
        await self.push_lobby()
        return player

    async def remove_player(self, pid: str) -> None:
        if pid in self.players:
            del self.players[pid]
            await self.push_lobby()

    async def record_answer(self, pid: str, index: int) -> None:
        p = self.players.get(pid)
        if not p or self.phase != "question" or p.answer_index is not None:
            return
        q = self.quiz["perguntas"][self.current]
        if not (0 <= index < len(q["opcoes"])):
            return
        p.answer_index = index
        p.answer_time = time.monotonic()
        await self._safe_send(p.ws, {"type": "answer_ack", "index": index})
        respondidos = sum(1 for x in self.players.values() if x.answer_index is not None)
        await self.broadcast_hosts(
            {"type": "answers", "respostas": respondidos, "total": len(self.players)}
        )
        await self._maybe_reveal_early()


game = Game()
app = FastAPI(title="Quiz CFC")


# --------------------------------------------------------------------------- #
# Rotas HTTP
# --------------------------------------------------------------------------- #
@app.get("/")
async def player_page():
    return FileResponse(STATIC_DIR / "player.html")


@app.get("/host")
async def host_page():
    return FileResponse(STATIC_DIR / "host.html")


# --------------------------------------------------------------------------- #
# Administração de perguntas (/admin) — protegida por senha
# --------------------------------------------------------------------------- #
def _conferir_senha(x_admin_password: Optional[str]) -> None:
    enviada = x_admin_password or ""
    if not secrets.compare_digest(enviada, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Senha incorreta.")


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin/api/questions")
async def admin_get_questions(x_admin_password: Optional[str] = Header(default=None)):
    _conferir_senha(x_admin_password)
    return carregar_quiz()


@app.put("/admin/api/questions")
async def admin_put_questions(
    dados: dict = Body(...),
    x_admin_password: Optional[str] = Header(default=None),
):
    _conferir_senha(x_admin_password)
    try:
        quiz = validar_quiz(dados)
    except QuizInvalido as e:
        raise HTTPException(status_code=422, detail=str(e))
    salvar_quiz(quiz)
    aplicado = await game.reload_quiz()
    return JSONResponse(
        {
            "ok": True,
            "total_perguntas": len(quiz["perguntas"]),
            "aplicado": aplicado,
            "mensagem": (
                "Perguntas salvas e aplicadas ao jogo."
                if aplicado
                else "Perguntas salvas. Como há uma partida em andamento, "
                "as mudanças valerão ao clicar em Reiniciar."
            ),
        }
    )


# --------------------------------------------------------------------------- #
# WebSocket do apresentador
# --------------------------------------------------------------------------- #
@app.websocket("/ws/host")
async def ws_host(ws: WebSocket):
    await ws.accept()
    game.hosts.add(ws)
    await game._safe_send(ws, game.lobby_payload())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = msg.get("type")
            if action == "qr":
                # o navegador do host informa o endereço de entrada que está usando;
                # devolvemos o QR já renderizado em SVG.
                url = str(msg.get("url", "")).strip()
                if url:
                    await game._safe_send(
                        ws, {"type": "qr", "url": url, "svg": gerar_qr_svg(url)}
                    )
            elif action == "start":
                await game.start()
            elif action == "next":
                if game.phase == "reveal":
                    await game.next_question()
            elif action == "skip":
                if game.phase == "question":
                    await game.reveal()
            elif action == "reset":
                await game.reset()
    except WebSocketDisconnect:
        pass
    finally:
        game.hosts.discard(ws)


# --------------------------------------------------------------------------- #
# WebSocket do jogador
# --------------------------------------------------------------------------- #
@app.websocket("/ws/play")
async def ws_play(ws: WebSocket):
    await ws.accept()
    player: Optional[Player] = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = msg.get("type")

            if action == "join" and player is None:
                player = await game.add_player(msg.get("nickname", ""), ws)
                if player is None:
                    await game._safe_send(
                        ws,
                        {"type": "error", "message": "Sala cheia (limite de 500 jogadores)."},
                    )
                    await ws.close()
                    return
                await game._safe_send(
                    ws,
                    {
                        "type": "joined",
                        "pid": player.pid,
                        "nickname": player.nickname,
                        "titulo": game.quiz.get("titulo", "Quiz"),
                    },
                )
            elif action == "answer" and player is not None:
                await game.record_answer(player.pid, int(msg.get("index", -1)))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player is not None:
            await game.remove_player(player.pid)


# arquivos estáticos auxiliares (se houver)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        ws_max_queue=1024,
        log_level="info",
    )

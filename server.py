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
import math
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
    pid: str                       # também é o token de sessão (reconexão)
    nickname: str
    ws: Optional[WebSocket] = None
    connected: bool = True
    score: int = 0
    streak: int = 0  # acertos seguidos
    # estado da pergunta corrente
    answer_index: Optional[int] = None
    answer_time: Optional[float] = None
    # tarefa que remove o jogador se ele não reconectar dentro da carência
    _drop_task: Optional[asyncio.Task] = field(default=None, repr=False)


# tempo (s) que um jogador desconectado é mantido para permitir reconexão sem
# perder a pontuação. Celulares caem o tempo todo (tela bloqueada, troca de rede).
RECONNECT_GRACE = 90


# --------------------------------------------------------------------------- #
# Estado do jogo (uma sala global — suficiente para uma turma/evento)
# --------------------------------------------------------------------------- #
class Game:
    # fases: lobby -> starting -> question -> reveal -> ... -> finished
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
        # últimos payloads enviados ao host — reenviados quando o host reconecta
        self.last_reveal_payload: Optional[dict] = None
        self.last_game_over_payload: Optional[dict] = None

    # ---- utilidades de envio -------------------------------------------- #
    async def _safe_send(self, ws: Optional[WebSocket], payload: dict) -> bool:
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception:
            return False

    async def _send_jobs(self, jobs: list[tuple[Optional[WebSocket], dict]]) -> None:
        """Dispara vários envios concorrentemente (fora do lock)."""
        if jobs:
            await asyncio.gather(
                *(self._safe_send(ws, pl) for ws, pl in jobs),
                return_exceptions=True,
            )

    def _connected_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.connected]

    async def broadcast_players(self, payload: dict) -> None:
        targets = [p.ws for p in self.players.values() if p.connected and p.ws]
        if targets:
            await asyncio.gather(
                *(self._safe_send(ws, payload) for ws in targets),
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
        conectados = self._connected_players()
        nomes = [p.nickname for p in conectados]
        return {
            "type": "lobby",
            "pin": self.pin,
            "titulo": self.quiz.get("titulo", "Quiz"),
            "count": len(conectados),
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

    def _purge_disconnected(self) -> None:
        """Remove jogadores desconectados (chamar dentro do lock)."""
        for pid in [pid for pid, p in self.players.items() if not p.connected]:
            t = self.players[pid]._drop_task
            if t and not t.done():
                t.cancel()
            del self.players[pid]

    # ---- ciclo de pergunta ---------------------------------------------- #
    async def start(self) -> None:
        async with self._lock:
            if self.phase not in ("lobby", "finished"):
                return
            if not self._connected_players():
                await self.broadcast_hosts(
                    {"type": "info", "message": "Nenhum jogador conectado ainda."}
                )
                return
            # quem não está conectado não entra na nova partida
            self._purge_disconnected()
            for p in self.players.values():
                p.score = 0
                p.streak = 0
                p.answer_index = None
                p.answer_time = None
            self.current = -1
            # fase intermediária bloqueia start/next/skip concorrentes
            self.phase = "starting"
        await self.next_question()

    async def next_question(self) -> None:
        finish_host: Optional[dict] = None
        finish_jobs: list[tuple[Optional[WebSocket], dict]] = []
        q_host: Optional[dict] = None
        q_players: Optional[dict] = None
        timer_tempo = 0
        async with self._lock:
            if self.phase not in ("starting", "reveal"):
                return
            self._cancel_timer()
            self.current += 1
            if self.current >= len(self.quiz["perguntas"]):
                self.phase = "finished"
                ranking = self.ranking()
                finish_host = {
                    "type": "game_over",
                    "podium": ranking[:3],
                    "ranking": ranking,
                }
                self.last_game_over_payload = finish_host
                rankmap = {e["pid"]: e for e in ranking}
                finish_jobs = [
                    (
                        p.ws,
                        {
                            "type": "game_over",
                            "rank": rankmap[p.pid]["rank"],
                            "score": rankmap[p.pid]["score"],
                            "total_jogadores": len(ranking),
                        },
                    )
                    for p in self.players.values()
                    if p.connected and p.ws and p.pid in rankmap
                ]
            else:
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
                q_host = {**base, "type": "question", "opcoes": q["opcoes"]}
                q_players = {
                    **base,
                    "type": "question",
                    "opcoes": q["opcoes"],
                    "n_opcoes": len(q["opcoes"]),
                }
                timer_tempo = tempo

        # ---- envios fora do lock ----
        if finish_host is not None:
            await self._send_jobs(finish_jobs)
            await self.broadcast_hosts(finish_host)
            return
        await self.broadcast_hosts(q_host)
        await self.broadcast_players(q_players)
        self._timer_task = asyncio.create_task(self._question_timer(timer_tempo))

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

    async def reveal(self) -> None:
        jobs: list[tuple[Optional[WebSocket], dict]] = []
        host_payload: Optional[dict] = None
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
                if p.answer_index is not None and 0 <= p.answer_index < len(counts):
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
                # feedback individual ao jogador (enviado fora do lock)
                if p.connected and p.ws:
                    jobs.append(
                        (
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
                    )

            ranking = self.ranking()
            rankmap = {e["pid"]: e for e in ranking}
            for p in self.players.values():
                if p.connected and p.ws and p.pid in rankmap:
                    jobs.append(
                        (
                            p.ws,
                            {
                                "type": "rank",
                                "rank": rankmap[p.pid]["rank"],
                                "total_jogadores": len(ranking),
                            },
                        )
                    )

            host_payload = {
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
            self.last_reveal_payload = host_payload

        # ---- envios fora do lock (não bloqueia o jogo se um socket travar) ----
        await self._send_jobs(jobs)
        await self.broadcast_hosts(host_payload)

    async def reset(self) -> None:
        async with self._lock:
            self._cancel_timer()
            self.quiz = carregar_quiz()  # recarrega eventuais perguntas editadas
            self.phase = "lobby"
            self.current = -1
            self.last_reveal_payload = None
            self.last_game_over_payload = None
            self._purge_disconnected()
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

    # ---- snapshot de estado (reconexão) --------------------------------- #
    async def push_state_player(self, p: Player) -> None:
        """Envia ao jogador o estado atual da partida (usado ao (re)conectar)."""
        payload: Optional[dict] = None
        ack: Optional[dict] = None
        async with self._lock:
            ws = p.ws
            if self.phase == "question":
                q = self.quiz["perguntas"][self.current]
                rem = max(0, math.ceil(self.deadline - time.monotonic()))
                payload = {
                    "type": "question",
                    "index": self.current,
                    "total": len(self.quiz["perguntas"]),
                    "tempo": rem,
                    "pergunta": q["pergunta"],
                    "opcoes": q["opcoes"],
                    "n_opcoes": len(q["opcoes"]),
                }
                if p.answer_index is not None:
                    ack = {"type": "answer_ack", "index": p.answer_index}
            elif self.phase == "finished":
                ranking = self.ranking()
                entry = next((e for e in ranking if e["pid"] == p.pid), None)
                if entry:
                    payload = {
                        "type": "game_over",
                        "rank": entry["rank"],
                        "score": entry["score"],
                        "total_jogadores": len(ranking),
                    }
            else:  # lobby / starting / reveal -> tela de espera
                payload = {"type": "lobby"}
        if payload:
            await self._safe_send(ws, payload)
        if ack:
            await self._safe_send(ws, ack)

    async def send_host_state(self, ws: WebSocket) -> None:
        """Envia ao host o estado atual (usado ao (re)conectar)."""
        extra: list[dict] = []
        async with self._lock:
            lobby = self.lobby_payload()
            if self.phase == "question":
                q = self.quiz["perguntas"][self.current]
                rem = max(0, math.ceil(self.deadline - time.monotonic()))
                respondidos = sum(
                    1 for x in self.players.values() if x.answer_index is not None
                )
                extra = [
                    {
                        "type": "question",
                        "index": self.current,
                        "total": len(self.quiz["perguntas"]),
                        "tempo": rem,
                        "pergunta": q["pergunta"],
                        "opcoes": q["opcoes"],
                    },
                    {
                        "type": "answers",
                        "respostas": respondidos,
                        "total": len(self._connected_players()),
                    },
                ]
            elif self.phase == "reveal" and self.last_reveal_payload:
                extra = [self.last_reveal_payload]
            elif self.phase == "finished" and self.last_game_over_payload:
                extra = [self.last_game_over_payload]
        await self._safe_send(ws, lobby)
        for e in extra:
            await self._safe_send(ws, e)

    # ---- entrada / reconexão de jogadores ------------------------------- #
    async def connect_player(
        self, nickname: str, ws: WebSocket, pid: Optional[str] = None
    ) -> Optional[Player]:
        """Cria um jogador novo ou reanexa um já existente (reconexão por pid).

        Retorna o Player, ou None se a sala estiver cheia. Envia 'joined',
        o estado atual e atualiza o lobby."""
        old_ws: Optional[WebSocket] = None
        async with self._lock:
            existente = self.players.get(pid) if pid else None
            if existente is not None:
                # reconexão: mantém pontuação e estado
                if existente._drop_task and not existente._drop_task.done():
                    existente._drop_task.cancel()
                existente._drop_task = None
                old_ws = existente.ws if existente.ws is not ws else None
                existente.ws = ws
                existente.connected = True
                p = existente
            else:
                if len(self.players) >= MAX_PLAYERS:
                    return None
                nome = (nickname or "").strip()[:18] or "Jogador"
                existentes = {x.nickname.lower() for x in self.players.values()}
                base, n = nome, 2
                while nome.lower() in existentes:
                    nome = f"{base} {n}"
                    n += 1
                novo_pid = secrets.token_hex(6)
                p = Player(pid=novo_pid, nickname=nome, ws=ws)
                self.players[novo_pid] = p
            pid_final, nome_final = p.pid, p.nickname
            titulo = self.quiz.get("titulo", "Quiz")

        # encerra socket antigo (se reconectou em outra aba/conexão)
        if old_ws is not None:
            try:
                await old_ws.close()
            except Exception:
                pass
        await self._safe_send(
            ws,
            {
                "type": "joined",
                "pid": pid_final,
                "nickname": nome_final,
                "titulo": titulo,
            },
        )
        await self.push_state_player(p)
        await self.push_lobby()
        return p

    async def detach_player(self, pid: str, ws: WebSocket) -> None:
        """Marca o jogador como desconectado e agenda remoção após a carência.
        Mantém a pontuação para permitir reconexão."""
        async with self._lock:
            p = self.players.get(pid)
            if not p or p.ws is not ws:
                return  # já reanexado a uma conexão nova — não mexe
            p.connected = False
            p.ws = None
            if p._drop_task and not p._drop_task.done():
                p._drop_task.cancel()
            p._drop_task = asyncio.create_task(self._expire_player(pid))
        await self.push_lobby()

    async def _expire_player(self, pid: str) -> None:
        try:
            await asyncio.sleep(RECONNECT_GRACE)
        except asyncio.CancelledError:
            return
        async with self._lock:
            p = self.players.get(pid)
            if not p or p.connected:
                return
            # durante uma partida, mantém o jogador (pontuação) mesmo offline;
            # só remove de fato no lobby/fim
            if self.phase not in ("lobby", "finished"):
                return
            del self.players[pid]
        await self.push_lobby()

    async def record_answer(self, pid: str, index: int) -> None:
        should_reveal = False
        ack_ws: Optional[WebSocket] = None
        respondidos = total_conn = 0
        async with self._lock:
            p = self.players.get(pid)
            if not p or self.phase != "question" or p.answer_index is not None:
                return
            q = self.quiz["perguntas"][self.current]
            if not (0 <= index < len(q["opcoes"])):
                return
            p.answer_index = index
            p.answer_time = time.monotonic()
            ack_ws = p.ws
            respondidos = sum(
                1 for x in self.players.values() if x.answer_index is not None
            )
            conectados = self._connected_players()
            total_conn = len(conectados)
            # revela cedo só quando TODOS os conectados responderam
            should_reveal = bool(conectados) and all(
                x.answer_index is not None for x in conectados
            )

        await self._safe_send(ack_ws, {"type": "answer_ack", "index": index})
        await self.broadcast_hosts(
            {"type": "answers", "respostas": respondidos, "total": total_conn}
        )
        if should_reveal:
            await self.reveal()


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
    await game.send_host_state(ws)
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
                player = await game.connect_player(
                    msg.get("nickname", ""), ws, msg.get("pid")
                )
                if player is None:
                    await game._safe_send(
                        ws,
                        {"type": "error", "message": "Sala cheia (limite de 500 jogadores)."},
                    )
                    await ws.close()
                    return
            elif action == "answer" and player is not None:
                try:
                    index = int(msg.get("index", -1))
                except (TypeError, ValueError):
                    continue
                await game.record_answer(player.pid, index)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if player is not None:
            await game.detach_player(player.pid, ws)


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

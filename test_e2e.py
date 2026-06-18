"""Teste ponta-a-ponta: simula apresentador + 3 jogadores via WebSocket."""
import asyncio, json, websockets

URL = "ws://127.0.0.1:8123"

async def recv_until(ws, types, timeout=8):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if m["type"] in types:
            return m

async def main():
    host = await websockets.connect(f"{URL}/ws/host")
    lobby = await recv_until(host, {"lobby"})
    print("LOBBY pin =", lobby["pin"], "| perguntas =", lobby["total_perguntas"])

    # jogadores
    players = {}
    for nome in ["Ana", "Bruno", "Caio"]:
        ws = await websockets.connect(f"{URL}/ws/play")
        await ws.send(json.dumps({"type": "join", "nickname": nome}))
        j = await recv_until(ws, {"joined"})
        players[nome] = {"ws": ws, "pid": j["pid"]}
    await recv_until(host, {"lobby"})  # atualização de contagem
    print("Jogadores conectados.")

    await host.send(json.dumps({"type": "start"}))

    rounds = 0
    while True:
        q = await recv_until(host, {"question", "game_over"})
        if q["type"] == "game_over":
            print("\n=== PODIO ===")
            for p in q["podium"]:
                print(f"  {p['rank']}º {p['nickname']}: {p['score']} pts")
            break
        rounds += 1
        correta = server_correta(q["index"])
        print(f"\nPergunta {q['index']+1}: resposta certa idx={correta}")

        # Ana responde rápido e certo; Bruno devagar e certo; Caio errado
        await asyncio.sleep(0.2)
        await players["Ana"]["ws"].send(json.dumps({"type": "answer", "index": correta}))
        await asyncio.sleep(1.5)
        await players["Bruno"]["ws"].send(json.dumps({"type": "answer", "index": correta}))
        errada = (correta + 1) % 4
        await players["Caio"]["ws"].send(json.dumps({"type": "answer", "index": errada}))

        # host encerra a pergunta cedo
        await host.send(json.dumps({"type": "skip"}))
        rev = await recv_until(host, {"reveal"})
        print("  counts:", rev["counts"], "| ranking parcial:",
              [(r["nickname"], r["score"]) for r in rev["ranking"]])
        # pontos individuais
        for nome in players:
            fb = await recv_until(players[nome]["ws"], {"reveal"})
            print(f"    {nome}: acertou={fb['acertou']} pontos={fb['pontos']} total={fb['total']}")
        await host.send(json.dumps({"type": "next"}))

    assert rounds == lobby["total_perguntas"], "número de rodadas diverge"
    print("\nOK — jogo completou", rounds, "perguntas.")
    await host.close()
    for p in players.values():
        await p["ws"].close()

# carrega o gabarito direto do arquivo (igual ao servidor)
import json as _j
with open("questions.json", encoding="utf-8") as f:
    _Q = _j.load(f)["perguntas"]
def server_correta(i): return _Q[i]["correta"]

asyncio.run(main())

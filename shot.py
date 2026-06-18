"""Sobe jogadores fake e tira screenshot da tela do apresentador (lobby + ranking)."""
import asyncio, json, threading, time
import websockets
from playwright.sync_api import sync_playwright

BASE = "127.0.0.1:8130"
NOMES = ["Ana Paula", "Bruno", "Caio", "Daniela", "Eduardo", "Fernanda",
         "Gustavo", "Helena", "Igor", "Júlia", "Karen", "Lucas", "Marina",
         "Nelson", "Olívia", "Paulo", "Rafaela", "Sérgio", "Tatiane", "Vitor"]

players = []

async def join_all():
    for nome in NOMES:
        ws = await websockets.connect(f"ws://{BASE}/ws/play")
        await ws.send(json.dumps({"type": "join", "nickname": nome}))
        await ws.recv()  # joined
        players.append(ws)
    # mantém vivo
    await asyncio.sleep(60)

def run_players():
    asyncio.run(join_all())

# 1) conecta jogadores em thread separada
t = threading.Thread(target=run_players, daemon=True)
t.start()
time.sleep(3)  # deixa todos entrarem

# 2) screenshot do lobby
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=2)
    page.goto(f"http://{BASE}/host")
    page.wait_for_selector("#qrbox svg", timeout=8000)  # espera o QR renderizar
    page.wait_for_timeout(1200)
    page.screenshot(path="host_lobby.png")
    print("host_lobby.png salvo")
    browser.close()

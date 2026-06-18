# Quiz CFC — imagem de produção
# Estado do jogo é em memória: rode SEMPRE como um único processo/contêiner.
FROM python:3.13-slim

# Boas práticas de runtime Python em contêiner
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

# Instala dependências primeiro (melhor cache de camadas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY server.py .
COPY questions.json .
COPY static/ ./static/

EXPOSE 8000

# Single process (uvicorn). NÃO use --workers > 1: quebraria o estado em memória.
CMD ["sh", "-c", "uvicorn server:app --host ${HOST} --port ${PORT} --ws-max-queue 1024 --log-level info"]

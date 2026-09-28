# syntax=docker/dockerfile:1
# Image unique pour l'interface Streamlit et le worker : seule la commande change.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

RUN groupadd --system --gid 10001 bsm \
    && useradd --system --uid 10001 --gid bsm --create-home --home-dir /home/bsm bsm

COPY requirements.txt .
RUN pip install -r requirements.txt

# Code en lecture seule pour l'utilisateur applicatif ; seuls data/ et logs/
# lui appartiennent (les volumes nommes heritent de ces droits a la creation).
COPY . .
RUN mkdir -p data logs && chown bsm:bsm data logs

USER bsm

EXPOSE 8501

# Dans le conteneur, Streamlit ecoute sur toutes les interfaces ; Compose ne
# publie le port que sur 127.0.0.1 de l'hote.
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]

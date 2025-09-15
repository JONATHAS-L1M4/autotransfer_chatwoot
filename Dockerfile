# Etapa base
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Atualiza certificados (TLS) e dependências de sistema mínimas
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    update-ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Diretório de trabalho
WORKDIR /app

# Copia dependências
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copia código
COPY service.py chatwoot_balancer.py ./

# (Opcional, recomendado) roda como usuário não-root
RUN useradd -ms /bin/bash appuser
USER appuser

# Expõe porta do FastAPI
EXPOSE 80

# Comando de entrada

CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "80"]


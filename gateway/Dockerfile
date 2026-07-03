# Minimal image for the extensions gateway.
#
# Secrets (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, GH_TOKEN, ADMIN_GH_TOKEN,
# ADMIN_API_TOKEN, ...) are deliberately NOT referenced here. Railway injects
# them into the container's environment at RUN time, where main.py reads them
# via os.environ.get(). Because they never appear as ARG/ENV, they are never
# baked into an image layer — nothing to extract from the built image.
FROM python:3.13-slim

WORKDIR /app

# Install dependencies first so this layer caches unless requirements change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY . .

# Railway provides $PORT at runtime; default to 8080 for local runs.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]

FROM python:3.12-slim

WORKDIR /app

# Dependencies first so a code change does not invalidate the layer that
# installs them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The catalog is generated, not shipped as a fixture — the image builds its
# own substrate from a seed, so a clone reproduces the published numbers
# rather than trusting a checked-in blob.
RUN python -m gen.catalog   --out data/catalog.json --seed 20260811 \
 && python -m gen.questions --catalog data/catalog.json \
      --out-questions data/questions.jsonl --out-key data/key.json

ENV CATALOG_PATH=/app/data/catalog.json \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# No key is required to run. Without one the layer answers every question from
# records and reports `classifier: regex` — set GEMINI_API_KEY (or another
# provider's) to enable model-backed intent classification.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health',timeout=2).status==200 else 1)"

CMD ["python", "service.py"]

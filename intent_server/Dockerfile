FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the model into the image at build time (same idea as stt/Dockerfile
# baking in its whisper model) so the container needs no internet access at
# runtime and starts in ~1s instead of downloading ~90MB on first request.
# Cached under /root/.cache/fastembed -- outside /app, so it survives the
# docker-compose bind mount below.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')"
COPY server.py .
EXPOSE 8803
CMD ["python", "server.py"]

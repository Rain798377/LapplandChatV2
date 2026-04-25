FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install discord.py groq yt-dlp requests spotipy
CMD ["python", "LapplandV2.py"]
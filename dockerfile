FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install discord.py groq yt-dlp requests aiohttp pillow mutagen discord.py[voice] huggingface_hub httpx
RUN apt-get update && apt-get install -y ffmpeg
CMD ["python", "LapplandV2.py"]
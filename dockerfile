FROM python:3.11-slim
WORKDIR /app
COPY . .
# git is needed here: discord-ext-voice-recv is installed straight from GitHub main,
# since the PyPI release predates Discord's voice-encryption changes (see requirements.txt).
RUN apt-get update && apt-get install -y --no-install-recommends git ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install discord.py groq google-genai requests aiohttp pillow mutagen discord.py[voice] "discord-ext-voice-recv @ git+https://github.com/imayhaveborkedit/discord-ext-voice-recv.git@ac04ea7b0941112e83767cf1c1469b408fa06748" huggingface_hub httpx fonttools spotdl
RUN pip install -U --pre "yt-dlp[default]"
CMD ["python", "LapplandV2.py"]
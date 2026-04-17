FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install discord.py groq python-dotenv
CMD ["python", "discord_bot.py"]
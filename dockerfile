FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install discord.py groq python-dotenv requests
CMD ["python", "LapplandV2.py"]
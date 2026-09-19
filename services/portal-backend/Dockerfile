FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*
COPY services/portal-backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/portal-backend/ .
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

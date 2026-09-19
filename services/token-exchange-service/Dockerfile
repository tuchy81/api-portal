FROM python:3.12-slim
WORKDIR /app
COPY services/token-exchange-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/redis/ /app/infra/redis/
COPY services/token-exchange-service/ .
EXPOSE 8081
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]

FROM python:3.12-slim
WORKDIR /app
COPY services/citizen-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY infra/redis/ /app/infra/redis/
COPY services/citizen-gateway/ .
EXPOSE 9080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9080"]

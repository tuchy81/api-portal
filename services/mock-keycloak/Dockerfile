FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python certs/gen_keys.py
EXPOSE 8180
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8180"]

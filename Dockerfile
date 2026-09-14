# Cahoots web + A2A network, one container (live demo). The AgentCore Runtime deployment is separate
# (agentcore_app.py); this image is what judges open in a browser.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app AWS_REGION=us-east-1 TZ=America/Winnipeg
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cahoots ./cahoots
COPY web ./web
COPY data ./data
COPY docs ./docs
COPY VERSION ./VERSION
RUN mkdir -p work/users
EXPOSE 8080
CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY retail_edge ./retail_edge
COPY web ./web
COPY data ./data
COPY models ./models
# train at build time if no model was shipped (reproducible: fixed seed)
RUN [ -f models/LATEST ] || python -m retail_edge.train
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "retail_edge.service"]

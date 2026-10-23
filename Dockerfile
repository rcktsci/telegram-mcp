FROM python:3-alpine

WORKDIR /app

ENV PYTHONPATH=/app
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_TIMEOUT=120

RUN apk add --no-cache \
    libpq \
    curl \
    && rm -rf /var/cache/apk/*

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -r requirements.txt

COPY . .

CMD ["python", "main.py"]

ARG PYTHON_IMAGE=python:3.13.14-slim-trixie
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app.py app.js index.html leaflet.js leaflet.css emoji-data.json UNICODE-LICENSE.txt LEAFLET-LICENSE.txt ./
CMD ["python", "app.py"]

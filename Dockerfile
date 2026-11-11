FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY rules.yaml /defaults/rules.yaml

# first run: seed the editable rules file into the mounted config folder
CMD ["sh", "-c", "[ -f /config/rules.yaml ] || cp /defaults/rules.yaml /config/rules.yaml; exec python -m app.main"]

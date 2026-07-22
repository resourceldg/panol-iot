# Imagen del servidor de pañoles. La misma sirve para los dos procesos
# (API HTTP y puente MQTT): cambia el comando, no el contenido.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

# Las dependencias primero, en su propia capa: cambian mucho menos seguido
# que el código, así que el build queda cacheado en el caso normal.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuario sin privilegios. El contenedor no necesita root para nada, y si
# alguien llega hasta acá desde la subred IoT, mejor que no lo tenga.
RUN useradd --create-home panol && chown -R panol:panol /app
USER panol

CMD ["python", "-m", "gunicorn", "--chdir", "/app", "--bind", "0.0.0.0:18500", \
     "--workers", "2", "api.app:app"]

# Pañol IoT — Control de acceso y auditabilidad

Sistema IoT de control de acceso a un pañol: puerta con RFID + solenoide + reed + PIR (ESP32 #1) y,
más adelante, armarios de CPU con sensores IR (ESP32 #2). Un servidor concentra la **máquina de
estados de sesiones** y deriva alarmas/tickets a EMATP.

**Criterio de diseño:** profesional, robusto y **a prueba de fallos de manera sencilla**
(tolerante a cortes de suministro). Ver [docs/DISENO.md](docs/DISENO.md).

## Estructura

```
server/          Cerebro (Etapa 1, corre local)
  engine/        Máquina de estados PURA (sin transporte ni DB)
  api/           Shell HTTP (Flask) — curl-testeable
  db/            SQLite (6 tablas) + recuperación al reinicio
  adapters/      emisor_ematp.py (webhook aislado)
  tests/         Simulador de escenarios (docs/DISENO.md §5)
firmware/
  nodo_panol/    ESP32 #1 (migrado del prototipo Documents/Door)
  nodo_armarios/ ESP32 #2 (MCP23017 + IR) — etapa posterior
docs/            Especificación v1.0 + diseño
```

## Hoja de ruta

- **Etapa 1:** server local + validar la máquina de estados con el simulador (sin hardware).
- **Etapa 2:** Mosquitto + Node-RED en homelab, expuesto LAN + WLAN; firmware real de los nodos.

## Mapa de pines — nodo pañol (ESP32 #1, WROOM)

| Función | GPIO | Nota |
|---|---|---|
| RC522 SCK / MOSI / MISO / RST / SDA | 18 / 23 / 19 / 4 / 5 | SoftSPI, VCC 3.3 V exclusivo |
| Relé solenoide | 26 | Activo-LOW, pulso 800 ms, **pull-up externo + 1N4007** |
| Reed switch | 27 | Pull-up interno, ABIERTA = 1 |
| PIR HC-SR501 | 16 | Retrigger H, throttle 30 s |
| LED acceso / LED puerta | 2 / 33 | |
| Botón grabar tarjeta | 32 | Pull-down |

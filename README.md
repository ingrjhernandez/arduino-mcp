# arduino-mcp

Servidor MCP que expone un Arduino Nano 33 BLE Sense a Claude por puerto serie.
Traduce el protocolo de texto del firmware (comandos ASCII, uno por línea, a
115200 baudios) en tools tipados con unidades explícitas.

## Instalación

```bash
cd arduino-mcp
uv sync            # o: pip install -e .
```

Comprobar el enlace antes de conectar Claude:

```bash
python -m pytest tests/ -q          # 19 tests, no necesitan la placa
python -c "from arduino_mcp.transport import available_ports; print(available_ports())"
```

## Configuración

En Claude Desktop (`claude_desktop_config.json`) o Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "arduino": {
      "command": "uv",
      "args": ["--directory", "C:\\ruta\\a\\arduino-mcp", "run", "arduino-mcp"],
      "env": {
        "ARDUINO_MCP_PORT": "COM5"
      }
    }
  }
}
```

Variables de entorno, todas opcionales:

| Variable | Default | Para qué |
|---|---|---|
| `ARDUINO_MCP_PORT` | autodetecta | Puerto COM. Sin esto busca un VID de Arduino y, si hay uno solo, lo usa. |
| `ARDUINO_MCP_BAUD` | `115200` | Velocidad. No la cambies salvo que cambies el firmware. |
| `ARDUINO_MCP_TIMEOUT` | `0.8` | Timeout por comando, en segundos. |
| `ARDUINO_MCP_SETTLE` | `0.15` | Espera tras abrir el puerto antes de la primera escritura. |

## Tools

**Lecturas** (cada una consulta el sensor en el momento, sin caché)

| Tool | Devuelve |
|---|---|
| `read_acc` | `ax, ay, az` en g |
| `read_gyro` | `gx, gy, gz` en °/s |
| `read_mag` | `mx, my, mz` en µT |
| `read_imu` | los nueve ejes del mismo instante |
| `read_baro` | `pressure_kpa`, `temp_c` |
| `read_proximity` | `proximity` en counts |
| `read_color` | `r, g, b, c` |
| `read_gesture` | `UP`/`DOWN`/`LEFT`/`RIGHT`/`NONE`, bloquea hasta 1.5 s |
| `read_all` | todo; con `composed=true` emite comando por comando |

**Actuadores**: `set_led(on)`, `set_rgb("R"|"G"|"B"|"OFF")`

**Ventana de tiempo**: `sample(command, duration_s, interval_ms, include_samples)`
devuelve min, max, media, desvío y pico a pico por columna. Es lo que permite
responder "¿hubo un golpe?" o "¿se movió?", que una lectura puntual no puede.
Por defecto omite la serie cruda para no llenar el contexto.

**Diagnóstico**: `list_serial_ports`, `device_info` (manda `HELP`)

**Escape hatch**: `raw_command(command)` para comandos que agregues al firmware
sin tocar este servidor.

## Decisiones de diseño

**`NA` no es un error.** El firmware responde `NA` cuando el sensor no tuvo dato
listo en ~100 ms. El servidor devuelve `status: "not_ready"` con `value: null`.
Si lo tratara como excepción, Claude concluiría que la placa está rota cuando en
realidad solo el APDS no llegó a tiempo.

**Un comando por vez.** Un lock serializa el acceso al puerto: sin él, dos tools
concurrentes se cruzan las respuestas y cada uno lee la línea del otro.

**Validación de cabecera.** Si pedís `ACC` y llega `GYRO,...`, el servidor falla
en vez de devolver los números del sensor equivocado. Es la señal de que el
buffer quedó desfasado.

**Unidades en la respuesta.** Cada lectura viaja con su unidad, para que el
modelo no tenga que adivinar si son g o m/s².

**Puerto abierto por comando.** Se abre, se manda, se lee y se cierra. Así el
monitor serie de PlatformIO puede convivir con el servidor. La excepción es
`sample()`, que lo mantiene abierto durante toda la ventana.

## Gotchas de hardware

- **El puerto COM es exclusivo en Windows.** Si el monitor serie de PlatformIO
  está abierto, el servidor no puede abrir el puerto y viceversa. Mientras
  corre `sample()` el puerto está tomado.
- **Nunca abrir a 1200 baudios.** En el nRF52840 eso dispara el bootloader y la
  aplicación deja de correr. El transporte rechaza ese valor explícitamente.
- **DTR queda asertado a propósito.** Un firmware con `while (!Serial);` no
  escribe nada si se baja DTR.
- **Para flashear desde Claude** hay que soltar el puerto antes del `pio run -t
  upload` y reabrirlo después. Como el servidor abre por comando, alcanza con
  no tener un `sample()` corriendo.

## Ajustar a tu firmware

Si cambiás el orden o la cantidad de campos, el único lugar a tocar es el dict
`COLUMNS` en `arduino_mcp/server.py`. Si la cantidad no coincide, el servidor no
rompe: devuelve los campos crudos con un `warning`, así el desajuste se ve en el
chat en vez de fallar en silencio.

El layout de `ALL` depende de tu firmware y no viene declarado. Hasta que lo
agregues a `COLUMNS`, `read_all()` devuelve los campos crudos; `read_all(composed=true)`
funciona con nombres desde el primer día.

"""Servidor MCP para el Arduino Nano 33 BLE Sense sobre puerto serie.

Expone tools tipados (uno por sensor / actuador) mas raw_command como escape
hatch para comandos que agregues al firmware sin tocar este servidor.

Convenciones de salida:
  - Todo valor numerico viaja con su unidad explicita, para que el modelo no
    tenga que adivinar si son g, grados por segundo o microtesla.
  - 'NA' del firmware no es un error: se devuelve value=None con
    status="not_ready". Un sensor sin dato listo no es una placa rota.
  - ERR,... del firmware si es un error y se propaga como ToolError.
"""

from __future__ import annotations

import statistics
import time
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .transport import Config, LinkError, available_ports, session

mcp = FastMCP(
    name="arduino-nano33",
    instructions=(
        "Controla un Arduino Nano 33 BLE Sense por puerto serie. Cada lectura "
        "consulta el sensor en el momento (no hay cache ni streaming). Un "
        "status 'not_ready' significa que el sensor no tenia dato dentro de "
        "~100 ms, no que la placa falle: reintentar suele alcanzar. Para "
        "detectar eventos en el tiempo (golpes, movimiento, cambios de luz) "
        "usar sample() en vez de repetir lecturas puntuales."
    ),
)

CFG = Config.from_env()

# Timeout mas largo para GESTURE: el firmware espera hasta 1.5 s un gesto.
GESTURE_TIMEOUT_S = 2.2

# Nombres de columna por comando. Si cambias el orden de campos en el firmware,
# este es el unico lugar a tocar.
COLUMNS: dict[str, list[str]] = {
    "ACC": ["ax", "ay", "az"],
    "GYRO": ["gx", "gy", "gz"],
    "MAG": ["mx", "my", "mz"],
    "BARO": ["pressure_kpa", "temp_c"],
    "PROX": ["proximity"],
    "COLOR": ["r", "g", "b", "c"],
    "IMU": ["ax", "ay", "az", "gx", "gy", "gz", "mx", "my", "mz"],
}

UNITS: dict[str, str] = {
    "ax": "g", "ay": "g", "az": "g",
    "gx": "deg/s", "gy": "deg/s", "gz": "deg/s",
    "mx": "uT", "my": "uT", "mz": "uT",
    "pressure_kpa": "kPa",
    "temp_c": "C",
    "proximity": "counts",
    "r": "counts", "g": "counts", "b": "counts", "c": "counts",
}


# --------------------------------------------------------------------------
# Parseo
# --------------------------------------------------------------------------

def _split(line: str) -> tuple[str, list[str]]:
    parts = [p.strip() for p in line.split(",")]
    return parts[0].upper(), parts[1:]


def _check(line: str, expected_head: str) -> list[str]:
    """Valida la cabecera de la respuesta y devuelve los campos."""
    head, fields = _split(line)
    if head == "ERR":
        raise ToolError(f"La placa rechazo el comando: {line}")
    if head != expected_head:
        raise ToolError(
            f"Respuesta inesperada para {expected_head}: {line!r}. "
            "Puede haber quedado el buffer desfasado o el firmware cambio."
        )
    return fields


def _is_na(fields: list[str]) -> bool:
    return len(fields) == 1 and fields[0].upper() == "NA"


def _named(head: str, fields: list[str]) -> dict[str, Any]:
    """Convierte los campos posicionales en un dict con unidades."""
    names = COLUMNS.get(head, [])
    if len(fields) != len(names):
        # No rompemos: devolvemos lo crudo y avisamos, asi un cambio de
        # firmware se ve en el chat en vez de fallar en silencio.
        return {
            "raw_fields": fields,
            "warning": (
                f"Esperaba {len(names)} campos para {head} "
                f"({', '.join(names)}) y llegaron {len(fields)}. "
                "Revisa COLUMNS en server.py."
            ),
        }
    out: dict[str, Any] = {}
    for name, value in zip(names, fields):
        try:
            out[name] = float(value)
        except ValueError:
            out[name] = value
    out["units"] = {n: UNITS[n] for n in names if n in UNITS}
    return out


def _simple_read(command: str, timeout_s: float | None = None) -> dict[str, Any]:
    """Patron comun: abrir, mandar un comando, parsear, cerrar."""
    try:
        with session(CFG) as s:
            line = s.send(command, timeout_s)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    head, fields = _split(line)
    if head == "ERR":
        raise ToolError(f"La placa rechazo el comando: {line}")
    # Si la cabecera no coincide con el comando, el buffer quedo desfasado y
    # estamos leyendo la respuesta de otra consulta. Fallar es mas seguro que
    # devolver numeros del sensor equivocado.
    expected = command.split()[0].upper()
    if head != expected:
        raise ToolError(
            f"Respuesta inesperada para {expected}: {line!r}. "
            "El buffer serie quedo desfasado o el firmware cambio de formato."
        )
    if _is_na(fields):
        return {
            "status": "not_ready",
            "value": None,
            "detail": (
                f"{command} no tenia dato listo dentro de ~100 ms. "
                "Es una condicion normal del firmware, no una falla."
            ),
            "raw": line,
        }
    result = _named(head, fields)
    result["status"] = "ok"
    result["raw"] = line
    return result


# --------------------------------------------------------------------------
# Diagnostico y conexion
# --------------------------------------------------------------------------

@mcp.tool
def list_serial_ports() -> dict[str, Any]:
    """Lista los puertos serie del sistema e indica cuales parecen Arduino.

    Usalo cuando una lectura falle por puerto: dice que hay conectado y con
    que VID/PID, para elegir el valor de ARDUINO_MCP_PORT.
    """
    ports = available_ports()
    return {
        "configured_port": CFG.port,
        "baud": CFG.baud,
        "ports": ports,
        "count": len(ports),
    }


@mcp.tool
def device_info() -> dict[str, Any]:
    """Pregunta a la placa que comandos soporta (HELP) y devuelve la lista.

    Sirve para verificar que el enlace funciona y para descubrir comandos que
    este servidor todavia no expone como tool tipado.
    """
    try:
        with session(CFG) as s:
            lines = s.send_multiline("HELP", first_timeout_s=1.5, quiet_s=0.3)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc
    return {"status": "ok", "baud": CFG.baud, "help": lines}


# --------------------------------------------------------------------------
# Lecturas
# --------------------------------------------------------------------------

@mcp.tool
def read_acc() -> dict[str, Any]:
    """Lee el acelerometro. Devuelve ax, ay, az en g."""
    return _simple_read("ACC")


@mcp.tool
def read_gyro() -> dict[str, Any]:
    """Lee el giroscopo. Devuelve gx, gy, gz en grados por segundo."""
    return _simple_read("GYRO")


@mcp.tool
def read_mag() -> dict[str, Any]:
    """Lee el magnetometro. Devuelve mx, my, mz en microtesla."""
    return _simple_read("MAG")


@mcp.tool
def read_imu() -> dict[str, Any]:
    """Lee acelerometro, giroscopo y magnetometro en una sola consulta.

    Preferilo sobre tres llamadas separadas cuando necesites los tres ejes
    del mismo instante: las tres lecturas quedan sincronizadas.
    """
    return _simple_read("IMU")


@mcp.tool
def read_baro() -> dict[str, Any]:
    """Lee el barometro. Devuelve presion en kPa y temperatura en grados C."""
    return _simple_read("BARO")


@mcp.tool
def read_proximity() -> dict[str, Any]:
    """Lee el sensor de proximidad. Valor mas alto significa objeto mas cerca."""
    return _simple_read("PROX")


@mcp.tool
def read_color() -> dict[str, Any]:
    """Lee el sensor de color: componentes r, g, b y c (claridad total)."""
    return _simple_read("COLOR")


@mcp.tool
def read_gesture() -> dict[str, Any]:
    """Espera hasta 1.5 s un gesto sobre el sensor y devuelve su direccion.

    Resultado: UP, DOWN, LEFT, RIGHT o NONE. NONE significa que no hubo gesto
    en la ventana, no que el sensor falle. Esta llamada bloquea ~1.5 s, asi
    que no la uses en bucle sin avisar al usuario.
    """
    try:
        with session(CFG) as s:
            line = s.send("GESTURE", GESTURE_TIMEOUT_S)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    fields = _check(line, "GESTURE")
    gesture = fields[0].upper() if fields else "NONE"
    return {
        "status": "ok",
        "gesture": gesture,
        "detected": gesture not in ("NONE", "NA"),
        "raw": line,
    }


@mcp.tool
def read_all(composed: bool = False) -> dict[str, Any]:
    """Lee todos los sensores de una vez.

    Args:
        composed: si es False (por defecto) usa el comando ALL del firmware,
            que devuelve todo en una linea. Si es True, emite ACC, GYRO, MAG,
            BARO, PROX y COLOR por separado: mas lento, pero con parseo
            garantizado y un status por sensor. Usalo si ALL devuelve un
            formato que este servidor no reconoce.
    """
    if composed:
        readings = {
            "acc": _simple_read("ACC"),
            "gyro": _simple_read("GYRO"),
            "mag": _simple_read("MAG"),
            "baro": _simple_read("BARO"),
            "proximity": _simple_read("PROX"),
            "color": _simple_read("COLOR"),
        }
        return {"status": "ok", "mode": "composed", "sensors": readings}

    try:
        with session(CFG) as s:
            line = s.send("ALL", timeout_s=1.5)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    head, fields = _split(line)
    if head == "ERR":
        raise ToolError(f"La placa rechazo el comando: {line}")
    return {
        "status": "ok",
        "mode": "single_line",
        "raw": line,
        "fields": fields,
        "note": (
            "El layout de ALL depende de tu firmware y no esta declarado en "
            "COLUMNS. Si preferis campos con nombre, llama read_all con "
            "composed=True o agrega el layout a COLUMNS['ALL']."
        ),
    }


# --------------------------------------------------------------------------
# Actuadores
# --------------------------------------------------------------------------

@mcp.tool
def set_led(on: bool) -> dict[str, Any]:
    """Prende o apaga el LED integrado (D13) de la placa."""
    command = "LED ON" if on else "LED OFF"
    try:
        with session(CFG) as s:
            reply = s.send_optional(command)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    if reply and reply.upper().startswith("ERR"):
        raise ToolError(f"La placa rechazo el comando: {reply}")
    return {"status": "ok", "command": command, "reply": reply}


@mcp.tool
def set_rgb(color: Literal["R", "G", "B", "OFF"]) -> dict[str, Any]:
    """Controla el LED RGB de la placa: R, G, B o OFF.

    El firmware acepta un color primario por vez, no valores arbitrarios.
    """
    value = color.upper()
    if value not in ("R", "G", "B", "OFF"):
        raise ToolError(f"Color invalido: {color!r}. Usa R, G, B o OFF.")

    command = f"RGB {value}"
    try:
        with session(CFG) as s:
            reply = s.send_optional(command)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    if reply and reply.upper().startswith("ERR"):
        raise ToolError(f"La placa rechazo el comando: {reply}")
    return {"status": "ok", "command": command, "reply": reply}


# --------------------------------------------------------------------------
# Muestreo en ventana
# --------------------------------------------------------------------------

MAX_DURATION_S = 60.0
MAX_SAMPLES = 2000


@mcp.tool
def sample(
    command: Literal["ACC", "GYRO", "MAG", "IMU", "BARO", "PROX", "COLOR"] = "ACC",
    duration_s: float = 5.0,
    interval_ms: int = 100,
    include_samples: bool = False,
) -> dict[str, Any]:
    """Muestrea un sensor durante una ventana de tiempo y devuelve estadisticas.

    Esta es la herramienta para responder preguntas sobre el tiempo: si hubo
    un golpe, si la placa se movio, cuanto vario la luz. Una lectura puntual
    no puede contestar eso.

    A diferencia de las lecturas simples, mantiene el puerto abierto durante
    toda la ventana, asi que el monitor serie de PlatformIO no puede usarlo
    mientras corre.

    Args:
        command: sensor a muestrear.
        duration_s: duracion de la ventana, maximo 60 s.
        interval_ms: separacion entre muestras, minimo 20 ms.
        include_samples: si es True incluye la serie completa ademas de las
            estadisticas. Dejalo en False salvo que necesites ver cada punto:
            una serie larga ocupa mucho contexto.

    Returns:
        Estadisticas por columna (min, max, media, desvio, pico a pico), el
        conteo de muestras validas y las no listas, y opcionalmente la serie.
    """
    if duration_s <= 0 or duration_s > MAX_DURATION_S:
        raise ToolError(f"duration_s debe estar entre 0 y {MAX_DURATION_S} s.")
    if interval_ms < 20:
        raise ToolError("interval_ms minimo es 20 ms: la placa no sigue el ritmo.")

    head = command.upper()
    names = COLUMNS.get(head, [])
    interval_s = interval_ms / 1000.0

    samples: list[dict[str, Any]] = []
    not_ready = 0
    errors = 0
    started = time.monotonic()

    try:
        with session(CFG) as s:
            deadline = started + duration_s
            while time.monotonic() < deadline and len(samples) < MAX_SAMPLES:
                tick = time.monotonic()
                try:
                    line = s.send(head)
                except LinkError:
                    errors += 1
                    line = None

                if line is not None:
                    resp_head, fields = _split(line)
                    if resp_head == "ERR":
                        raise ToolError(f"La placa rechazo el comando: {line}")
                    if _is_na(fields):
                        not_ready += 1
                    elif len(fields) == len(names):
                        row = {"t_s": round(tick - started, 4)}
                        try:
                            for name, value in zip(names, fields):
                                row[name] = float(value)
                            samples.append(row)
                        except ValueError:
                            errors += 1
                    else:
                        errors += 1

                sleep_for = interval_s - (time.monotonic() - tick)
                if sleep_for > 0:
                    time.sleep(sleep_for)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    elapsed = time.monotonic() - started
    stats: dict[str, dict[str, float]] = {}
    for name in names:
        series = [row[name] for row in samples if name in row]
        if not series:
            continue
        stats[name] = {
            "min": round(min(series), 6),
            "max": round(max(series), 6),
            "mean": round(statistics.fmean(series), 6),
            "stdev": round(statistics.pstdev(series), 6) if len(series) > 1 else 0.0,
            "peak_to_peak": round(max(series) - min(series), 6),
            "unit": UNITS.get(name, ""),
        }

    result: dict[str, Any] = {
        "status": "ok",
        "command": head,
        "requested_duration_s": duration_s,
        "actual_duration_s": round(elapsed, 3),
        "interval_ms": interval_ms,
        "samples_valid": len(samples),
        "samples_not_ready": not_ready,
        "samples_failed": errors,
        "effective_rate_hz": round(len(samples) / elapsed, 2) if elapsed > 0 else 0.0,
        "columns": names,
        "stats": stats,
    }
    if include_samples:
        result["samples"] = samples
    return result


# --------------------------------------------------------------------------
# Escape hatch
# --------------------------------------------------------------------------

@mcp.tool
def raw_command(command: str, timeout_s: float = 1.0) -> dict[str, Any]:
    """Envia un comando arbitrario a la placa y devuelve la linea cruda.

    Para comandos que agregues al firmware y todavia no tengan tool propio.
    Si la placa no lo conoce responde ERR,UNKNOWN_COMMAND,<texto>.
    """
    text = command.strip()
    if not text:
        raise ToolError("El comando no puede estar vacio.")
    if "\n" in text or "\r" in text:
        raise ToolError("El comando debe ser una sola linea, sin saltos.")
    if timeout_s <= 0 or timeout_s > 10:
        raise ToolError("timeout_s debe estar entre 0 y 10 segundos.")

    try:
        with session(CFG) as s:
            line = s.send(text, timeout_s)
    except LinkError as exc:
        raise ToolError(str(exc)) from exc

    head, fields = _split(line)
    return {
        "status": "error" if head == "ERR" else "ok",
        "command": text,
        "raw": line,
        "head": head,
        "fields": fields,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

"""Transporte serie para el protocolo de texto del Arduino Nano 33 BLE Sense.

Protocolo: comandos ASCII, uno por linea terminada en '\\n', a 115200 baudios.
La placa responde una linea por comando (HELP responde varias).

Estrategia de puerto: se abre por comando y se cierra al terminar, para no
pelear el puerto COM con el monitor serie de PlatformIO. La unica excepcion es
sample(), que mantiene el puerto abierto durante toda la ventana de muestreo.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import serial
from serial.tools import list_ports

DEFAULT_BAUD = 115200

# Abrir el puerto a 1200 baudios en esta placa dispara el bootloader nRF52840
# y la aplicacion deja de correr. Nunca usar ese valor.
BOOTLOADER_BAUD = 1200

# VIDs de Arduino y derivados, usados para autodetectar el puerto.
ARDUINO_VIDS = {0x2341, 0x2A03, 0x1B4F, 0x239A}


class LinkError(RuntimeError):
    """Falla de transporte: puerto ausente, ocupado o sin respuesta."""


class DeviceError(RuntimeError):
    """La placa respondio ERR,... a un comando valido a nivel transporte."""


@dataclass(frozen=True)
class Config:
    port: str | None = None
    baud: int = DEFAULT_BAUD
    settle_s: float = 0.15
    default_timeout_s: float = 0.8

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            port=os.getenv("ARDUINO_MCP_PORT") or None,
            baud=int(os.getenv("ARDUINO_MCP_BAUD", DEFAULT_BAUD)),
            settle_s=float(os.getenv("ARDUINO_MCP_SETTLE", "0.15")),
            default_timeout_s=float(os.getenv("ARDUINO_MCP_TIMEOUT", "0.8")),
        )


# La placa atiende un comando por vez. Sin este lock, dos tools concurrentes
# se cruzan las respuestas y cada uno lee la linea del otro.
_PORT_LOCK = threading.Lock()


def available_ports() -> list[dict]:
    """Lista los puertos serie del sistema con su identificacion USB."""
    out = []
    for p in list_ports.comports():
        out.append(
            {
                "port": p.device,
                "description": p.description,
                "hwid": p.hwid,
                "vid": p.vid,
                "pid": p.pid,
                "looks_like_arduino": p.vid in ARDUINO_VIDS if p.vid else False,
            }
        )
    return out


def resolve_port(configured: str | None) -> str:
    """Devuelve el puerto a usar: el configurado, o uno autodetectado."""
    if configured:
        return configured

    ports = list_ports.comports()
    if not ports:
        raise LinkError(
            "No hay puertos serie disponibles. Conecta la placa o define "
            "ARDUINO_MCP_PORT."
        )

    arduinos = [p for p in ports if p.vid in ARDUINO_VIDS]
    if len(arduinos) == 1:
        return arduinos[0].device
    if len(arduinos) > 1:
        names = ", ".join(p.device for p in arduinos)
        raise LinkError(
            f"Hay varias placas Arduino conectadas ({names}). "
            "Define ARDUINO_MCP_PORT para elegir una."
        )
    if len(ports) == 1:
        return ports[0].device

    names = ", ".join(p.device for p in ports)
    raise LinkError(
        f"No pude identificar la placa entre los puertos disponibles ({names}). "
        "Define ARDUINO_MCP_PORT."
    )


class Session:
    """Un puerto abierto. Vive dentro de un unico tool call."""

    def __init__(self, ser: serial.Serial, cfg: Config):
        self._ser = ser
        self._cfg = cfg

    def send(self, command: str, timeout_s: float | None = None) -> str:
        """Envia un comando y devuelve la primera linea util de respuesta."""
        timeout = timeout_s or self._cfg.default_timeout_s
        cmd = command.strip()

        # Descartar lo que haya quedado en el buffer de una respuesta tardia
        # anterior: si no, se lee desfasado y todo el dialogo queda corrido.
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode("ascii", "ignore"))
        self._ser.flush()

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkError(
                    f"La placa no respondio a '{cmd}' en {timeout:.1f} s."
                )
            self._ser.timeout = remaining
            raw = self._ser.readline()
            if not raw:
                raise LinkError(
                    f"La placa no respondio a '{cmd}' en {timeout:.1f} s."
                )
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            # Algunos firmwares hacen eco del comando antes de responder.
            if line.upper() == cmd.upper():
                continue
            return line

    def send_optional(self, command: str, timeout_s: float = 0.4) -> str | None:
        """Como send(), pero devuelve None si no hay respuesta.

        Para comandos de actuacion (LED, RGB) cuya respuesta no esta
        especificada: si el firmware no contesta nada, no es un error.
        """
        try:
            return self.send(command, timeout_s)
        except LinkError:
            return None

    def send_multiline(
        self, command: str, first_timeout_s: float = 1.0, quiet_s: float = 0.25
    ) -> list[str]:
        """Envia un comando y junta lineas hasta que la placa se queda callada.

        Necesario para HELP, que responde varias lineas sin terminador.
        """
        cmd = command.strip()
        self._ser.reset_input_buffer()
        self._ser.write((cmd + "\n").encode("ascii", "ignore"))
        self._ser.flush()

        lines: list[str] = []
        self._ser.timeout = first_timeout_s
        while True:
            raw = self._ser.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if line and line.upper() != cmd.upper():
                lines.append(line)
            self._ser.timeout = quiet_s

        if not lines:
            raise LinkError(f"La placa no respondio a '{cmd}'.")
        return lines


@contextmanager
def session(cfg: Config, settle_s: float | None = None) -> Iterator[Session]:
    """Abre el puerto, cede una Session y cierra siempre al salir."""
    if cfg.baud == BOOTLOADER_BAUD:
        raise LinkError(
            "Abrir el puerto a 1200 baudios dispara el bootloader de la placa. "
            "Usa 115200."
        )

    port = resolve_port(cfg.port)

    with _PORT_LOCK:
        try:
            ser = serial.Serial(port=port, baudrate=cfg.baud, timeout=cfg.default_timeout_s)
        except serial.SerialException as exc:
            raise LinkError(
                f"No pude abrir {port}: {exc}. En Windows el puerto es exclusivo: "
                "cerra el monitor serie de PlatformIO si lo tenes abierto."
            ) from exc

        try:
            # Margen para que el CDC del nRF52840 quede listo tras la apertura.
            # DTR queda asertado a proposito: un firmware con 'while (!Serial)'
            # no escribe nada si lo bajamos.
            time.sleep(settle_s if settle_s is not None else cfg.settle_s)
            ser.reset_input_buffer()
            yield Session(ser, cfg)
        finally:
            try:
                ser.close()
            except Exception:
                pass

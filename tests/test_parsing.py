"""Tests de parseo y logica de tools, con un puerto serie simulado.

No requieren la placa: reemplazan transport.session por un doble que devuelve
lineas fijas. Sirven para verificar que un cambio en el firmware o en el
parseo no rompe nada antes de enchufar el hardware.
"""

from __future__ import annotations

import contextlib

import pytest

from arduino_mcp import server
from fastmcp.exceptions import ToolError


def fn(tool):
    """Devuelve la funcion Python detras de un tool de FastMCP."""
    return getattr(tool, "fn", tool)


class FakeSession:
    def __init__(self, responses):
        # responses: dict comando -> linea, o callable(cmd) -> linea
        self._responses = responses
        self.sent: list[str] = []

    def _resolve(self, command):
        self.sent.append(command)
        if callable(self._responses):
            return self._responses(command)
        if command not in self._responses:
            return f"ERR,UNKNOWN_COMMAND,{command}"
        return self._responses[command]

    def send(self, command, timeout_s=None):
        return self._resolve(command)

    def send_optional(self, command, timeout_s=0.4):
        return self._resolve(command)

    def send_multiline(self, command, first_timeout_s=1.0, quiet_s=0.25):
        value = self._resolve(command)
        return value if isinstance(value, list) else [value]


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(responses):
        sess = FakeSession(responses)
        holder["session"] = sess

        @contextlib.contextmanager
        def fake_session(cfg, settle_s=None):
            yield sess

        monkeypatch.setattr(server, "session", fake_session)
        return sess

    return install


# --- lecturas -------------------------------------------------------------

def test_acc_devuelve_campos_con_nombre_y_unidades(fake):
    fake({"ACC": "ACC,0.01,-0.02,0.98"})
    out = fn(server.read_acc)()
    assert out["status"] == "ok"
    assert out["ax"] == 0.01
    assert out["az"] == 0.98
    assert out["units"]["ax"] == "g"


def test_baro_mapea_presion_y_temperatura(fake):
    fake({"BARO": "BARO,101.3,24.5"})
    out = fn(server.read_baro)()
    assert out["pressure_kpa"] == 101.3
    assert out["temp_c"] == 24.5


def test_imu_mapea_los_nueve_ejes(fake):
    fake({"IMU": "IMU,0,0,1,1,2,3,10,20,30"})
    out = fn(server.read_imu)()
    assert out["az"] == 1.0
    assert out["gy"] == 2.0
    assert out["mz"] == 30.0


def test_na_no_es_error_sino_not_ready(fake):
    fake({"PROX": "PROX,NA"})
    out = fn(server.read_proximity)()
    assert out["status"] == "not_ready"
    assert out["value"] is None


def test_err_del_firmware_se_propaga_como_error(fake):
    fake({"COLOR": "ERR,UNKNOWN_COMMAND,COLOR"})
    with pytest.raises(ToolError):
        fn(server.read_color)()


def test_cabecera_inesperada_falla_en_vez_de_devolver_basura(fake):
    fake({"ACC": "GYRO,1,2,3"})
    with pytest.raises(ToolError):
        fn(server.read_acc)()


def test_conteo_de_campos_distinto_avisa_sin_romper(fake):
    fake({"ACC": "ACC,1,2"})
    out = fn(server.read_acc)()
    assert "warning" in out
    assert out["raw_fields"] == ["1", "2"]


# --- gestos ---------------------------------------------------------------

def test_gesture_detectado(fake):
    fake({"GESTURE": "GESTURE,LEFT"})
    out = fn(server.read_gesture)()
    assert out["gesture"] == "LEFT"
    assert out["detected"] is True


def test_gesture_none_no_es_deteccion(fake):
    fake({"GESTURE": "GESTURE,NONE"})
    out = fn(server.read_gesture)()
    assert out["detected"] is False


# --- actuadores -----------------------------------------------------------

def test_led_on_manda_el_comando_correcto(fake):
    sess = fake({"LED ON": "OK,LED,ON"})
    out = fn(server.set_led)(on=True)
    assert sess.sent == ["LED ON"]
    assert out["status"] == "ok"


def test_rgb_rechaza_color_invalido(fake):
    fake({})
    with pytest.raises(ToolError):
        fn(server.set_rgb)(color="PURPLE")


def test_rgb_acepta_off(fake):
    sess = fake({"RGB OFF": "OK,RGB,OFF"})
    fn(server.set_rgb)(color="OFF")
    assert sess.sent == ["RGB OFF"]


# --- muestreo -------------------------------------------------------------

def test_sample_calcula_estadisticas(fake):
    valores = iter(["ACC,0,0,1.0", "ACC,0,0,1.5", "ACC,0,0,0.5"])

    def responder(command):
        try:
            return next(valores)
        except StopIteration:
            return "ACC,0,0,1.0"

    fake(responder)
    out = fn(server.sample)(command="ACC", duration_s=0.15, interval_ms=20)
    assert out["samples_valid"] >= 3
    az = out["stats"]["az"]
    assert az["min"] <= 0.5
    assert az["max"] >= 1.5
    assert az["unit"] == "g"


def test_sample_cuenta_los_na_aparte(fake):
    fake({"PROX": "PROX,NA"})
    out = fn(server.sample)(command="PROX", duration_s=0.1, interval_ms=20)
    assert out["samples_valid"] == 0
    assert out["samples_not_ready"] > 0


def test_sample_rechaza_intervalo_demasiado_corto(fake):
    fake({})
    with pytest.raises(ToolError):
        fn(server.sample)(command="ACC", duration_s=1, interval_ms=5)


def test_sample_omite_la_serie_por_defecto(fake):
    fake({"ACC": "ACC,0,0,1"})
    out = fn(server.sample)(command="ACC", duration_s=0.1, interval_ms=20)
    assert "samples" not in out
    con_serie = fn(server.sample)(
        command="ACC", duration_s=0.1, interval_ms=20, include_samples=True
    )
    assert "samples" in con_serie


# --- escape hatch ---------------------------------------------------------

def test_raw_command_devuelve_la_linea_cruda(fake):
    fake({"WHOAMI": "WHOAMI,nano33"})
    out = fn(server.raw_command)(command="WHOAMI")
    assert out["raw"] == "WHOAMI,nano33"
    assert out["fields"] == ["nano33"]


def test_raw_command_marca_error_sin_lanzar(fake):
    fake({})
    out = fn(server.raw_command)(command="PEPE")
    assert out["status"] == "error"
    assert out["head"] == "ERR"


def test_raw_command_rechaza_multilinea(fake):
    fake({})
    with pytest.raises(ToolError):
        fn(server.raw_command)(command="ACC\nGYRO")

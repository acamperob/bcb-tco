#!/usr/bin/env python3
"""
Descarga y consolida las bases de cálculo del Tipo de Cambio Oficial (TCO)
que publica el Banco Central de Bolivia.

Fuente:
  https://www.bcb.gob.bo/bcb_tco_publico_detalle_historico.php   (lista de fechas)
  https://www.bcb.gob.bo/bcb_tco_publico_descargar_csv.php?desde=YYYY-MM-DD&hasta=YYYY-MM-DD

Metodología del TCO:
  - Cortes hasta el 24/09/2026: promedio ponderado por monto (RD 88/2026).
  - Cortes desde el 25/09/2026: mediana ponderada por monto (RD 142/2026).
  El script calcula ambos para todos los días y verifica contra el vigente.

Salidas (todas dentro de data/):
  raw/YYYY-MM-DD.csv   CSV original por fecha de corte, tal cual lo entrega el BCB
  operaciones.csv      una fila por (fecha_corte, banco, tipo_cambio)
  diario.csv           una fila por (fecha_corte, banco)
  total.csv            una fila por fecha de corte: TCO publicado, promedio, mediana, método
  verificacion.csv     TCO recalculado con el método vigente vs publicado
  tco.json             todo lo anterior, compacto, para el dashboard
  tco.db               SQLite con las mismas tablas

Solo usa la biblioteca estándar. Idempotente: cada corrida descarga únicamente
las fechas que aún no están en raw/ y reconstruye las tablas derivadas.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.bcb.gob.bo"
URL_LISTA = f"{BASE}/bcb_tco_publico_detalle_historico.php"
URL_CSV = f"{BASE}/bcb_tco_publico_descargar_csv.php?desde={{d}}&hasta={{d}}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Primer corte calculado con mediana ponderada (Resolución de Directorio BCB 142/2026)
CAMBIO_METODO = "2026-09-25"
METODOLOGIA = [
    {"desde": "2026-06-26", "hasta": "2026-09-24", "metodo": "promedio",
     "norma": "RD 88/2026", "descripcion": "Promedio ponderado por monto"},
    {"desde": CAMBIO_METODO, "hasta": None, "metodo": "mediana",
     "norma": "RD 142/2026", "descripcion": "Mediana ponderada por monto"},
]

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"


def metodo_de(fecha_corte: str) -> str:
    return "mediana" if fecha_corte >= CAMBIO_METODO else "promedio"


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #
def http_get(url: str, retries: int = 3) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept-Language": "es-BO,es;q=0.9"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"No se pudo descargar {url}: {last}")


def fechas_publicadas() -> list[str]:
    """Lee el atributo data-fechas del selector de fecha de la página del BCB."""
    html = http_get(URL_LISTA).decode("utf-8", errors="replace")
    m = re.search(r'data-fechas=(["\'])(.*?)\1', html, re.S)
    if not m:
        raise RuntimeError("No se encontró data-fechas en la página del BCB; ¿cambió el HTML?")
    fechas = json.loads(m.group(2).replace("&quot;", '"'))
    return sorted(f for f in fechas if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f))


def descargar_faltantes(fechas: list[str]) -> list[str]:
    RAW.mkdir(parents=True, exist_ok=True)
    nuevas = []
    for f in fechas:
        destino = RAW / f"{f}.csv"
        if destino.exists() and destino.stat().st_size > 500:
            continue
        texto = http_get(URL_CSV.format(d=f)).decode("utf-8", errors="replace")
        if "Fecha de corte" not in texto:
            print(f"  aviso: {f} devolvió un CSV sin datos, se omite", file=sys.stderr)
            continue
        destino.write_text(texto, encoding="utf-8")
        nuevas.append(f)
        print(f"  descargado {f}")
        time.sleep(1)
    return nuevas


# --------------------------------------------------------------------------- #
# Parseo (tolera el formato original y el de texto-fórmula de Excel, ="1.234")
# --------------------------------------------------------------------------- #
def limpiar(s: str) -> str:
    s = (s or "").strip()
    if s.startswith("="):
        s = s[1:]
    return s.strip().strip('"').strip()


def norm_banco(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", limpiar(nombre)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def num(s: str) -> float | None:
    """'1.234.567' -> 1234567 ; '11,5200' -> 11.52 ; '-' o '' -> None."""
    s = limpiar(s)
    if s in ("", "-"):
        return None
    return float(s.replace(".", "").replace(",", "."))


def vigencia_rango(s: str) -> tuple[str, str]:
    """'2026-09-26' -> (d, d) ; '2026-09-26 al 2026-09-28' -> (desde, hasta)."""
    fechas = re.findall(r"\d{4}-\d{2}-\d{2}", s or "")
    if not fechas:
        return "", ""
    return fechas[0], fechas[-1]


def leer(path: Path) -> tuple[list[str], list[list[str]]]:
    lineas = path.read_text(encoding="utf-8-sig").splitlines()
    idx = next(i for i, l in enumerate(lineas) if limpiar(l.split(";")[0]) == "Fecha de corte")
    header = next(csv.reader([lineas[idx]], delimiter=";"))
    filas = [next(csv.reader([l], delimiter=";")) for l in lineas[idx + 2:] if l.strip()]
    return header, filas


def columnas_bancos(header: list[str]) -> list[tuple[str, int]]:
    out, col = [], 3
    while col < len(header):
        nombre = limpiar(header[col])
        if nombre:
            out.append((norm_banco(nombre), col))
        col += 2
    return out


def parse_raw(path: Path) -> tuple[list[dict], dict[str, float | None]]:
    """Filas largas (fecha, vigencia, banco, tc, n_ops, monto) y fila TCO publicada."""
    header, filas = leer(path)
    bancos = columnas_bancos(header)
    ops, publicado = [], {}
    for campos in filas:
        if len(campos) < 4:
            continue
        fecha_corte = limpiar(campos[0])
        v_desde, v_hasta = vigencia_rango(campos[1])
        etiqueta = limpiar(campos[2])
        if etiqueta == "TCO":
            for banco, c in bancos:
                publicado[banco] = num(campos[c]) if c < len(campos) else None
            continue
        if etiqueta == "TOTAL":
            continue
        tc = num(etiqueta)
        for banco, c in bancos:
            if banco == "TOTAL BANCOS":
                continue
            n = num(campos[c]) if c < len(campos) else None
            monto = num(campos[c + 1]) if c + 1 < len(campos) else None
            if n is None and monto is None:
                continue
            ops.append({"fecha_corte": fecha_corte, "vigencia": v_desde, "vigencia_hasta": v_hasta,
                        "banco": banco, "tc": tc, "n_ops": int(n or 0), "monto_usd": monto or 0.0})
    return ops, publicado


# --------------------------------------------------------------------------- #
# Estadísticos
# --------------------------------------------------------------------------- #
def r2(x: float | None) -> float | None:
    return None if x is None else round(x + 1e-9, 2)


def promedio_pond(celdas: list[tuple[float, float]]) -> float | None:
    t = sum(m for _, m in celdas)
    return sum(tc * m for tc, m in celdas) / t if t else None


def mediana_pond(celdas: list[tuple[float, float]]) -> float | None:
    """Primer precio (orden ascendente) donde el monto acumulado alcanza la mitad del total."""
    t = sum(m for _, m in celdas)
    if not t:
        return None
    acum = 0.0
    for tc, m in sorted(celdas):
        acum += m
        if acum >= t / 2 - 1e-9:
            return tc
    return None


def estadistico(celdas: list[tuple[float, float]], metodo: str) -> float | None:
    return mediana_pond(celdas) if metodo == "mediana" else promedio_pond(celdas)


def colchones(celdas: list[tuple[float, float]], mediana: float) -> tuple[float, float]:
    """USD adicionales necesarios para mover la mediana un nivel abajo / arriba.
    Abajo: compras nuevas a cualquier precio < mediana. Arriba: a cualquier precio > mediana."""
    t = sum(m for _, m in celdas)
    debajo = sum(m for tc, m in celdas if tc < mediana)
    hasta = sum(m for tc, m in celdas if tc <= mediana)
    return max(t - 2 * debajo, 0.0), max(2 * hasta - t, 0.0)


# --------------------------------------------------------------------------- #
# Consolidación
# --------------------------------------------------------------------------- #
def consolidar() -> dict:
    ops: list[dict] = []
    total, diario, verificacion = [], [], []
    for path in sorted(RAW.glob("*.csv")):
        filas, publicado = parse_raw(path)
        if not filas:
            continue
        fecha = path.stem
        metodo = metodo_de(fecha)
        ops.extend(filas)

        celdas = [(r["tc"], r["monto_usd"]) for r in filas]
        prom, med = promedio_pond(celdas), mediana_pond(celdas)
        oficial_calc = r2(med if metodo == "mediana" else prom)
        pub = publicado.get("TOTAL BANCOS")
        abajo, arriba = colchones(celdas, med) if med is not None else (None, None)
        total.append({
            "fecha_corte": fecha, "vigencia": filas[0]["vigencia"], "vigencia_hasta": filas[0]["vigencia_hasta"],
            "metodo": metodo, "tco": pub if pub is not None else oficial_calc,
            "tco_promedio": r2(prom), "tco_mediana": r2(med),
            "n_ops": sum(r["n_ops"] for r in filas), "monto_usd": sum(r["monto_usd"] for r in filas),
            "colchon_baja_usd": round(abajo) if abajo is not None else None,
            "colchon_sube_usd": round(arriba) if arriba is not None else None,
        })
        verificacion.append({"fecha_corte": fecha, "metodo": metodo, "tco_publicado": pub,
                             "tco_recalculado": oficial_calc,
                             "ok": pub is not None and oficial_calc is not None and abs(pub - oficial_calc) < 0.006})

        tco_ref = pub if pub is not None else oficial_calc
        bancos = sorted({r["banco"] for r in filas})
        for b in bancos:
            propias = [(r["tc"], r["monto_usd"]) for r in filas if r["banco"] == b]
            resto = [(r["tc"], r["monto_usd"]) for r in filas if r["banco"] != b]
            sin_b = estadistico(resto, metodo)
            diario.append({
                "fecha_corte": fecha, "banco": b, "metodo": metodo,
                "n_ops": sum(r["n_ops"] for r in filas if r["banco"] == b),
                "monto_usd": sum(m for _, m in propias),
                "tc_promedio": r2(promedio_pond(propias)), "tc_mediana": r2(mediana_pond(propias)),
                "tc_publicado": publicado.get(b),
                "usd_bajo_tco": sum(m for tc, m in propias if tc_ref_cmp(tc, tco_ref) < 0),
                "usd_en_tco": sum(m for tc, m in propias if tc_ref_cmp(tc, tco_ref) == 0),
                "usd_sobre_tco": sum(m for tc, m in propias if tc_ref_cmp(tc, tco_ref) > 0),
                "tco_sin_banco": r2(sin_b),
            })

    return {
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fuente": URL_LISTA,
        "metodologia": METODOLOGIA,
        "operaciones": ops,
        "diario": diario,
        "total": total,
        "verificacion": verificacion,
    }


def tc_ref_cmp(tc: float, ref: float | None) -> int:
    if ref is None:
        return 0
    if abs(tc - ref) < 1e-9:
        return 0
    return -1 if tc < ref else 1


# --------------------------------------------------------------------------- #
# Escritura
# --------------------------------------------------------------------------- #
def escribir_csv(nombre: str, filas: list[dict]) -> None:
    if not filas:
        return
    with (DATA / nombre).open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        w.writeheader()
        w.writerows(filas)


def escribir_salidas(c: dict) -> None:
    DATA.mkdir(exist_ok=True)
    escribir_csv("operaciones.csv", c["operaciones"])
    escribir_csv("diario.csv", c["diario"])
    escribir_csv("total.csv", c["total"])
    escribir_csv("verificacion.csv", c["verificacion"])

    compacto = dict(c)
    # en el JSON las operaciones van como listas para reducir tamaño
    compacto["operaciones"] = [[r["fecha_corte"], r["banco"], r["tc"], r["n_ops"], round(r["monto_usd"], 2)]
                               for r in c["operaciones"]]
    compacto["operaciones_campos"] = ["fecha_corte", "banco", "tc", "n_ops", "monto_usd"]
    del compacto["diario"]  # el dashboard lo recalcula a partir de las operaciones
    (DATA / "tco.json").write_text(json.dumps(compacto, ensure_ascii=False, separators=(",", ":")),
                                   encoding="utf-8")

    db = DATA / "tco.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    for nombre in ("operaciones", "diario", "total", "verificacion"):
        filas = c[nombre]
        if not filas:
            continue
        cols = list(filas[0].keys())
        con.execute(f"CREATE TABLE {nombre} ({', '.join(cols)})")
        con.executemany(f"INSERT INTO {nombre} VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[k] for k in cols) for r in filas])
    con.execute("CREATE INDEX ix_ops ON operaciones (fecha_corte, banco)")
    con.commit()
    con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-descarga", action="store_true", help="solo reconstruye las tablas a partir de raw/")
    args = ap.parse_args()

    if not args.sin_descarga:
        print("Consultando fechas publicadas...")
        fechas = fechas_publicadas()
        print(f"  {len(fechas)} fechas de corte disponibles ({fechas[0]} a {fechas[-1]})")
        nuevas = descargar_faltantes(fechas)
        print(f"  {len(nuevas)} archivos nuevos")

    c = consolidar()
    escribir_salidas(c)
    malos = [v for v in c["verificacion"] if not v["ok"]]
    print(f"Consolidado: {len(c['total'])} días, {len(c['operaciones'])} filas de operaciones.")
    if malos:
        print("AVISO: el TCO recalculado no coincide con el publicado en:", file=sys.stderr)
        for v in malos:
            print(f"  {v}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

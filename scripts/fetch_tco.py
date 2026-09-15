#!/usr/bin/env python3
"""
Descarga y consolida las bases de cálculo del Tipo de Cambio Oficial (TCO)
que publica el Banco Central de Bolivia.

Fuente:
  https://www.bcb.gob.bo/bcb_tco_publico_detalle_historico.php   (lista de fechas)
  https://www.bcb.gob.bo/bcb_tco_publico_descargar_csv.php?desde=YYYY-MM-DD&hasta=YYYY-MM-DD

Salidas (todas dentro de data/):
  raw/YYYY-MM-DD.csv   CSV original por fecha de corte, tal cual lo entrega el BCB
  operaciones.csv      tabla larga: una fila por (fecha_corte, banco, tipo_cambio)
  diario.csv           una fila por (fecha_corte, banco) con monto, N° y TC ponderado
  tco.json             todo lo anterior, compacto, para el dashboard
  tco.db               SQLite con las mismas tablas, para consultas ad hoc

Solo usa la biblioteca estándar. Idempotente: cada corrida descarga únicamente
las fechas que aún no están en raw/ y reconstruye las tablas derivadas.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.request
from datetime import date, datetime
from pathlib import Path

BASE = "https://www.bcb.gob.bo"
URL_LISTA = f"{BASE}/bcb_tco_publico_detalle_historico.php"
URL_CSV = f"{BASE}/bcb_tco_publico_descargar_csv.php?desde={{d}}&hasta={{d}}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"


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
    raw = m.group(2).replace("&quot;", '"')
    fechas = json.loads(raw)
    return sorted(f for f in fechas if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f))


def descargar_faltantes(fechas: list[str]) -> list[str]:
    RAW.mkdir(parents=True, exist_ok=True)
    nuevas = []
    for f in fechas:
        destino = RAW / f"{f}.csv"
        if destino.exists() and destino.stat().st_size > 500:
            continue
        contenido = http_get(URL_CSV.format(d=f))
        texto = contenido.decode("utf-8", errors="replace")
        if "Fecha de corte" not in texto:
            print(f"  aviso: {f} devolvió un CSV sin datos, se omite", file=sys.stderr)
            continue
        destino.write_text(texto, encoding="utf-8")
        nuevas.append(f)
        print(f"  descargado {f}")
        time.sleep(1)
    return nuevas


# --------------------------------------------------------------------------- #
# Parseo
# --------------------------------------------------------------------------- #
def norm_banco(nombre: str) -> str:
    """Unifica variantes (BANCO DE CRÉDITO / BANCO DE CREDITO, etc.)."""
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


def num(s: str) -> float | None:
    """'1.234.567' -> 1234567 ; '11,5200' -> 11.52 ; '-' -> None."""
    s = (s or "").strip()
    if s in ("", "-"):
        return None
    return float(s.replace(".", "").replace(",", "."))


def parse_raw(path: Path) -> list[dict]:
    """Devuelve filas largas: fecha_corte, vigencia, banco, tc, n_ops, monto_usd."""
    filas = []
    lineas = path.read_text(encoding="utf-8").splitlines()
    # localizar encabezado
    idx = next(i for i, l in enumerate(lineas) if l.startswith('"Fecha de corte"'))
    header = next(csv.reader([lineas[idx]], delimiter=";"))
    bancos = []  # (nombre, col_n, col_monto)
    col = 3
    while col < len(header):
        nombre = header[col].strip()
        if nombre:
            bancos.append((norm_banco(nombre), col, col + 1))
        col += 2
    for linea in lineas[idx + 2:]:
        if not linea.strip():
            continue
        campos = next(csv.reader([linea], delimiter=";"))
        if len(campos) < 4:
            continue
        fecha_corte, vigencia, tc_txt = campos[0], campos[1], campos[2].strip()
        if tc_txt in ("TOTAL", "TCO"):
            continue  # se recalculan; la fila TCO se usa solo para verificar
        tc = num(tc_txt)
        for banco, cn, cm in bancos:
            if banco == "TOTAL BANCOS":
                continue
            n = num(campos[cn]) if cn < len(campos) else None
            monto = num(campos[cm]) if cm < len(campos) else None
            if n is None and monto is None:
                continue
            filas.append({
                "fecha_corte": fecha_corte,
                "vigencia": vigencia,
                "banco": banco,
                "tc": tc,
                "n_ops": int(n or 0),
                "monto_usd": monto or 0.0,
            })
    return filas


def tco_publicado(path: Path) -> dict[str, float | None]:
    """Fila 'TCO' del CSV: TC ponderado por banco y total, tal como lo publica el BCB."""
    lineas = path.read_text(encoding="utf-8").splitlines()
    idx = next(i for i, l in enumerate(lineas) if l.startswith('"Fecha de corte"'))
    header = next(csv.reader([lineas[idx]], delimiter=";"))
    out = {}
    for linea in lineas[idx + 2:]:
        campos = next(csv.reader([linea], delimiter=";")) if linea.strip() else []
        if len(campos) > 3 and campos[2].strip() == "TCO":
            col = 3
            while col < len(header):
                nombre = header[col].strip()
                if nombre:
                    out[norm_banco(nombre)] = num(campos[col]) if col < len(campos) else None
                col += 2
    return out


# --------------------------------------------------------------------------- #
# Consolidación
# --------------------------------------------------------------------------- #
def consolidar() -> dict:
    ops: list[dict] = []
    verificacion = []
    for path in sorted(RAW.glob("*.csv")):
        filas = parse_raw(path)
        ops.extend(filas)
        # verificación: TCO recalculado vs publicado
        tot_m = sum(r["monto_usd"] for r in filas)
        tot_v = sum(r["monto_usd"] * r["tc"] for r in filas)
        pub = tco_publicado(path).get("TOTAL BANCOS")
        rec = round(tot_v / tot_m, 2) if tot_m else None
        verificacion.append({"fecha_corte": path.stem, "tco_publicado": pub,
                             "tco_recalculado": rec,
                             "ok": (pub is not None and rec is not None and abs(pub - rec) < 0.006)})

    # diario por banco
    diario: dict[tuple, dict] = {}
    for r in ops:
        k = (r["fecha_corte"], r["banco"])
        d = diario.setdefault(k, {"fecha_corte": r["fecha_corte"], "vigencia": r["vigencia"],
                                  "banco": r["banco"], "n_ops": 0, "monto_usd": 0.0, "_v": 0.0})
        d["n_ops"] += r["n_ops"]
        d["monto_usd"] += r["monto_usd"]
        d["_v"] += r["monto_usd"] * r["tc"]
    for d in diario.values():
        d["tc_ponderado"] = round(d["_v"] / d["monto_usd"], 4) if d["monto_usd"] else None
        del d["_v"]

    # total diario
    total: dict[str, dict] = {}
    for d in diario.values():
        t = total.setdefault(d["fecha_corte"], {"fecha_corte": d["fecha_corte"], "vigencia": d["vigencia"],
                                                "n_ops": 0, "monto_usd": 0.0, "_v": 0.0})
        t["n_ops"] += d["n_ops"]
        t["monto_usd"] += d["monto_usd"]
        t["_v"] += (d["tc_ponderado"] or 0) * d["monto_usd"]
    for t in total.values():
        t["tco"] = round(t["_v"] / t["monto_usd"], 4) if t["monto_usd"] else None
        del t["_v"]

    return {
        "generado": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fuente": URL_LISTA,
        "operaciones": ops,
        "diario": sorted(diario.values(), key=lambda d: (d["fecha_corte"], d["banco"])),
        "total": sorted(total.values(), key=lambda t: t["fecha_corte"]),
        "verificacion": verificacion,
    }


def escribir_salidas(c: dict) -> None:
    DATA.mkdir(exist_ok=True)
    with (DATA / "operaciones.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fecha_corte", "vigencia", "banco", "tc", "n_ops", "monto_usd"])
        w.writeheader()
        w.writerows(c["operaciones"])
    with (DATA / "diario.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fecha_corte", "vigencia", "banco", "n_ops", "monto_usd", "tc_ponderado"])
        w.writeheader()
        w.writerows(c["diario"])
    with (DATA / "verificacion.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fecha_corte", "tco_publicado", "tco_recalculado", "ok"])
        w.writeheader()
        w.writerows(c["verificacion"])
    (DATA / "tco.json").write_text(json.dumps(c, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    db = DATA / "tco.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE operaciones (fecha_corte TEXT, vigencia TEXT, banco TEXT, tc REAL, n_ops INTEGER, monto_usd REAL);
        CREATE TABLE diario (fecha_corte TEXT, vigencia TEXT, banco TEXT, n_ops INTEGER, monto_usd REAL, tc_ponderado REAL);
        CREATE TABLE total (fecha_corte TEXT, vigencia TEXT, n_ops INTEGER, monto_usd REAL, tco REAL);
        CREATE INDEX ix_ops ON operaciones (fecha_corte, banco);
    """)
    con.executemany("INSERT INTO operaciones VALUES (?,?,?,?,?,?)",
                    [(r["fecha_corte"], r["vigencia"], r["banco"], r["tc"], r["n_ops"], r["monto_usd"]) for r in c["operaciones"]])
    con.executemany("INSERT INTO diario VALUES (?,?,?,?,?,?)",
                    [(r["fecha_corte"], r["vigencia"], r["banco"], r["n_ops"], r["monto_usd"], r["tc_ponderado"]) for r in c["diario"]])
    con.executemany("INSERT INTO total VALUES (?,?,?,?,?)",
                    [(r["fecha_corte"], r["vigencia"], r["n_ops"], r["monto_usd"], r["tco"]) for r in c["total"]])
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

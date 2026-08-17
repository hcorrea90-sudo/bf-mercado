"""
mercado.py — Comparador de precios BF vs Chileautos
Scraping BF + Chileautos → análisis estadístico IQR → informe HTML
"""

import re
import sys
import time
import json
import logging
import statistics
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

OUTPUT_DIR  = Path(__file__).parent.parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "index.html"
DATA_FILE   = OUTPUT_DIR / "data.json"

BF_URL  = "https://www.brunofritsch.cl/autos-usados"
CA_BASE = "https://www.chileautos.cl/vehiculos/usado-tipo"

DELAY = 1.2

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def parsear_precio(txt):
    m = re.findall(r"[\$\s]*([\d]{1,3}(?:[.,\s][\d]{3})+)", str(txt or ""))
    for g in m:
        limpio = re.sub(r"[^\d]", "", g)
        try:
            v = int(limpio)
            if 1_000_000 <= v <= 200_000_000:
                return v
        except:
            pass
    return None

def parsear_km(txt):
    m = re.search(r"([\d]{1,3}(?:[.,][\d]{3})*)\s*km", str(txt or ""), re.I)
    if m:
        try:
            v = int(re.sub(r"[^\d]", "", m.group(1)))
            if 0 < v < 999_999:
                return v
        except:
            pass
    return None

def extraer_ano(txt):
    m = re.search(r"\b(199\d|20[012]\d)\b", str(txt or ""))
    return int(m.group(1)) if m else None

def normalizar_marca(marca):
    """Normaliza marca para URL de Chileautos."""
    return (marca.lower()
            .replace(" ", "-")
            .replace("á","a").replace("é","e").replace("í","i")
            .replace("ó","o").replace("ú","u"))

def normalizar_modelo(modelo):
    """Normaliza modelo para URL de Chileautos."""
    # Extraer solo el nombre del modelo (sin versión/año)
    partes = modelo.split()
    # Tomar hasta 2-3 palabras que sean el modelo real
    modelo_base = []
    for p in partes:
        if re.match(r"^(19|20)\d{2}$", p):
            break
        if re.match(r"^\d+\.\d+", p):  # versión tipo "2.0"
            break
        modelo_base.append(p)
    resultado = "-".join(modelo_base).lower()
    resultado = re.sub(r"[^a-z0-9\-]", "", resultado)
    return resultado or modelo.split()[0].lower()

# ── Scraping BF ───────────────────────────────────────────────────────────────

def scrape_bf(page) -> list[dict]:
    """Scraping completo de BF igual que en la auditoría."""
    todos = []
    pagina = 1
    page_size = 100
    log.info(f"Scraping BF: {BF_URL}")

    while pagina <= 15:
        url = f"{BF_URL}?page={pagina}&pageSize={page_size}"
        page.goto(url, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector("#grid-mode-product-card", timeout=12000)
        except:
            pass
        time.sleep(1.5)

        datos = page.evaluate("""() => {
            const tarjetas = document.querySelectorAll('#grid-mode-product-card');
            const out = [];
            tarjetas.forEach(t => {
                const txt = t.innerText || '';
                let titulo = '', version = '';
                const parrafos = t.querySelectorAll('p');
                for (const p of parrafos) {
                    const text = p.innerText.trim();
                    if (/bono|incluye|^\\$/.test(text)) continue;
                    if (text.length < 3) continue;
                    titulo = text; break;
                }
                const skipSpan = /^(Gasolina|Bencina|Di.sel|H.brido|El.ctrico|Autom.tica|Mec.nica|Manual|GNC|GLP|.nico\\s*due.o|Pocos\\s*kil.metros|Pocos\\s*KM|SALE|Winter\\s*Sale|Vendido)$/i;
                for (const el of t.querySelectorAll('span')) {
                    const text = el.innerText.trim();
                    if (text.length < 4) continue;
                    if (/km$/i.test(text)) continue;
                    if (/^\\$/.test(text)) continue;
                    if (/bono|incluye|vendido/i.test(text)) continue;
                    if (skipSpan.test(text)) continue;
                    if (text === titulo) continue;
                    if (/\\d/.test(text)) { version = text; break; }
                }
                let precio = '', km = '', combustible = '', transmision = '';
                for (const el of t.querySelectorAll('p, span, div')) {
                    const text = el.innerText.trim();
                    if (/^(Gasolina|Bencina|Di.sel|H.brido|El.ctrico|GNC|GLP)$/i.test(text)) combustible = text;
                    if (/^\\d{1,3}\\.\\d{3}\\s*km$/i.test(text)) km = text;
                    if (/^(Autom.tica|Mec.nica|Manual)$/i.test(text)) transmision = text;
                }
                for (const el of t.querySelectorAll('p, span')) {
                    if (/^\\$[\\d\\.\\s]{6,}/.test(el.innerText.trim())) {
                        precio = el.innerText.trim(); break;
                    }
                }
                let url_auto = '';
                for (const a of t.querySelectorAll('a')) {
                    const href = a.getAttribute('href') || '';
                    if (href.length > 5) {
                        url_auto = href.startsWith('http') ? href : 'https://www.brunofritsch.cl' + href;
                        break;
                    }
                }
                out.push({ titulo, version, precio_txt: precio, combustible, km_txt: km, transmision, url_auto, txt_full: txt.substring(0,200) });
            });
            return out;
        }""")

        # Detectar total y paginación
        html    = page.content()
        soup    = BeautifulSoup(html, "lxml")
        total_m = re.search(r"(\d+)\s*autos", soup.get_text())
        total   = int(total_m.group(1)) if total_m else 0

        CHIPS = re.compile(r"\b(Pocos\s*Kil[oó]metros|[uú]nico\s*Due[nñ]o|SALE|WINTER\s*SALE|Pocos\s*KM|Nuevo\s*Ingreso|Destacado|Winter\s*Sale|Vendido)\b", re.I)

        for d in datos:
            precio = parsear_precio(d["precio_txt"] or d["txt_full"])
            km     = parsear_km(d["km_txt"]) or parsear_km(d["txt_full"])
            titulo = CHIPS.sub("", d["titulo"]).strip()
            ver    = CHIPS.sub("", d.get("version","") or "").strip()
            if ver and ver not in titulo:
                titulo = f"{titulo} {ver}".strip()
            titulo = re.sub(r"\s+", " ", titulo).strip()
            ano    = extraer_ano(titulo)
            marca  = titulo.split()[0].upper() if titulo else "DESCONOCIDA"

            # Extraer modelo base (sin versión)
            partes_titulo = titulo.split()
            modelo_partes = []
            skip = False
            for p in partes_titulo[1:]:  # skip marca
                if re.match(r"^(19|20)\d{2}$", p):
                    continue
                if re.match(r"^\d+\.\d+", p) or skip:
                    skip = True
                    continue
                modelo_partes.append(p)
            modelo = " ".join(modelo_partes[:3]).upper() if modelo_partes else ""

            todos.append({
                "titulo":      titulo,
                "marca":       marca,
                "modelo":      modelo,
                "ano":         ano,
                "km":          km,
                "precio":      precio,
                "combustible": d.get("combustible",""),
                "transmision": d.get("transmision",""),
                "url":         d.get("url_auto",""),
            })

        hay_sig = (pagina * page_size) < total if total else len(datos) >= page_size * 0.7
        log.info(f"  BF página {pagina:02d}: {len(datos)} autos (total: {total})")
        if not datos or not hay_sig:
            break
        pagina += 1
        time.sleep(DELAY)

    log.info(f"BF total: {len(todos)}")
    return todos

# ── Scraping Chileautos ───────────────────────────────────────────────────────

def scrape_chileautos_modelo(page, marca: str, modelo: str, ano: int) -> list[dict]:
    """Scraping de Chileautos para un modelo/año específico."""
    marca_url  = normalizar_marca(marca)
    modelo_url = normalizar_modelo(modelo)
    url = f"{CA_BASE}/{marca_url}/{modelo_url}/{ano}-ano/"

    try:
        resp = page.goto(url, wait_until="networkidle", timeout=40000)
        if not resp or resp.status >= 400:
            # Intentar URL alternativa sin año
            url2 = f"{CA_BASE}/{marca_url}/{modelo_url}/"
            resp2 = page.goto(url2, wait_until="networkidle", timeout=30000)
            if not resp2 or resp2.status >= 400:
                return []
        time.sleep(2.0)
    except Exception as e:
        log.debug(f"  CA error {marca} {modelo} {ano}: {e}")
        return []

    # Extraer via JS usando el patrón de precio CLP (clase-agnostic)
    datos = page.evaluate("""(ano) => {
        const out = [];

        // Chileautos usa clases generadas — buscamos por contenido
        // Estrategia: encontrar elementos con precio CLP y subir al contenedor
        const allEls = document.querySelectorAll('*');
        const precioEls = [...allEls].filter(el => {
            if (el.children.length > 0) return false;
            const txt = el.innerText || '';
            return /\\$\\s*\\d{2,3}[.,]\\d{3}/.test(txt) && txt.length < 30;
        });

        const procesados = new Set();
        precioEls.forEach(precioEl => {
            // Subir 5-8 niveles para encontrar el contenedor de la tarjeta
            let contenedor = precioEl;
            for (let i = 0; i < 8; i++) {
                if (!contenedor.parentElement) break;
                contenedor = contenedor.parentElement;
                const txt = contenedor.innerText || '';
                // El contenedor correcto tiene título, precio y km
                if (txt.includes('km') && txt.includes('$') && txt.length > 50 && txt.length < 600) {
                    break;
                }
            }

            const key = contenedor.className + contenedor.innerText.slice(0, 50);
            if (procesados.has(key)) return;
            procesados.add(key);

            const txt = contenedor.innerText || '';
            if (!txt.includes('km') || !txt.includes('$')) return;

            // Extraer líneas del texto
            const lineas = txt.split('\\n').map(l => l.trim()).filter(l => l.length > 1);

            // Título: línea con año y marca
            let titulo = '', version = '', precio_txt = '', km_txt = '';
            for (const linea of lineas) {
                if (/^\\d{4}\\s+[A-Z]/.test(linea) && !titulo) {
                    titulo = linea;
                } else if (/\\d+\\.\\d+.*\\b(4[Xx][24]|2[Ww][Dd]|[Aa][Ww][Dd]|[Mm][Tt]|[Cc][Vv][Tt]|[Aa][Tt]|[Ss][Dd][Nn]|[Hh][Bb])/.test(linea) && !version) {
                    version = linea;
                } else if (/^\\$[\\d.,\\s]+/.test(linea) && !precio_txt) {
                    precio_txt = linea;
                } else if (/\\d{1,3}[.,]\\d{3}\\s*km/i.test(linea) && !km_txt) {
                    km_txt = linea;
                }
            }

            // Filtrar por año
            if (titulo && ano && !titulo.includes(String(ano))) return;
            if (!precio_txt) return;

            out.push({ titulo, version, precio_txt, km_txt, txt: txt.slice(0, 300) });
        });

        return out;
    }""", ano)

    resultado = []
    for d in datos:
        precio = parsear_precio(d.get("precio_txt","") or d.get("txt",""))
        km     = parsear_km(d.get("km_txt","")) or parsear_km(d.get("txt",""))
        titulo = (d.get("titulo","") or "").strip()
        ver    = (d.get("version","") or "").strip()

        if not precio:
            continue

        titulo_completo = f"{titulo} {ver}".strip() if ver and ver not in titulo else titulo
        titulo_completo = re.sub(r"\s+", " ", titulo_completo).strip()

        resultado.append({
            "titulo":  titulo_completo,
            "version": ver,
            "precio":  precio,
            "km":      km,
            "ano":     extraer_ano(titulo_completo) or ano,
        })

    return resultado

# ── Estadística IQR ───────────────────────────────────────────────────────────

def calcular_stats_iqr(precios: list[int]) -> dict | None:
    """
    Aplica IQR para eliminar outliers y calcula estadísticas.
    Retorna None si la muestra es insuficiente.
    """
    if len(precios) < 3:
        return None

    precios_sorted = sorted(precios)
    n = len(precios_sorted)
    q1 = precios_sorted[n // 4]
    q3 = precios_sorted[(3 * n) // 4]
    iqr = q3 - q1

    # Filtro IQR: eliminar outliers fuera de [Q1 - 1.5*IQR, Q3 + 1.5*IQR]
    limite_inf = q1 - 1.5 * iqr
    limite_sup = q3 + 1.5 * iqr
    limpios = [p for p in precios_sorted if limite_inf <= p <= limite_sup]

    if len(limpios) < 3:
        limpios = precios_sorted  # Si quedan muy pocos, usar todos

    mediana   = statistics.median(limpios)
    promedio  = statistics.mean(limpios)
    n_orig    = len(precios)
    n_limpio  = len(limpios)
    outliers  = n_orig - n_limpio

    # Percentiles
    p25 = limpios[len(limpios) // 4]
    p75 = limpios[(3 * len(limpios)) // 4]

    # Nivel de confiabilidad
    if n_limpio >= 8:
        confiabilidad = "ALTA"
        confiabilidad_txt = f"✅ Muestra confiable ({n_limpio} unidades)"
    elif n_limpio >= 4:
        confiabilidad = "MEDIA"
        confiabilidad_txt = f"⚠️ Muestra referencial ({n_limpio} unidades)"
    else:
        confiabilidad = "BAJA"
        confiabilidad_txt = f"❌ Muestra insuficiente ({n_limpio} unidades)"

    return {
        "n_orig":       n_orig,
        "n_limpio":     n_limpio,
        "outliers":     outliers,
        "mediana":      int(mediana),
        "promedio":     int(promedio),
        "p25":          int(p25),
        "p75":          int(p75),
        "min":          min(limpios),
        "max":          max(limpios),
        "confiabilidad":     confiabilidad,
        "confiabilidad_txt": confiabilidad_txt,
    }

def posicion_mercado(precio_bf: int, stats: dict) -> dict:
    """Determina la posición de un auto BF vs el mercado."""
    p25, mediana, p75 = stats["p25"], stats["mediana"], stats["p75"]
    diff = precio_bf - mediana
    diff_pct = (diff / mediana * 100) if mediana else 0

    if precio_bf < p25:
        estado = "BAJO"
        color  = "#16a34a"
        emoji  = "🟢"
        texto  = "Bajo mercado"
    elif precio_bf > p75:
        estado = "ALTO"
        color  = "#dc2626"
        emoji  = "🔴"
        texto  = "Sobre mercado"
    else:
        estado = "OK"
        color  = "#2563eb"
        emoji  = "⚪"
        texto  = "En mercado"

    return {
        "estado":    estado,
        "color":     color,
        "emoji":     emoji,
        "texto":     texto,
        "diff":      diff,
        "diff_pct":  round(diff_pct, 1),
    }

# ── Match BF vs Chileautos ────────────────────────────────────────────────────

def normalizar_version_match(titulo: str) -> str:
    """Normaliza para matching fuzzy entre BF y Chileautos."""
    t = titulo.upper()
    t = re.sub(r"\b(19|20)\d{2}\b", "", t)
    t = re.sub(r"\b(AT|MT|CVT|DCT)\s*\d+P\b", lambda m: m.group(0)[:3], t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def match_score(ver_bf: str, ver_ca: str) -> int:
    """Score de similitud entre versiones (0-100)."""
    tokens_bf = set(normalizar_version_match(ver_bf).split())
    tokens_ca = set(normalizar_version_match(ver_ca).split())
    if not tokens_bf or not tokens_ca:
        return 0
    interseccion = tokens_bf & tokens_ca
    union        = tokens_bf | tokens_ca
    return int(len(interseccion) / len(union) * 100)

# ── Análisis principal ────────────────────────────────────────────────────────

def analizar(bf_autos: list[dict], ca_data: dict) -> list[dict]:
    """
    Para cada auto BF, busca su referencia en CA y calcula posición de mercado.
    """
    resultados = []

    for auto in bf_autos:
        if not auto["precio"] or not auto["ano"]:
            continue

        marca  = auto["marca"]
        modelo = auto["modelo"]
        ano    = auto["ano"]
        titulo = auto["titulo"]

        # Clave de búsqueda en CA
        clave = f"{marca}|{modelo}|{ano}"
        ca_unidades = ca_data.get(clave, [])

        if not ca_unidades:
            resultados.append({
                **auto,
                "ca_stats":   None,
                "ca_nivel":   "SIN_DATOS",
                "posicion":   None,
                "ca_version": "MODELO_AÑO",
            })
            continue

        # Intentar match por versión
        mejor_score = 0
        mejor_grupo = ca_unidades  # fallback: todas las unidades del modelo/año

        # Agrupar CA por versión
        grupos_ver: dict[str, list[int]] = defaultdict(list)
        for u in ca_unidades:
            ver_key = normalizar_version_match(u.get("version","") or u.get("titulo",""))
            grupos_ver[ver_key].append(u["precio"])

        # Buscar mejor match de versión
        ver_bf = normalizar_version_match(titulo)
        for ver_ca, precios_ver in grupos_ver.items():
            sc = match_score(ver_bf, ver_ca)
            if sc > mejor_score and sc >= 40:
                mejor_score = sc
                mejor_grupo = [{"precio": p} for p in precios_ver]

        precios = [u["precio"] if isinstance(u, dict) else u for u in mejor_grupo]
        if not precios:
            precios = [u["precio"] for u in ca_unidades]

        stats = calcular_stats_iqr(precios)
        if not stats:
            resultados.append({
                **auto,
                "ca_stats":   None,
                "ca_nivel":   "INSUFICIENTE",
                "posicion":   None,
                "ca_version": "MODELO_AÑO",
                "match_score": mejor_score,
            })
            continue

        pos = posicion_mercado(auto["precio"], stats)
        nivel_ver = "VERSION" if mejor_score >= 60 else "MODELO_AÑO"

        resultados.append({
            **auto,
            "ca_stats":    stats,
            "ca_nivel":    nivel_ver,
            "posicion":    pos,
            "match_score": mejor_score,
        })

    return resultados

# ── Generación HTML ───────────────────────────────────────────────────────────

def fp(n): return f"${n:,.0f}".replace(",", ".") if n else "—"
def fk(n): return f"{n:,.0f} km".replace(",", ".") if n else "—"

def generar_html(resultados: list[dict], fecha_gen: str, hora_gen: str) -> str:
    total_bf     = len([r for r in resultados if r["precio"]])
    con_datos    = [r for r in resultados if r.get("posicion")]
    sin_datos    = [r for r in resultados if not r.get("posicion")]
    sobre_mercado = [r for r in con_datos if r["posicion"]["estado"] == "ALTO"]
    bajo_mercado  = [r for r in con_datos if r["posicion"]["estado"] == "BAJO"]
    en_mercado    = [r for r in con_datos if r["posicion"]["estado"] == "OK"]

    # Ordenar: primero sobre mercado (más urgente)
    con_datos_sorted = sorted(con_datos, key=lambda r: (
        {"ALTO": 0, "BAJO": 1, "OK": 2}[r["posicion"]["estado"]],
        -abs(r["posicion"]["diff"])
    ))

    # Agrupar por marca
    por_marca: dict[str, list] = defaultdict(list)
    for r in con_datos_sorted:
        por_marca[r["marca"]].append(r)

    # ── Tarjetas por marca ────────────────────────────────────────────────────
    marcas_html = ""
    for marca in sorted(por_marca.keys()):
        autos = por_marca[marca]
        n_sobre  = sum(1 for a in autos if a["posicion"]["estado"] == "ALTO")
        n_bajo   = sum(1 for a in autos if a["posicion"]["estado"] == "BAJO")
        n_ok     = sum(1 for a in autos if a["posicion"]["estado"] == "OK")
        marca_id = f"marca_{marca.replace(' ','_').replace('-','_')}"

        badges = ""
        if n_sobre: badges += f'<span class="badge-alto">{n_sobre} sobre mercado</span>'
        if n_bajo:  badges += f'<span class="badge-bajo">{n_bajo} bajo mercado</span>'
        if n_ok:    badges += f'<span class="badge-ok">{n_ok} OK</span>'

        cards_html = ""
        for r in autos:
            pos    = r["posicion"]
            stats  = r["ca_stats"]
            conf   = stats["confiabilidad_txt"] if stats else "Sin datos"
            nivel  = "Por versión" if r.get("ca_nivel") == "VERSION" else "Por modelo/año"
            diff_txt = (f'+{fp(pos["diff"])}' if pos["diff"] >= 0 else f'-{fp(abs(pos["diff"]))}') + f' ({pos["diff_pct"]:+.1f}%)'
            link   = f'<a href="{r["url"]}" target="_blank" class="btn-ver">Ver en BF →</a>' if r.get("url") else ""

            # Barra de posición
            p25, med, p75 = stats["p25"], stats["mediana"], stats["p75"]
            precio_bf = r["precio"]
            rango = max(p75 - p25, 1)
            pos_rel = max(0, min(100, (precio_bf - p25) / rango * 100))

            cards_html += f"""
            <div class="auto-card estado-{pos['estado'].lower()}">
              <div class="auto-head">
                <div class="auto-info">
                  <div class="auto-titulo">{r['titulo']}</div>
                  <div class="auto-meta">{fk(r['km'])} · {r.get('combustible','')} · {r.get('transmision','')}</div>
                </div>
                <div class="auto-precio-block">
                  <div class="auto-precio-bf">{fp(precio_bf)}</div>
                  <div class="mercado-badge" style="background:{pos['color']}">{pos['emoji']} {pos['texto']}</div>
                </div>
              </div>
              <div class="mercado-detail">
                <div class="mercado-stats">
                  <div class="stat-item"><span class="stat-lbl">P25 Chileautos</span><span class="stat-val">{fp(p25)}</span></div>
                  <div class="stat-item highlight"><span class="stat-lbl">Mediana</span><span class="stat-val">{fp(med)}</span></div>
                  <div class="stat-item"><span class="stat-lbl">P75</span><span class="stat-val">{fp(p75)}</span></div>
                  <div class="stat-item"><span class="stat-lbl">Diferencia vs mediana</span><span class="stat-val" style="color:{pos['color']};font-weight:700">{diff_txt}</span></div>
                </div>
                <div class="posicion-barra">
                  <div class="barra-track">
                    <div class="barra-zona bajo" style="width:33%"></div>
                    <div class="barra-zona ok" style="width:34%"></div>
                    <div class="barra-zona alto" style="width:33%"></div>
                    <div class="barra-marker" style="left:{pos_rel:.0f}%"></div>
                  </div>
                  <div class="barra-labels"><span>P25</span><span>Mediana</span><span>P75</span></div>
                </div>
                <div class="conf-row">
                  <span class="conf-txt">{conf}</span>
                  <span class="nivel-txt">Comparación: {nivel}</span>
                  {link}
                </div>
              </div>
            </div>"""

        marcas_html += f"""
        <div class="marca-group">
          <button class="marca-btn" onclick="toggle('{marca_id}')">
            <span class="marca-name">{marca}</span>
            <span class="marca-badges">{badges}</span>
            <span class="marca-total">{len(autos)} unidades</span>
            <span class="chevron" id="ch_{marca_id}">▶</span>
          </button>
          <div class="marca-body" id="{marca_id}">{cards_html}</div>
        </div>"""

    # Sin datos
    sin_datos_html = ""
    sin_datos_por_marca: dict[str, list] = defaultdict(list)
    for r in sin_datos:
        sin_datos_por_marca[r["marca"]].append(r)

    for marca, autos in sorted(sin_datos_por_marca.items()):
        filas = "".join(
            f'<tr><td>{a["titulo"]}</td><td>{fk(a["km"])}</td><td>{fp(a["precio"])}</td><td style="color:#94a3b8;font-size:11px">Sin unidades en Chileautos</td></tr>'
            for a in autos
        )
        sin_datos_html += f'<div class="sin-datos-marca"><strong>{marca}</strong> ({len(autos)})<table>{filas}</table></div>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Comparador Mercado BF — {fecha_gen}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#f1f5f9;--card:#fff;--border:#e2e8f0;--text:#0f172a;--muted:#64748b;
      --alto:#dc2626;--bajo:#16a34a;--ok:#2563eb;--medio:#d97706}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:13px;line-height:1.5}}

/* HEADER */
.hdr{{background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);color:#fff;padding:24px 40px}}
.hdr h1{{font-size:18px;font-weight:800}}
.hdr .sub{{color:#93c5fd;font-size:11px;margin-top:3px}}
.hdr-meta{{font-size:11px;color:#cbd5e1;margin-top:10px;display:flex;gap:20px;flex-wrap:wrap}}
.hdr-meta strong{{color:#fff}}
.ubadge{{display:inline-flex;align-items:center;gap:5px;background:rgba(29,78,216,.3);border:1px solid rgba(99,179,237,.4);color:#93c5fd;border-radius:20px;padding:3px 10px;font-size:10px;margin-top:8px}}

/* RESUMEN EJECUTIVO */
.resumen{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;padding:16px 40px;background:#fff;border-bottom:2px solid var(--border)}}
.res-box{{border-radius:8px;padding:12px 16px;border-left:3px solid}}
.res-box.alto{{background:#fef2f2;border-color:var(--alto)}}
.res-box.bajo{{background:#f0fdf4;border-color:var(--bajo)}}
.res-box.ok{{background:#eff6ff;border-color:var(--ok)}}
.res-box.neutro{{background:#f8fafc;border-color:#94a3b8}}
.res-box .n{{font-size:28px;font-weight:900;line-height:1}}
.res-box.alto .n{{color:var(--alto)}}
.res-box.bajo .n{{color:var(--bajo)}}
.res-box.ok .n{{color:var(--ok)}}
.res-box.neutro .n{{color:#475569}}
.res-box .desc{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-top:4px}}
.res-box .sub{{font-size:9px;color:var(--muted);margin-top:2px}}

/* LEYENDA */
.leyenda{{background:#1e293b;padding:8px 40px;display:flex;gap:20px;flex-wrap:wrap;font-size:10px;align-items:center}}
.leyenda .lbl{{color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.5px}}
.leyenda span{{color:#94a3b8}}

/* CONTENT */
.content{{padding:24px 40px;max-width:1400px;margin:0 auto}}
.section{{margin-bottom:28px}}
.sec-hdr{{display:flex;align-items:center;gap:10px;margin-bottom:12px;padding-bottom:8px;border-bottom:2px solid var(--border)}}
.sec-hdr h2{{font-size:14px;font-weight:700}}
.sec-hdr .cnt{{margin-left:auto;background:#e2e8f0;color:#475569;border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600}}

/* MARCA ACCORDION */
.marca-group{{margin-bottom:6px;border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.marca-btn{{width:100%;display:flex;align-items:center;gap:10px;padding:10px 16px;background:#f8fafc;border:none;cursor:pointer;text-align:left}}
.marca-btn:hover{{background:#f1f5f9}}
.marca-name{{font-size:13px;font-weight:700;color:var(--text)}}
.marca-badges{{display:flex;gap:6px;flex-wrap:wrap}}
.marca-total{{margin-left:auto;font-size:11px;color:var(--muted)}}
.chevron{{font-size:10px;color:var(--muted);transition:transform .2s;margin-left:8px}}
.chevron.open{{transform:rotate(90deg)}}
.marca-body{{display:none;padding:10px 12px;background:#fff;display:flex;flex-direction:column;gap:8px}}
.marca-body.open{{display:flex}}

/* BADGES */
.badge-alto{{background:#fef2f2;color:var(--alto);border:1px solid #fca5a5;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700}}
.badge-bajo{{background:#f0fdf4;color:var(--bajo);border:1px solid #86efac;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700}}
.badge-ok{{background:#eff6ff;color:var(--ok);border:1px solid #93c5fd;border-radius:4px;padding:2px 7px;font-size:10px;font-weight:700}}

/* AUTO CARD */
.auto-card{{border:1px solid var(--border);border-radius:8px;padding:14px;background:var(--card)}}
.auto-card.estado-alto{{border-left:3px solid var(--alto)}}
.auto-card.estado-bajo{{border-left:3px solid var(--bajo)}}
.auto-card.estado-ok{{border-left:3px solid var(--ok)}}
.auto-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px}}
.auto-titulo{{font-size:13px;font-weight:700;color:var(--text)}}
.auto-meta{{font-size:11px;color:var(--muted);margin-top:3px}}
.auto-precio-bf{{font-size:18px;font-weight:800;color:var(--text);text-align:right}}
.mercado-badge{{display:inline-block;color:#fff;padding:3px 10px;border-radius:4px;font-size:11px;font-weight:700;margin-top:4px;text-align:center}}

/* DETALLE MERCADO */
.mercado-detail{{border-top:1px solid var(--border);padding-top:10px}}
.mercado-stats{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px}}
.stat-item{{text-align:center;min-width:80px}}
.stat-item.highlight{{background:#f0fdf4;border-radius:6px;padding:4px 10px}}
.stat-lbl{{display:block;font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}}
.stat-val{{display:block;font-size:13px;font-weight:700;color:var(--text);margin-top:2px}}

/* BARRA POSICIÓN */
.posicion-barra{{margin:8px 0}}
.barra-track{{position:relative;height:10px;border-radius:5px;overflow:hidden;display:flex;margin-bottom:4px}}
.barra-zona.bajo{{background:#dcfce7}}
.barra-zona.ok{{background:#dbeafe}}
.barra-zona.alto{{background:#fee2e2}}
.barra-marker{{position:absolute;top:-2px;width:14px;height:14px;background:#0f172a;border:2px solid #fff;border-radius:50%;transform:translateX(-50%);box-shadow:0 1px 4px rgba(0,0,0,.3)}}
.barra-labels{{display:flex;justify-content:space-between;font-size:9px;color:var(--muted)}}

/* CONF ROW */
.conf-row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px;font-size:11px}}
.conf-txt{{color:var(--muted)}}
.nivel-txt{{color:var(--muted);font-style:italic}}
.btn-ver{{display:inline-block;padding:3px 10px;background:#1d4ed8;color:#fff;border-radius:4px;font-size:10px;font-weight:600;text-decoration:none;margin-left:auto}}
.btn-ver:hover{{background:#1e40af}}

/* SIN DATOS */
.sin-datos-marca{{margin-bottom:12px}}
.sin-datos-marca strong{{display:block;margin-bottom:4px;font-size:12px}}
.sin-datos-marca table{{width:100%;border-collapse:collapse;font-size:11px}}
.sin-datos-marca td{{padding:4px 8px;border-bottom:1px solid var(--border)}}

.empty-state{{text-align:center;color:var(--muted);padding:24px;background:#f8fafc;border-radius:8px;border:1px dashed var(--border)}}
footer{{text-align:center;padding:20px;color:var(--muted);font-size:10px;border-top:1px solid var(--border);margin-top:20px}}
@media(max-width:680px){{.hdr,.resumen,.leyenda,.content{{padding-left:16px;padding-right:16px}}
  .auto-head{{flex-direction:column}}.mercado-stats{{gap:8px}}}}
</style>
</head>
<body>

<div class="hdr">
  <h1>COMPARADOR DE MERCADO — AUTOS USADOS BRUNO FRITSCH vs CHILEAUTOS</h1>
  <div class="sub">Posicionamiento de precios BF vs mercado C2C con análisis estadístico IQR</div>
  <div class="hdr-meta">
    <span>Generado: <strong>{fecha_gen} {hora_gen}</strong></span>
    <span>BF analizado: <strong>{total_bf} vehículos</strong></span>
    <span>Con comparación: <strong>{len(con_datos)} vehículos</strong></span>
    <span>Fuente mercado: <strong>Chileautos.cl</strong></span>
  </div>
  <div class="ubadge">🔄 Se actualiza automáticamente cada lunes</div>
</div>

<div class="leyenda">
  <span class="lbl">Posición vs mercado</span>
  <span>🔴 <strong>Sobre mercado</strong> — precio BF mayor al P75 de Chileautos (riesgo de rotación lenta)</span>
  <span>⚪ <strong>En mercado</strong> — entre P25 y P75 (precio competitivo)</span>
  <span>🟢 <strong>Bajo mercado</strong> — precio BF menor al P25 (oportunidad de ajuste al alza)</span>
</div>

<div class="resumen">
  <div class="res-box alto">
    <div class="n">{len(sobre_mercado)}</div>
    <div class="desc">🔴 Sobre mercado</div>
    <div class="sub">Precio mayor al P75</div>
  </div>
  <div class="res-box ok">
    <div class="n">{len(en_mercado)}</div>
    <div class="desc">⚪ En mercado</div>
    <div class="sub">Precio competitivo</div>
  </div>
  <div class="res-box bajo">
    <div class="n">{len(bajo_mercado)}</div>
    <div class="desc">🟢 Bajo mercado</div>
    <div class="sub">Potencial ajuste al alza</div>
  </div>
  <div class="res-box neutro">
    <div class="n">{len(sin_datos)}</div>
    <div class="desc">⚫ Sin referencia</div>
    <div class="sub">Sin datos en Chileautos</div>
  </div>
  <div class="res-box neutro">
    <div class="n">{len([r for r in con_datos if r["ca_stats"] and r["ca_stats"]["confiabilidad"]=="ALTA"])}</div>
    <div class="desc">✅ Alta confiabilidad</div>
    <div class="sub">≥8 unidades en CA</div>
  </div>
  <div class="res-box neutro">
    <div class="n">{len([r for r in con_datos if r["ca_stats"] and r["ca_stats"]["confiabilidad"]=="MEDIA"])}</div>
    <div class="desc">⚠️ Referencial</div>
    <div class="sub">4-7 unidades en CA</div>
  </div>
</div>

<div class="content">

<div class="section">
  <div class="sec-hdr">
    <h2>📊 Comparación precio BF vs mercado Chileautos</h2>
    <span class="sec-hdr hint" style="font-size:10px;color:#64748b">Ordenado por: sobre mercado primero, luego por mayor diferencia</span>
    <span class="cnt">{len(con_datos)} vehículos</span>
  </div>
  {marcas_html if marcas_html else '<div class="empty-state">Sin datos de comparación disponibles</div>'}
</div>

<div class="section">
  <div class="sec-hdr">
    <h2>⚫ Sin referencia en Chileautos</h2>
    <span style="font-size:10px;color:#64748b">Modelos sin unidades publicadas en Chileautos al momento del análisis</span>
    <span class="cnt">{len(sin_datos)} vehículos</span>
  </div>
  {sin_datos_html if sin_datos_html else '<div class="empty-state">Todos los modelos tienen referencia en Chileautos ✓</div>'}
</div>

</div>

<footer>
  Comparador generado automáticamente · Bruno Fritsch vs Chileautos.cl · {fecha_gen} {hora_gen} · {total_bf} vehículos BF analizados<br>
  Metodología: eliminación de outliers por IQR (1.5×) · Comparación por versión exacta cuando muestra ≥4 · Fallback a modelo/año
</footer>

<script>
function toggle(id){{
  const body=document.getElementById(id);
  const ch=document.getElementById('ch_'+id);
  if(!body) return;
  const open=body.classList.toggle('open');
  if(ch) ch.classList.toggle('open',open);
}}
document.addEventListener('DOMContentLoaded',()=>{{
  // Abrir primera marca automáticamente
  const primero=document.querySelector('.marca-body');
  if(primero){{
    primero.classList.add('open');
    const ch=document.getElementById('ch_'+primero.id);
    if(ch) ch.classList.add('open');
  }}
}});
</script>
</body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now       = datetime.now()
    fecha_gen = now.strftime("%d/%m/%Y")
    hora_gen  = now.strftime("%H:%M")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 55)
    log.info("  COMPARADOR MERCADO BF vs CHILEAUTOS")
    log.info(f"  {fecha_gen} {hora_gen}")
    log.info("=" * 55)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            locale="es-CL",
        )
        page = ctx.new_page()

        # 1. Scraping BF
        bf_autos = scrape_bf(page)
        log.info(f"BF: {len(bf_autos)} autos extraídos")

        # 2. Determinar modelos únicos a buscar en CA
        modelos_unicos: set[tuple] = set()
        for a in bf_autos:
            if a["marca"] and a["modelo"] and a["ano"]:
                modelos_unicos.add((a["marca"], a["modelo"], a["ano"]))

        log.info(f"Modelos únicos a buscar en Chileautos: {len(modelos_unicos)}")

        # 3. Scraping Chileautos por modelo
        ca_data: dict[str, list] = {}
        for i, (marca, modelo, ano) in enumerate(sorted(modelos_unicos)):
            clave = f"{marca}|{modelo}|{ano}"
            unidades = scrape_chileautos_modelo(page, marca, modelo, ano)
            ca_data[clave] = unidades
            log.info(f"  CA [{i+1}/{len(modelos_unicos)}] {marca} {modelo} {ano}: {len(unidades)} unidades")
            time.sleep(DELAY)

        browser.close()

    # 4. Análisis
    resultados = analizar(bf_autos, ca_data)

    # 5. HTML
    html = generar_html(resultados, fecha_gen, hora_gen)
    OUTPUT_FILE.write_text(html, encoding="utf-8")

    # 6. Data JSON
    DATA_FILE.write_text(
        json.dumps({
            "generado": now.isoformat(),
            "total_bf": len(bf_autos),
            "total_ca_modelos": len(ca_data),
            "con_comparacion": len([r for r in resultados if r.get("posicion")]),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    sobre  = len([r for r in resultados if r.get("posicion") and r["posicion"]["estado"]=="ALTO"])
    bajo   = len([r for r in resultados if r.get("posicion") and r["posicion"]["estado"]=="BAJO"])
    ok     = len([r for r in resultados if r.get("posicion") and r["posicion"]["estado"]=="OK"])
    sin    = len([r for r in resultados if not r.get("posicion")])

    log.info(f"RESULTADO: Sobre={sobre} | OK={ok} | Bajo={bajo} | SinDatos={sin}")
    log.info(f"Informe: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

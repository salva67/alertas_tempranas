"""Motor de alertas meteorológicas para pólizas agrícolas.

Este módulo contiene la lógica reusable para:
- Normalizar el Excel de pólizas.
- Filtrar cultivos de gruesa con coordenadas válidas.
- Consultar Visual Crossing.
- Evaluar umbrales de lluvia, viento y temperatura mínima.
- Construir mapa Folium y enviar email.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import time
from dataclasses import dataclass
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import folium
import pandas as pd
import requests
from folium.plugins import Fullscreen, MarkerCluster, MeasureControl, MiniMap, MousePosition


COARSE_CROPS = {"SOJA", "MAIZ", "MAÍZ", "GIRASOL", "SORGO", "MANI", "MANÍ", "ALGODON", "ALGODÓN"}
FINE_CROPS = {"TRIGO", "CEBADA", "AVENA", "CENTENO", "COLZA", "TRITICALE", "ARVEJA", "LENTEJA", "GARBANZO"}

REQUIRED_COLUMNS = ["LATITUD", "LONGITUD", "CULTIVO"]
OPTIONAL_COLUMNS = ["IT", "ASEGURADO", "PROVINCIA", "DEPTO", "LOCALIDAD", "CAMPO", "HAS", "MONEDA", "SUMA_ASEGURADA", "ADICIONALES", "CAMPAÑA"]


@dataclass
class AlertThresholds:
    lluvia_mm_dia: float = 30.0
    lluvia_acum_72h: float = 30.0
    viento_kmh: float = 50.0
    tmin_c: float = 3.0


@dataclass
class EmailConfig:
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_user: str = ""
    email_pass: str = ""
    sender_name: str = "Sistema de Alertas Meteorológicas"


def _clean_column_name(name: Any) -> str:
    return str(name).strip().upper()


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza nombres de columnas y agrega opcionales faltantes."""
    out = df.copy()
    out.columns = [_clean_column_name(c) for c in out.columns]
    for col in OPTIONAL_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"Faltan columnas requeridas en el Excel: {', '.join(missing)}")
    return out


def norm_txt(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().upper()


def to_numeric_coord(series: pd.Series) -> pd.Series:
    """Convierte coordenadas con coma o punto decimal a float."""
    return pd.to_numeric(series.astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")


def infer_campaign(cultivo: Any) -> str:
    """Infere campaña fina/gruesa desde el nombre de cultivo."""
    c = norm_txt(cultivo)
    if not c:
        return "DESCONOCIDA"
    if c in FINE_CROPS or "TRIG" in c or "CEBAD" in c or "AVEN" in c:
        return "FINA"
    if c in COARSE_CROPS or "SOJ" in c or "MAI" in c or "MAÍ" in c or "GIRAS" in c or "SOR" in c:
        return "GRUESA"
    return "DESCONOCIDA"


def prepare_policies(df: pd.DataFrame, only_gruesa: bool = True) -> Tuple[pd.DataFrame, Dict[str, int]]:
    """Prepara el dataframe de pólizas para el motor de alertas.

    Devuelve el dataframe listo y un resumen de calidad.
    """
    original_rows = len(df)
    out = normalize_columns(df)

    # Texto normalizado
    for col in ["ASEGURADO", "CAMPO", "CULTIVO", "PROVINCIA", "DEPTO", "LOCALIDAD", "MONEDA"]:
        if col in out.columns:
            out[col] = out[col].map(norm_txt)

    # Coordenadas y numéricos
    out["LAT_NUM"] = to_numeric_coord(out["LATITUD"])
    out["LON_NUM"] = to_numeric_coord(out["LONGITUD"])
    out["HAS"] = pd.to_numeric(out["HAS"], errors="coerce")
    if "SUMA_ASEGURADA" in out.columns:
        out["SUMA_ASEGURADA"] = pd.to_numeric(out["SUMA_ASEGURADA"], errors="coerce")

    out["TIPO_CAMPAÑA"] = out["CULTIVO"].apply(infer_campaign)
    out["COORD_OK"] = out["LAT_NUM"].notna() & out["LON_NUM"].notna()
    out["COORD_IN_RANGE"] = (
        out["COORD_OK"]
        & out["LAT_NUM"].between(-60, -20)
        & out["LON_NUM"].between(-75, -50)
        & (out["LAT_NUM"] != 0)
        & (out["LON_NUM"] != 0)
    )

    valid_coords = int(out["COORD_IN_RANGE"].sum())
    out = out[out["COORD_IN_RANGE"]].copy()

    if only_gruesa:
        out = out[out["TIPO_CAMPAÑA"].eq("GRUESA")].copy()

    out["IT"] = out["IT"].astype(str)

    stats = {
        "filas_originales": original_rows,
        "coordenadas_validas": valid_coords,
        "filas_luego_filtro": len(out),
        "gruesa": int((out["TIPO_CAMPAÑA"] == "GRUESA").sum()),
    }
    return out.reset_index(drop=True), stats


def fetch_visual_crossing(
    lat: float,
    lon: float,
    api_key: str,
    days: int = 3,
    timeout: int = 30,
    retries: int = 2,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Consulta Visual Crossing y devuelve dataframe diario de pronóstico."""
    if not api_key:
        return None, "Falta VISUALCROSSING_API_KEY"

    url = f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{lat},{lon}"
    params = {
        "key": api_key,
        "unitGroup": "metric",
        "include": "days",
        "contentType": "json",
        "elements": "datetime,precip,tempmin,windspeed,conditions,description",
    }

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:250].replace(chr(10), ' ')}"
                if resp.status_code >= 500 and attempt < retries:
                    time.sleep(1.2 * attempt)
                    continue
                return None, last_error

            data = resp.json()
            days_payload = data.get("days", [])
            if not days_payload:
                return None, "Respuesta sin bloque 'days'"

            df_day = pd.DataFrame(days_payload).head(days)
            expected = ["datetime", "precip", "tempmin", "windspeed"]
            missing = [c for c in expected if c not in df_day.columns]
            if missing:
                return None, f"Faltan campos climáticos: {', '.join(missing)}"

            df_day = df_day.rename(
                columns={
                    "datetime": "FECHA",
                    "precip": "PRECIP_MM",
                    "windspeed": "VIENTO_KMH",
                    "tempmin": "TMIN_C",
                    "conditions": "CONDICIONES",
                    "description": "DESCRIPCION",
                }
            )
            for col in ["PRECIP_MM", "VIENTO_KMH", "TMIN_C"]:
                df_day[col] = pd.to_numeric(df_day[col], errors="coerce")
            return df_day, "OK"
        except requests.Timeout:
            last_error = f"Timeout mayor a {timeout}s"
            if attempt < retries:
                time.sleep(1.2 * attempt)
                continue
        except requests.RequestException as exc:
            last_error = f"Error de request: {exc}"
            if attempt < retries:
                time.sleep(1.2 * attempt)
                continue
        except json.JSONDecodeError:
            return None, "Respuesta no es JSON válido"
        except Exception as exc:  # noqa: BLE001
            return None, f"Error inesperado: {type(exc).__name__}: {exc}"

    return None, last_error


def fetch_open_meteo(
    lat: float,
    lon: float,
    days: int = 3,
    timeout: int = 30,
    retries: int = 2,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Consulta Open-Meteo y devuelve dataframe diario de pronóstico.

    No requiere API key. Se usa como fuente principal o fallback cuando Visual Crossing falla.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "precipitation_sum,temperature_2m_min,wind_speed_10m_max",
        "forecast_days": max(int(days), 1),
        "timezone": "America/Argentina/Buenos_Aires",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                last_error = f"OPEN_METEO HTTP {resp.status_code}: {resp.text[:250].replace(chr(10), ' ')}"
                if resp.status_code >= 500 and attempt < retries:
                    time.sleep(1.2 * attempt)
                    continue
                return None, last_error

            data = resp.json()
            daily = data.get("daily", {})
            if not daily or "time" not in daily:
                return None, "OPEN_METEO respuesta sin bloque daily/time"

            df_day = pd.DataFrame(
                {
                    "FECHA": daily.get("time", []),
                    "PRECIP_MM": daily.get("precipitation_sum", []),
                    "TMIN_C": daily.get("temperature_2m_min", []),
                    "VIENTO_KMH": daily.get("wind_speed_10m_max", []),
                }
            ).head(days)

            if df_day.empty:
                return None, "OPEN_METEO daily vacío"

            for col in ["PRECIP_MM", "VIENTO_KMH", "TMIN_C"]:
                df_day[col] = pd.to_numeric(df_day[col], errors="coerce")
            df_day["CONDICIONES"] = ""
            df_day["DESCRIPCION"] = ""
            return df_day, "OK_OPEN_METEO"
        except requests.Timeout:
            last_error = f"OPEN_METEO Timeout mayor a {timeout}s"
            if attempt < retries:
                time.sleep(1.2 * attempt)
                continue
        except requests.RequestException as exc:
            last_error = f"OPEN_METEO error de request: {exc}"
            if attempt < retries:
                time.sleep(1.2 * attempt)
                continue
        except json.JSONDecodeError:
            return None, "OPEN_METEO respuesta no es JSON válido"
        except Exception as exc:  # noqa: BLE001
            return None, f"OPEN_METEO error inesperado: {type(exc).__name__}: {exc}"

    return None, last_error


def fetch_weather_forecast(
    lat: float,
    lon: float,
    api_key: str = "",
    days: int = 3,
    provider: str = "Auto: Visual Crossing + fallback Open-Meteo",
) -> Tuple[Optional[pd.DataFrame], str]:
    """Obtiene pronóstico según proveedor elegido.

    provider:
    - Auto: Visual Crossing + fallback Open-Meteo
    - Open-Meteo sin API key
    - Visual Crossing únicamente
    """
    provider_norm = str(provider or "").strip().lower()

    if "open-meteo" in provider_norm and "visual" not in provider_norm:
        return fetch_open_meteo(lat, lon, days=days)

    if "visual" in provider_norm and "únicamente" in provider_norm:
        return fetch_visual_crossing(lat, lon, api_key=api_key, days=days)

    # Modo auto: intenta Visual Crossing si hay key real; si falla, usa Open-Meteo.
    vc_df, vc_status = fetch_visual_crossing(lat, lon, api_key=api_key, days=days) if api_key else (None, "VISUAL_CROSSING omitido: sin API key")
    if vc_df is not None and not vc_df.empty:
        return vc_df, "OK_VISUAL_CROSSING"

    om_df, om_status = fetch_open_meteo(lat, lon, days=days)
    if om_df is not None and not om_df.empty:
        return om_df, f"OK_OPEN_METEO | fallback usado; Visual Crossing: {vc_status}"

    return None, f"Sin dato. Visual Crossing: {vc_status} | Open-Meteo: {om_status}"


def evaluate_forecast(df_day: pd.DataFrame, thresholds: AlertThresholds) -> Tuple[List[str], str, Dict[str, float]]:
    """Evalúa pronóstico diario y devuelve alertas, riesgo y métricas resumidas."""
    alertas: List[str] = []
    risk_rank = {"Bajo": 0, "Moderado": 1, "Alto": 2, "Muy Alto": 3}
    risk = "Bajo"

    def bump(new_risk: str) -> None:
        nonlocal risk
        if risk_rank[new_risk] > risk_rank[risk]:
            risk = new_risk

    for _, row in df_day.iterrows():
        fecha = str(row.get("FECHA", ""))
        precip = row.get("PRECIP_MM")
        viento = row.get("VIENTO_KMH")
        tmin = row.get("TMIN_C")

        if pd.notna(precip) and float(precip) >= thresholds.lluvia_mm_dia:
            alertas.append(f"{fecha}: LLUVIAS INTENSAS ({float(precip):.1f} mm)")
            bump("Alto")

        if pd.notna(viento) and float(viento) >= thresholds.viento_kmh:
            alertas.append(f"{fecha}: VIENTO FUERTE ({float(viento):.0f} km/h)")
            bump("Alto")

        if pd.notna(tmin) and float(tmin) <= thresholds.tmin_c:
            alertas.append(f"{fecha}: HELADA / BAJA TEMPERATURA (Tmin {float(tmin):.1f} °C)")
            bump("Muy Alto")

    acum72 = float(df_day["PRECIP_MM"].fillna(0).sum())
    max_wind = float(df_day["VIENTO_KMH"].max()) if df_day["VIENTO_KMH"].notna().any() else 0.0
    min_tmin = float(df_day["TMIN_C"].min()) if df_day["TMIN_C"].notna().any() else 999.0
    max_rain_day = float(df_day["PRECIP_MM"].max()) if df_day["PRECIP_MM"].notna().any() else 0.0

    if acum72 >= thresholds.lluvia_acum_72h:
        alertas.append(f"72h: LLUVIA ACUMULADA {acum72:.1f} mm")
        bump("Alto")

    metrics = {
        "LLUVIA_72H_MM": acum72,
        "LLUVIA_MAX_DIA_MM": max_rain_day,
        "VIENTO_MAX_KMH": max_wind,
        "TMIN_MIN_C": min_tmin if min_tmin != 999.0 else float("nan"),
    }
    return alertas, risk, metrics


def evaluate_policy(row: pd.Series, api_key: str, thresholds: AlertThresholds, days: int = 3, provider: str = "Auto: Visual Crossing + fallback Open-Meteo") -> Dict[str, Any]:
    """Ejecuta consulta y evaluación para una póliza/campo."""
    lat = float(row["LAT_NUM"])
    lon = float(row["LON_NUM"])
    df_day, status = fetch_weather_forecast(lat, lon, api_key=api_key, days=days, provider=provider)

    base = {
        "IT": row.get("IT", ""),
        "ASEGURADO": row.get("ASEGURADO", ""),
        "CULTIVO": row.get("CULTIVO", ""),
        "CAMPO": row.get("CAMPO", ""),
        "PROVINCIA": row.get("PROVINCIA", ""),
        "DEPTO": row.get("DEPTO", ""),
        "LOCALIDAD": row.get("LOCALIDAD", ""),
        "HAS": row.get("HAS", None),
        "LAT": lat,
        "LON": lon,
        "API_STATUS": status,
    }

    if df_day is None or df_day.empty:
        return {**base, "RIESGO": "Sin dato", "ALERTAS": [], "TIENE_ALERTA": False}

    alertas, riesgo, metrics = evaluate_forecast(df_day, thresholds)
    return {
        **base,
        **metrics,
        "RIESGO": riesgo,
        "ALERTAS": alertas,
        "ALERTAS_TXT": " | ".join(alertas),
        "TIENE_ALERTA": len(alertas) > 0,
        "PRONOSTICO_JSON": df_day.to_json(orient="records", force_ascii=False),
    }


def _risk_color(risk: str) -> str:
    return {
        "Bajo": "#2ECC71",
        "Moderado": "#F1C40F",
        "Alto": "#E67E22",
        "Muy Alto": "#E74C3C",
        "Sin dato": "#7F8C8D",
    }.get(risk, "#3498DB")


def build_alert_map(df_alerts: pd.DataFrame, include_no_alerts: bool = False) -> Optional[folium.Map]:
    """Construye mapa Folium con pólizas alertadas."""
    if df_alerts is None or df_alerts.empty:
        return None

    plot_df = df_alerts.copy()
    if not include_no_alerts and "TIENE_ALERTA" in plot_df.columns:
        plot_df = plot_df[plot_df["TIENE_ALERTA"]]
    if plot_df.empty:
        return None

    lat_c = float(plot_df["LAT"].astype(float).mean())
    lon_c = float(plot_df["LON"].astype(float).mean())
    m = folium.Map(location=[lat_c, lon_c], zoom_start=6, tiles="CartoDB positron", control_scale=True)

    marker_cluster = MarkerCluster(name="Campos / pólizas").add_to(m)

    for _, r in plot_df.iterrows():
        risk = str(r.get("RIESGO", "Bajo"))
        color = _risk_color(risk)
        alertas_raw = r.get("ALERTAS", [])
        if isinstance(alertas_raw, str):
            alertas = [a.strip() for a in alertas_raw.split("|") if a.strip()]
        elif isinstance(alertas_raw, Iterable):
            alertas = [str(a) for a in alertas_raw]
        else:
            alertas = []

        alertas_html = "<br>".join(f"• {html.escape(a)}" for a in alertas) or "Sin alertas"
        popup_html = f"""
        <div style="font-size:13px; line-height:1.35; min-width:260px;">
            <b>{html.escape(str(r.get('ASEGURADO', '')))}</b><br>
            <b>Campo:</b> {html.escape(str(r.get('CAMPO', '')))}<br>
            <b>Cultivo:</b> {html.escape(str(r.get('CULTIVO', '')))}<br>
            <b>Ubicación:</b> {html.escape(str(r.get('DEPTO', '')))}, {html.escape(str(r.get('PROVINCIA', '')))}<br>
            <b>Riesgo:</b> <span style="color:{color}; font-weight:700">{html.escape(risk)}</span><br>
            <b>Lluvia 72h:</b> {float(r.get('LLUVIA_72H_MM', 0) or 0):.1f} mm<br>
            <b>Viento máx:</b> {float(r.get('VIENTO_MAX_KMH', 0) or 0):.0f} km/h<br>
            <b>Tmin:</b> {float(r.get('TMIN_MIN_C', 0) or 0):.1f} °C<br>
            <hr style="margin:6px 0;">
            <b>Alertas:</b><br>{alertas_html}
        </div>
        """
        folium.CircleMarker(
            location=[float(r["LAT"]), float(r["LON"])],
            radius=9 if risk in {"Alto", "Muy Alto"} else 6,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.75,
            popup=folium.Popup(popup_html, max_width=420),
            tooltip=f"{r.get('CAMPO', '')} | {risk}",
        ).add_to(marker_cluster)

    MiniMap(toggle_display=True).add_to(m)
    Fullscreen().add_to(m)
    MeasureControl(primary_length_unit="kilometers").add_to(m)
    MousePosition(position="bottomright", separator=" | ", prefix="Lat/Lon:").add_to(m)
    folium.LayerControl().add_to(m)
    return m


def map_to_html_file(m: folium.Map, path: str | Path = "mapa_alertas.html") -> Path:
    out = Path(path)
    m.save(str(out))
    return out


def build_email_body(df_alerts: pd.DataFrame) -> str:
    body = "⚠️ Reporte de Alertas Meteorológicas – Próximas 72h\n\n"
    for _, r in df_alerts[df_alerts["TIENE_ALERTA"]].iterrows():
        alertas = r.get("ALERTAS", [])
        if isinstance(alertas, str):
            alertas = [a.strip() for a in alertas.split("|") if a.strip()]
        body += (
            f"Asegurado: {r.get('ASEGURADO', '')}\n"
            f"Campo: {r.get('CAMPO', '')} ({r.get('CULTIVO', '')})\n"
            f"Ubicación: {r.get('DEPTO', '')}, {r.get('PROVINCIA', '')}\n"
            f"Riesgo: {r.get('RIESGO', '')}\n"
            f"Lluvia 72h: {float(r.get('LLUVIA_72H_MM', 0) or 0):.1f} mm\n"
            f"Viento máximo: {float(r.get('VIENTO_MAX_KMH', 0) or 0):.0f} km/h\n"
            f"Alertas:\n- " + "\n- ".join(alertas) + "\n\n"
        )
    return body


def send_email_alerts(
    df_alerts: pd.DataFrame,
    recipient: str,
    email_cfg: EmailConfig,
    map_html_path: Optional[str | Path] = None,
) -> None:
    """Envía email con alertas y mapa HTML adjunto."""
    if df_alerts.empty or not df_alerts["TIENE_ALERTA"].any():
        raise ValueError("No hay alertas para enviar.")
    if not recipient:
        raise ValueError("Falta destinatario.")
    if not email_cfg.email_user or not email_cfg.email_pass:
        raise ValueError("Falta usuario o password SMTP.")

    msg = MIMEMultipart()
    msg["From"] = email_cfg.email_user
    msg["To"] = recipient
    msg["Subject"] = "⚠️ Alertas Meteorológicas – Pólizas agrícolas (72h)"
    msg.attach(MIMEText(build_email_body(df_alerts), "plain", "utf-8"))

    if map_html_path and Path(map_html_path).exists():
        with open(map_html_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{Path(map_html_path).name}"')
        msg.attach(part)

    with smtplib.SMTP(email_cfg.smtp_server, int(email_cfg.smtp_port)) as server:
        server.starttls()
        server.login(email_cfg.email_user, email_cfg.email_pass)
        server.sendmail(email_cfg.email_user, recipient, msg.as_string())

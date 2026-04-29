from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

from alert_engine import (
    AlertThresholds,
    EmailConfig,
    build_alert_map,
    evaluate_policy,
    fetch_visual_crossing,
    fetch_weather_forecast,
    map_to_html_file,
    prepare_policies,
    send_email_alerts,
)

st.set_page_config(
    page_title="Sistema de Alertas Meteorológicas",
    page_icon="🌦️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
    .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        padding: 12px 16px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.05);
    }
    .risk-card {
        border-radius: 14px;
        padding: 14px 16px;
        background: #f8fafc;
        border: 1px solid #e5e7eb;
    }
</style>
""",
    unsafe_allow_html=True,
)


def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value) if value is not None else default
    except Exception:
        return default


def read_excel(uploaded_file) -> pd.DataFrame:
    return pd.read_excel(uploaded_file)


@st.cache_data(show_spinner=False)
def to_csv_bytes(df: pd.DataFrame) -> bytes:
    export = df.copy()
    if "ALERTAS" in export.columns:
        export["ALERTAS"] = export["ALERTAS"].apply(lambda x: " | ".join(x) if isinstance(x, list) else x)
    return export.to_csv(index=False).encode("utf-8-sig")


def _cell_to_streamlit_safe(value):
    """Convierte celdas mixtas/listas/dicts a valores serializables por PyArrow."""
    if isinstance(value, (list, tuple, set)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if pd.isna(value):
        return ""
    return str(value)


def dataframe_for_streamlit(df: pd.DataFrame) -> pd.DataFrame:
    """Evita errores Arrow cuando una columna object mezcla texto, números y NaN."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(_cell_to_streamlit_safe).astype("string")
    return out


def safe_dataframe(df: pd.DataFrame, **kwargs) -> None:
    data = dataframe_for_streamlit(df)
    try:
        st.dataframe(data, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(data, use_container_width=True, **kwargs)


def safe_plotly_chart(fig, **kwargs) -> None:
    try:
        st.plotly_chart(fig, width="stretch", **kwargs)
    except TypeError:
        st.plotly_chart(fig, use_container_width=True, **kwargs)


def safe_folium_map(fmap, height: int = 620):
    try:
        return st_folium(fmap, height=height, width="stretch")
    except TypeError:
        try:
            return st_folium(fmap, height=height, width=1200)
        except TypeError:
            return st_folium(fmap, height=height, use_container_width=True)


def looks_like_placeholder_api_key(api_key: str) -> bool:
    k = str(api_key or "").strip().lower()
    bad_tokens = ["tu_api_key", "pegar_aca", "api_key", "visualcrossing_api_key", "xxx", "changeme"]
    return (not k) or any(token in k for token in bad_tokens) or len(k) < 10


def run_scan(df_policies: pd.DataFrame, api_key: str, thresholds: AlertThresholds, max_workers: int, provider: str) -> pd.DataFrame:
    results = []
    total = len(df_policies)
    progress = st.progress(0, text="Preparando consultas meteorológicas...")
    status_box = st.empty()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(evaluate_policy, row, api_key, thresholds, 3, provider): idx
            for idx, row in df_policies.iterrows()
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            idx = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                row = df_policies.loc[idx]
                results.append(
                    {
                        "IT": row.get("IT", ""),
                        "ASEGURADO": row.get("ASEGURADO", ""),
                        "CULTIVO": row.get("CULTIVO", ""),
                        "CAMPO": row.get("CAMPO", ""),
                        "PROVINCIA": row.get("PROVINCIA", ""),
                        "DEPTO": row.get("DEPTO", ""),
                        "LOCALIDAD": row.get("LOCALIDAD", ""),
                        "HAS": row.get("HAS", None),
                        "LAT": row.get("LAT_NUM", None),
                        "LON": row.get("LON_NUM", None),
                        "RIESGO": "Sin dato",
                        "ALERTAS": [],
                        "ALERTAS_TXT": "",
                        "TIENE_ALERTA": False,
                        "API_STATUS": f"Error interno al evaluar póliza: {type(exc).__name__}: {exc}",
                    }
                )
            progress.progress(i / total, text=f"Consultando clima y evaluando pólizas: {i}/{total}")
            if i % 10 == 0 or i == total:
                status_box.caption(f"Procesadas {i:,} de {total:,} pólizas")

    progress.empty()
    status_box.empty()
    return pd.DataFrame(results)


def render_api_diagnostic(df_results: pd.DataFrame) -> None:
    st.markdown("### Diagnóstico de respuestas climáticas")
    if "API_STATUS" not in df_results.columns:
        st.info("No hay columna API_STATUS para diagnosticar.")
        return

    status_counts = (
        df_results["API_STATUS"]
        .fillna("Sin estado")
        .astype(str)
        .value_counts()
        .reset_index()
    )
    status_counts.columns = ["API_STATUS", "CANTIDAD"]
    safe_dataframe(status_counts)

    sin_dato = df_results[df_results.get("RIESGO", "") == "Sin dato"].copy()
    if not sin_dato.empty:
        st.markdown("**Muestra de pólizas sin dato clima**")
        diag_cols = ["ASEGURADO", "CAMPO", "PROVINCIA", "DEPTO", "LAT", "LON", "API_STATUS"]
        diag_cols = [c for c in diag_cols if c in sin_dato.columns]
        safe_dataframe(sin_dato[diag_cols].head(30))

    st.markdown(
        """
**Cómo interpretar esto**

- `Falta VISUALCROSSING_API_KEY`: la app no está leyendo el secret o la clave no fue ingresada.
- `HTTP 401 / 403`: clave inválida, sin permisos o mal pegada.
- `HTTP 429`: límite de consultas alcanzado o demasiadas consultas en paralelo.
- `Timeout`: la API no respondió a tiempo; bajá las consultas en paralelo a 1 o 2.
- `Respuesta sin bloque 'days'`: la API respondió, pero no con el formato esperado.
"""
    )


st.title("🌦️ Sistema de Alertas Meteorológicas para Pólizas Agrícolas")
st.caption("Carga un Excel de pólizas, filtra cultivos de gruesa, consulta pronóstico 72h y genera alertas por lluvia, viento y baja temperatura.")

with st.sidebar:
    st.header("Configuración")

    api_key = st.text_input(
        "Visual Crossing API Key",
        value=get_secret("VISUALCROSSING_API_KEY", ""),
        type="password",
        help="En Streamlit Cloud configurala como secret: VISUALCROSSING_API_KEY",
    ).strip()

    provider = st.selectbox(
        "Fuente meteorológica",
        [
            "Auto: Visual Crossing + fallback Open-Meteo",
            "Open-Meteo sin API key",
            "Visual Crossing únicamente",
        ],
        index=0,
        help=(
            "Auto intenta Visual Crossing si hay API key y, si falla, usa Open-Meteo. "
            "Open-Meteo no requiere API key."
        ),
    )

    provider_requires_key = provider == "Visual Crossing únicamente"

    if looks_like_placeholder_api_key(api_key) and provider != "Open-Meteo sin API key":
        st.warning("La API key parece vacía, incompleta o placeholder. En modo Auto la app usará Open-Meteo como fallback.")

    st.subheader("Umbrales de alerta")
    lluvia_mm_dia = st.number_input("Lluvia intensa diaria (mm)", min_value=0.0, value=30.0, step=1.0)
    lluvia_acum_72h = st.number_input("Lluvia acumulada 72h (mm)", min_value=0.0, value=30.0, step=1.0)
    viento_kmh = st.number_input("Viento fuerte (km/h)", min_value=0.0, value=50.0, step=5.0)
    tmin_c = st.number_input("Helada / baja Tmin (°C)", min_value=-20.0, max_value=20.0, value=3.0, step=0.5)

    thresholds = AlertThresholds(
        lluvia_mm_dia=float(lluvia_mm_dia),
        lluvia_acum_72h=float(lluvia_acum_72h),
        viento_kmh=float(viento_kmh),
        tmin_c=float(tmin_c),
    )

    st.subheader("Ejecución")
    only_gruesa = st.checkbox("Filtrar solo campaña gruesa", value=True)
    max_policies = st.number_input("Máximo de pólizas a consultar", min_value=1, value=200, step=50)
    max_workers = st.slider("Consultas en paralelo", min_value=1, max_value=10, value=2)
    include_no_alerts_map = st.checkbox("Mostrar también campos sin alerta en el mapa", value=False)

uploaded = st.file_uploader("Subí el Excel de pólizas", type=["xlsx", "xls"])

if uploaded is None:
    st.info("Subí un Excel con columnas mínimas: LATITUD, LONGITUD y CULTIVO. Recomendadas: IT, ASEGURADO, CAMPO, PROVINCIA, DEPTO, LOCALIDAD, HAS.")
    st.stop()

try:
    df_raw = read_excel(uploaded)
except Exception as exc:  # noqa: BLE001
    st.error(f"No pude leer el Excel: {exc}")
    st.stop()

try:
    df_policies, stats = prepare_policies(df_raw, only_gruesa=only_gruesa)
except Exception as exc:  # noqa: BLE001
    st.error(str(exc))
    st.stop()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Filas Excel", f"{stats['filas_originales']:,}")
c2.metric("Coordenadas válidas", f"{stats['coordenadas_validas']:,}")
c3.metric("Pólizas a analizar", f"{len(df_policies):,}")
c4.metric("Campaña gruesa", f"{stats['gruesa']:,}")

with st.expander("Vista previa de pólizas preparadas", expanded=False):
    safe_dataframe(df_policies.head(50))

if len(df_policies) == 0:
    st.warning("No quedaron pólizas para analizar luego de aplicar filtros de coordenadas/campaña.")
    st.stop()

# Filtros opcionales antes de ejecutar
fc1, fc2, fc3 = st.columns(3)
with fc1:
    provincias = sorted([p for p in df_policies["PROVINCIA"].dropna().unique() if p])
    sel_prov = st.multiselect("Filtrar provincia", provincias, default=[])
with fc2:
    cultivos = sorted([c for c in df_policies["CULTIVO"].dropna().unique() if c])
    sel_cultivo = st.multiselect("Filtrar cultivo", cultivos, default=[])
with fc3:
    asegurados = sorted([a for a in df_policies["ASEGURADO"].dropna().unique() if a])
    sel_asegurado = st.multiselect("Filtrar asegurado", asegurados, default=[])

filtered = df_policies.copy()
if sel_prov:
    filtered = filtered[filtered["PROVINCIA"].isin(sel_prov)]
if sel_cultivo:
    filtered = filtered[filtered["CULTIVO"].isin(sel_cultivo)]
if sel_asegurado:
    filtered = filtered[filtered["ASEGURADO"].isin(sel_asegurado)]

if len(filtered) > int(max_policies):
    st.warning(f"Hay {len(filtered):,} pólizas filtradas. Se consultarán las primeras {int(max_policies):,} para controlar costo/rate limit de la API.")
    filtered = filtered.head(int(max_policies)).copy()

st.write(f"**Pólizas listas para consultar:** {len(filtered):,}")

if not api_key and provider == "Visual Crossing únicamente":
    st.warning("Falta configurar la API key de Visual Crossing. Podés ingresarla en la barra lateral o cargarla como secret en Streamlit Cloud.")
elif not api_key and provider != "Visual Crossing únicamente":
    st.info("No hay API key de Visual Crossing. La app puede ejecutar igual usando Open-Meteo.")

col_test, col_run = st.columns([1, 2])
with col_test:
    test_api = st.button("🧪 Probar fuente climática con 1 póliza", disabled=(provider == "Visual Crossing únicamente" and not bool(api_key)) or len(filtered) == 0)
with col_run:
    run = st.button("🚀 Ejecutar sistema de alertas", type="primary", disabled=(provider == "Visual Crossing únicamente" and not bool(api_key)) or len(filtered) == 0)

if test_api:
    first = filtered.iloc[0]
    with st.spinner(f"Probando fuente climática: {provider}..."):
        df_day, status = fetch_weather_forecast(float(first["LAT_NUM"]), float(first["LON_NUM"]), api_key=api_key, days=3, provider=provider)
    if str(status).startswith("OK") and df_day is not None:
        st.success(f"La fuente climática respondió OK para la primera póliza: {status}")
        safe_dataframe(df_day)
    else:
        st.error(f"La prueba de fuente climática falló: {status}")
        st.info("Si Visual Crossing falla, probá seleccionar 'Open-Meteo sin API key' o dejá el modo Auto para usar fallback.")

if run:
    if provider == "Visual Crossing únicamente" and looks_like_placeholder_api_key(api_key):
        st.error("La API key parece inválida o placeholder. Corregila o cambiá la fuente a Open-Meteo / Auto.")
        st.stop()
    with st.spinner(f"Ejecutando consultas meteorológicas con fuente: {provider}..."):
        df_results = run_scan(filtered, api_key, thresholds, max_workers=max_workers, provider=provider)
    st.session_state["df_results"] = df_results

if "df_results" not in st.session_state:
    st.stop()

df_results = st.session_state["df_results"].copy()
if "TIENE_ALERTA" not in df_results.columns:
    st.error("La ejecución no generó resultados válidos.")
    st.stop()

alerts_df = df_results[df_results["TIENE_ALERTA"]].copy()
sin_dato_api = int((df_results["RIESGO"] == "Sin dato").sum())

st.subheader("Resultado del monitoreo 72h")
r1, r2, r3, r4, r5 = st.columns(5)
r1.metric("Pólizas consultadas", f"{len(df_results):,}")
r2.metric("Con alerta", f"{len(alerts_df):,}")
r3.metric("Riesgo alto", f"{int((df_results['RIESGO'] == 'Alto').sum()):,}")
r4.metric("Riesgo muy alto", f"{int((df_results['RIESGO'] == 'Muy Alto').sum()):,}")
r5.metric("Sin dato clima", f"{sin_dato_api:,}")

if sin_dato_api == len(df_results):
    st.error("Todas las consultas quedaron sin dato clima. Esto no significa ausencia de alertas: hay que revisar API key, límite/rate limit o conectividad.")
elif sin_dato_api > 0:
    st.warning(f"Hay {sin_dato_api:,} pólizas sin dato clima. Revisá la pestaña Diagnóstico API.")
elif alerts_df.empty:
    st.success("La API respondió correctamente y no se detectaron alertas con los umbrales configurados.")
else:
    st.warning(f"Se detectaron {len(alerts_df):,} pólizas con alerta.")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📍 Mapa", "📋 Resultados", "🧪 Diagnóstico API", "📊 Resumen", "✉️ Email"])

with tab1:
    fmap = build_alert_map(df_results, include_no_alerts=(include_no_alerts_map or alerts_df.empty))
    if fmap:
        safe_folium_map(fmap, height=620)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp:
            map_path = map_to_html_file(fmap, tmp.name)
            st.download_button(
                "Descargar mapa HTML",
                data=Path(map_path).read_bytes(),
                file_name="mapa_alertas.html",
                mime="text/html",
            )
    else:
        st.info("No hay puntos para mostrar en el mapa.")

with tab2:
    cols = [
        "RIESGO",
        "ASEGURADO",
        "CAMPO",
        "CULTIVO",
        "PROVINCIA",
        "DEPTO",
        "LOCALIDAD",
        "HAS",
        "LLUVIA_72H_MM",
        "LLUVIA_MAX_DIA_MM",
        "VIENTO_MAX_KMH",
        "TMIN_MIN_C",
        "ALERTAS_TXT",
        "API_STATUS",
    ]
    show_cols = [c for c in cols if c in df_results.columns]
    sort_cols = [c for c in ["TIENE_ALERTA", "RIESGO"] if c in df_results.columns]

    # Ordenar antes de seleccionar columnas visibles.
    table = df_results.copy()
    if sort_cols:
        ascending = [False] * len(sort_cols)
        table = table.sort_values(sort_cols, ascending=ascending, na_position="last")

    if show_cols:
        table = table[show_cols]

    safe_dataframe(table)
    st.download_button(
        "Descargar resultados CSV",
        data=to_csv_bytes(df_results),
        file_name="alertas_meteorologicas_72h.csv",
        mime="text/csv",
    )

with tab3:
    render_api_diagnostic(df_results)

with tab4:
    g1, g2 = st.columns(2)
    with g1:
        risk_counts = df_results["RIESGO"].value_counts().reset_index()
        risk_counts.columns = ["RIESGO", "CANTIDAD"]
        fig = px.bar(risk_counts, x="RIESGO", y="CANTIDAD", title="Distribución por nivel de riesgo")
        safe_plotly_chart(fig)
    with g2:
        if alerts_df.empty:
            st.info("No hay alertas para graficar por provincia.")
        else:
            prov_counts = alerts_df.groupby("PROVINCIA", dropna=False).size().reset_index(name="ALERTAS").sort_values("ALERTAS", ascending=False)
            fig2 = px.bar(prov_counts.head(15), x="PROVINCIA", y="ALERTAS", title="Alertas por provincia")
            safe_plotly_chart(fig2)

    if "LLUVIA_72H_MM" in df_results.columns:
        st.markdown("**Top campos por lluvia acumulada 72h**")
        top_cols = ["ASEGURADO", "CAMPO", "PROVINCIA", "DEPTO", "CULTIVO", "LLUVIA_72H_MM", "VIENTO_MAX_KMH", "TMIN_MIN_C", "RIESGO"]
        top_cols = [c for c in top_cols if c in df_results.columns]
        safe_dataframe(df_results[top_cols].sort_values("LLUVIA_72H_MM", ascending=False).head(20))
    else:
        st.info("No hay métricas climáticas porque las consultas a la API no devolvieron pronóstico válido.")

with tab5:
    if alerts_df.empty:
        st.info("No hay alertas para enviar por email. Si todas figuran como 'Sin dato clima', primero resolvé la API key/límite en Diagnóstico API.")
    else:
        st.warning("Para enviar emails desde Streamlit Cloud, configurá credenciales en Secrets. No las subas al repo.")
        smtp_server = st.text_input("SMTP server", value=get_secret("SMTP_SERVER", "smtp.gmail.com"))
        smtp_port = st.number_input("SMTP port", min_value=1, value=int(get_secret("SMTP_PORT", 587)))
        email_user = st.text_input("Email usuario", value=get_secret("EMAIL_USER", ""))
        email_pass = st.text_input("Password / app password", value=get_secret("EMAIL_PASS", ""), type="password")
        recipient = st.text_input("Destinatario", value=get_secret("DEFAULT_RECIPIENT", ""))

        if st.button("Enviar email con alertas"):
            try:
                fmap = build_alert_map(df_results, include_no_alerts=False)
                map_path = None
                if fmap:
                    tmp_path = Path(tempfile.gettempdir()) / "mapa_alertas.html"
                    map_path = map_to_html_file(fmap, tmp_path)
                cfg = EmailConfig(
                    smtp_server=smtp_server,
                    smtp_port=int(smtp_port),
                    email_user=email_user,
                    email_pass=email_pass,
                )
                send_email_alerts(df_results, recipient=recipient, email_cfg=cfg, map_html_path=map_path)
                st.success(f"Email enviado a {recipient}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"No se pudo enviar el email: {exc}")

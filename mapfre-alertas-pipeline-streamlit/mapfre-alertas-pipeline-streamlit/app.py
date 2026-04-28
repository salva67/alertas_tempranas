from __future__ import annotations

import io
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
        return st.secrets.get(name, default)
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


def run_scan(df_policies: pd.DataFrame, api_key: str, thresholds: AlertThresholds, max_workers: int) -> pd.DataFrame:
    results = []
    total = len(df_policies)
    progress = st.progress(0, text="Preparando consultas meteorológicas...")
    status_box = st.empty()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(evaluate_policy, row, api_key, thresholds, 3): idx
            for idx, row in df_policies.iterrows()
        }
        for i, fut in enumerate(as_completed(futures), start=1):
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001
                results.append({"RIESGO": "Sin dato", "TIENE_ALERTA": False, "API_STATUS": f"Error: {exc}"})
            progress.progress(i / total, text=f"Consultando clima y evaluando pólizas: {i}/{total}")
            if i % 10 == 0 or i == total:
                status_box.caption(f"Procesadas {i:,} de {total:,} pólizas")

    progress.empty()
    status_box.empty()
    return pd.DataFrame(results)


st.title("🌦️ Sistema de Alertas Meteorológicas para Pólizas Agrícolas")
st.caption("Carga un Excel de pólizas, filtra cultivos de gruesa, consulta pronóstico 72h y genera alertas por lluvia, viento y baja temperatura.")

with st.sidebar:
    st.header("Configuración")

    api_key = st.text_input(
        "Visual Crossing API Key",
        value=get_secret("VISUALCROSSING_API_KEY", ""),
        type="password",
        help="En Streamlit Cloud configurala como secret: VISUALCROSSING_API_KEY",
    )

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
    max_workers = st.slider("Consultas en paralelo", min_value=1, max_value=10, value=4)
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
    st.dataframe(df_policies.head(50), use_container_width=True)

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

if not api_key:
    st.warning("Falta configurar la API key de Visual Crossing. Podés ingresarla en la barra lateral o cargarla como secret en Streamlit Cloud.")

run = st.button("🚀 Ejecutar sistema de alertas", type="primary", disabled=not bool(api_key) or len(filtered) == 0)

if run:
    with st.spinner("Ejecutando consultas meteorológicas..."):
        df_results = run_scan(filtered, api_key, thresholds, max_workers=max_workers)
    st.session_state["df_results"] = df_results

if "df_results" not in st.session_state:
    st.stop()

df_results = st.session_state["df_results"].copy()
if "TIENE_ALERTA" not in df_results.columns:
    st.error("La ejecución no generó resultados válidos.")
    st.stop()

alerts_df = df_results[df_results["TIENE_ALERTA"]].copy()

st.subheader("Resultado del monitoreo 72h")
r1, r2, r3, r4, r5 = st.columns(5)
r1.metric("Pólizas consultadas", f"{len(df_results):,}")
r2.metric("Con alerta", f"{len(alerts_df):,}")
r3.metric("Riesgo alto", f"{int((df_results['RIESGO'] == 'Alto').sum()):,}")
r4.metric("Riesgo muy alto", f"{int((df_results['RIESGO'] == 'Muy Alto').sum()):,}")
r5.metric("Sin dato API", f"{int((df_results['RIESGO'] == 'Sin dato').sum()):,}")

if alerts_df.empty:
    st.success("No se detectaron alertas con los umbrales configurados.")
else:
    tab1, tab2, tab3, tab4 = st.tabs(["📍 Mapa", "📋 Alertas", "📊 Resumen", "✉️ Email"])

    with tab1:
        fmap = build_alert_map(df_results, include_no_alerts=include_no_alerts_map)
        if fmap:
            st_folium(fmap, height=620, use_container_width=True)
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
        st.dataframe(df_results[show_cols].sort_values(["TIENE_ALERTA", "RIESGO"], ascending=[False, False]), use_container_width=True)
        st.download_button(
            "Descargar resultados CSV",
            data=to_csv_bytes(df_results),
            file_name="alertas_meteorologicas_72h.csv",
            mime="text/csv",
        )

    with tab3:
        g1, g2 = st.columns(2)
        with g1:
            risk_counts = df_results["RIESGO"].value_counts().reset_index()
            risk_counts.columns = ["RIESGO", "CANTIDAD"]
            fig = px.bar(risk_counts, x="RIESGO", y="CANTIDAD", title="Distribución por nivel de riesgo")
            st.plotly_chart(fig, use_container_width=True)
        with g2:
            prov_counts = alerts_df.groupby("PROVINCIA", dropna=False).size().reset_index(name="ALERTAS").sort_values("ALERTAS", ascending=False)
            fig2 = px.bar(prov_counts.head(15), x="PROVINCIA", y="ALERTAS", title="Alertas por provincia")
            st.plotly_chart(fig2, use_container_width=True)

        st.markdown("**Top campos por lluvia acumulada 72h**")
        top_cols = ["ASEGURADO", "CAMPO", "PROVINCIA", "DEPTO", "CULTIVO", "LLUVIA_72H_MM", "VIENTO_MAX_KMH", "TMIN_MIN_C", "RIESGO"]
        top_cols = [c for c in top_cols if c in df_results.columns]
        st.dataframe(df_results[top_cols].sort_values("LLUVIA_72H_MM", ascending=False).head(20), use_container_width=True)

    with tab4:
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

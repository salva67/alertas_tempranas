# Sistema de Alertas Meteorológicas para Pólizas Agrícolas

App en Streamlit basada en el pipeline de alertas meteorológicas para pólizas agrícolas.

La aplicación permite:

- Cargar un Excel de pólizas.
- Normalizar coordenadas y cultivos.
- Filtrar campaña gruesa.
- Consultar pronóstico de 72 horas con Visual Crossing.
- Detectar alertas por lluvia intensa, lluvia acumulada, viento fuerte y temperatura mínima.
- Visualizar resultados en tabla, gráficos y mapa Folium.
- Exportar resultados a CSV y mapa HTML.
- Enviar alertas por email con mapa adjunto.

## Estructura

```text
.
├── app.py
├── alert_engine.py
├── requirements.txt
├── README.md
├── .gitignore
└── .streamlit/
    ├── config.toml
    └── secrets.example.toml
```

## Columnas esperadas en el Excel

Mínimas:

```text
LATITUD, LONGITUD, CULTIVO
```

Recomendadas:

```text
IT, ASEGURADO, PROVINCIA, DEPTO, LOCALIDAD, CULTIVO, CAMPO, HAS, MONEDA, SUMA_ASEGURADA, ADICIONALES, LATITUD, LONGITUD, CAMPAÑA
```

## Ejecutar localmente

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

En macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Configurar secrets localmente

Crear `.streamlit/secrets.toml` usando `.streamlit/secrets.example.toml` como base.

```toml
VISUALCROSSING_API_KEY = "tu_api_key"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_USER = "tu_email@gmail.com"
EMAIL_PASS = "tu_app_password"
DEFAULT_RECIPIENT = "destinatario@empresa.com"
```

## Deploy en Streamlit Cloud

1. Subir el repo a GitHub.
2. Ir a Streamlit Community Cloud.
3. Crear una nueva app desde el repo.
4. Main file path: `app.py`.
5. Cargar los secrets desde la sección **App settings > Secrets**.
6. Deploy.

## Seguridad

No subir nunca al repo:

- API keys.
- App passwords de Gmail.
- Excel reales con pólizas o datos sensibles.
- Archivos `.streamlit/secrets.toml`.

Si una credencial fue compartida o commiteada accidentalmente, rotarla inmediatamente.

## Nota de compatibilidad Streamlit / PyArrow

La app fuerza las columnas de texto/mixed object a string antes de mostrarlas con `st.dataframe`. Esto evita errores de PyArrow cuando el Excel trae columnas mixtas, por ejemplo `CAMPAÑA` con texto, números o valores vacíos.

También se incluye `runtime.txt` para sugerir Python 3.12 en Streamlit Cloud.

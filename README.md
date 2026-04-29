# Sistema de Alertas Meteorológicas para Pólizas Agrícolas

App Streamlit para cargar pólizas agrícolas desde Excel, filtrar campaña gruesa, consultar pronóstico 72h en Visual Crossing y generar alertas por lluvia, viento y baja temperatura.

## Archivos principales

- `app.py`: interfaz Streamlit.
- `alert_engine.py`: motor de normalización, consulta API, alertas, mapa y email.
- `requirements.txt`: dependencias.
- `.streamlit/secrets.example.toml`: ejemplo de secrets.
- `runtime.txt`: versión Python sugerida para Streamlit Cloud.

## Secrets requeridos

En Streamlit Cloud, cargar en **App settings > Secrets**:

```toml
VISUALCROSSING_API_KEY = "tu_api_key"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_USER = "tu_email@gmail.com"
EMAIL_PASS = "tu_app_password"
DEFAULT_RECIPIENT = "destinatario@empresa.com"
```

No subir `.streamlit/secrets.toml` al repo.

## Ejecución local

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Diagnóstico de API

Si el panel muestra `Sin dato API = total de pólizas`, no significa que no haya alertas. Significa que las consultas climáticas fallaron. Revisar la pestaña **Diagnóstico API**.

Estados comunes:

- `Falta VISUALCROSSING_API_KEY`: falta cargar el secret o la clave.
- `HTTP 401 / 403`: clave inválida, vencida o sin permisos.
- `HTTP 429`: límite de uso alcanzado o demasiadas consultas paralelas.
- `Timeout`: bajar consultas paralelas a 1 o 2 y reintentar.
- `Respuesta sin bloque 'days'`: la API respondió con un formato distinto al esperado.

Antes de ejecutar 200 pólizas, usar el botón **Probar API con 1 póliza**.

## Nota de deploy en Streamlit Cloud

Si Streamlit Cloud construye con Python 3.14 o 3.13, entrar en la app desde el dashboard, abrir Settings / Advanced settings y seleccionar Python 3.12. El archivo runtime.txt queda como referencia, pero en Community Cloud la selección confiable se hace desde la UI.

Recomendación de estructura del repo: dejar `app.py`, `alert_engine.py`, `requirements.txt`, `runtime.txt`, `.gitignore` y la carpeta `.streamlit/` directamente en la raíz del repositorio. En ese caso el Main file path es `app.py`.

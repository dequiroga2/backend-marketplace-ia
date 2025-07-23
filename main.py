import os
import httpx
import uvicorn
import firebase_admin
import time
from pydantic import BaseModel
import uuid

from fastapi import FastAPI, Depends, HTTPException, status, File, UploadFile, Form, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from firebase_admin import credentials, auth
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv

load_dotenv()

voices_cache = {"data": None, "timestamp": 0}
avatars_cache = {"data": None, "timestamp": 0}
CACHE_DURATION_SECONDS = 3600

cred = credentials.Certificate("firebase-credentials.json")

firebase_admin.initialize_app(cred)

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

N8N_SECRET_KEY = os.getenv("N8N_SECRET_KEY")

origins = [
    "http://localhost:5173",
    # Cuando despliegues tu frontend, añade su URL aquí
    # "https://tu-app.onrender.com", 
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security_scheme = HTTPBearer()

class N8NBody(BaseModel):
    message: str
    botId: str
    avatarId: str | None = None
    voiceId: str | None = None
    videoWidth: int | None = None
    videoHeight: int | None = None
    type: str

async def get_current_user(token: HTTPAuthorizationCredentials = Depends(security_scheme)):
    """
    Toma el token Bearer del header, lo verifica con Firebase y devuelve los
    datos del usuario si es válido. De lo contrario, lanza una excepción HTTP.
    """
    try:
        id_token = token.credentials
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token de autorización inválido: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
HEYGEN_API_URL = "https://api.heygen.com/v2"
HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY")
HEADERS = {"accept": "application/json", "x-api-key": HEYGEN_API_KEY}

async def verify_n8n_secret(x_n8n_secret: str | None = Header(None)):
    if not N8N_SECRET_KEY:
        raise HTTPException(status_code=500, detail="La clave secreta del servidor no está configurada.")
    if x_n8n_secret != N8N_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Acceso denegado: Clave secreta inválida.")
    return True

@app.post("/api/n8n")
async def proxy_to_n8n(
    body: N8NBody, # ✅ 2. Usa el nuevo modelo flexible
    user_data: dict = Depends(get_current_user)
):
    user_uid = user_data.get("uid")
    print(f"Petición de proxy a n8n para el usuario UID: {user_uid}")

    n8n_payload = body.dict()
    n8n_payload["uid"] = user_uid
    
    # Asegúrate de que esta es la URL correcta de tu webhook en n8n
    n8n_webhook_url = "https://automation.luminotest.com/webhook/53816d93-2be0-4df2-8dec-031847e0bed1"

    async with httpx.AsyncClient() as client:
        try:
            # ✅ 3. Espera y GUARDA la respuesta de n8n
            n8n_response = await client.post(n8n_webhook_url, json=n8n_payload, timeout=60.0)
            n8n_response.raise_for_status()
            response_text = n8n_response.text
            print(f"Respuesta cruda de n8n (texto): '{response_text}'")
            return n8n_response.text

        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Timeout: n8n tardó demasiado en responder.")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Error al contactar n8n: {str(e)}")
        

@app.post("/api/upload-image")
async def upload_image_from_n8n(
    uid: str = Form(...),
    image_file: UploadFile = File(...),
    is_secret_valid: bool = Depends(verify_n8n_secret)
):
    # Genera un nombre de archivo único para evitar sobreescrituras
    file_extension = os.path.splitext(image_file.filename)[1]
    unique_filename = f"{uuid.uuid4()}{file_extension}"
    file_path = f"static/images/{unique_filename}"

    # Guarda el contenido del archivo en el servidor
    with open(file_path, "wb") as buffer:
        buffer.write(await image_file.read())
    
    # Construye la URL pública completa. 
    # ❗️ ATENCIÓN: Necesitarás añadir la URL de tu backend como variable de entorno
    base_url = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000")
    full_image_url = f"{base_url}/{file_path}"
    
    print(f"Imagen guardada para UID {uid} en: {full_image_url}")

    # Devuelve la URL pública a n8n
    return {"imageUrl": full_image_url}



@app.get("/api/voices")
async def get_voices(user_data: dict = Depends(get_current_user)):
    """Endpoint proxy PÚBLICO para obtener las voces."""

    current_time = time.time()

    if voices_cache["data"] and (current_time - voices_cache["timestamp"] < CACHE_DURATION_SECONDS):
        print(f"Sirviendo voces DESDE CACHÉ para usuario {user_data.get('uid')}")
        return voices_cache["data"]
    
    print(f"Cache de voces expirado. Pidiendo a HeyGen para usuario {user_data.get('uid')}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{HEYGEN_API_URL}/voices", headers=HEADERS)
            response.raise_for_status()
            new_data = response.json()
            voices_cache["data"] = new_data
            voices_cache["timestamp"] = current_time
            
            return new_data
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Error al contactar la API de HeyGen: {str(e)}")


@app.get("/api/avatars")
async def get_avatars(user_data: dict = Depends(get_current_user)):
    """Endpoint proxy PÚBLICO para obtener los avatares."""

    current_time = time.time()

    if avatars_cache["data"] and (current_time - avatars_cache["timestamp"] < CACHE_DURATION_SECONDS):
        print(f"Sirviendo avatares DESDE CACHÉ para usuario {user_data.get('uid')}")
        return avatars_cache["data"]
    
    print(f"Cache de avatares expirado. Pidiendo a HeyGen para usuario {user_data.get('uid')}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{HEYGEN_API_URL}/avatars", headers=HEADERS)
            response.raise_for_status()
            new_data = response.json()
            avatars_cache["data"] = new_data
            avatars_cache["timestamp"] = current_time
            return new_data
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Error al contactar la API de HeyGen: {str(e)}")


    
@app.get("/api/me")
async def get_my_info(user_data: dict = Depends(get_current_user)):
    """
    Un endpoint protegido. `get_current_user` se ejecuta primero.
    Si el token es válido, los datos del usuario se inyectan en `user_data`.
    """
    # Ahora tienes acceso a toda la información del usuario desde el token
    uid = user_data.get("uid")
    email = user_data.get("email")

    return {"message": f"¡Hola, {email}! Tu UID de Firebase es: {uid}"}


@app.get("/")
async def root():
    return {"message": "Este es un endpoint público."}
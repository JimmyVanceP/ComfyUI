# handler.py
import runpod
import json
import requests
import time
import os
import subprocess
import base64
import urllib.parse

COMFYUI_URL = "http://127.0.0.1:8188"


def log_system_info():
    """Loggear información completa del sistema"""
    print("=" * 60)
    print("DEBUG: Verificando sistema de archivos")
    print("=" * 60)

    print("\n--- Verificando /runpod-volume (Serverless) ---")
    if os.path.exists("/runpod-volume"):
        print("/runpod-volume EXISTE")
        result = subprocess.run(["ls", "-la", "/runpod-volume"], capture_output=True, text=True)
        print(result.stdout)

        if os.path.exists("/runpod-volume/models"):
            print("\nContenido de /runpod-volume/models:")
            result = subprocess.run(["ls", "-la", "/runpod-volume/models"], capture_output=True, text=True)
            print(result.stdout)

            for subdir in ["checkpoints", "unet", "vae", "clip"]:
                path = f"/runpod-volume/models/{subdir}"
                if os.path.exists(path):
                    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
                    print(f"\n{path}:\n{result.stdout}")
    else:
        print("/runpod-volume NO EXISTE")

    print("\n--- Verificando /workspace (Pods) ---")
    if os.path.exists("/workspace"):
        print("/workspace EXISTE")
        result = subprocess.run(["ls", "-la", "/workspace/models/checkpoints"], capture_output=True, text=True)
        print(f"checkpoints: {result.stdout}")

    print("\n--- Verificando extra_model_paths.yaml ---")
    config_path = "/comfyui/extra_model_paths.yaml"
    if os.path.exists(config_path):
        print(f"Archivo existe en: {config_path}")
        with open(config_path, "r") as f:
            print(f"Contenido:\n{f.read()}")
    else:
        print(f"NO EXISTE: {config_path}")

    print("=" * 60)


def wait_for_comfyui():
    max_retries = 30
    for i in range(max_retries):
        try:
            response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
            if response.status_code == 200:
                print("ComfyUI listo")
                log_system_info()
                return True
        except:
            print(f"Esperando ComfyUI... {i+1}/{max_retries}")
            time.sleep(2)
    return False


def download_audio_from_comfyui(audio_info):
    """
    Descarga el archivo de audio desde el servidor local de ComfyUI
    y lo devuelve como bytes + metadata.
    """
    filename = audio_info.get("filename", "")
    subfolder = audio_info.get("subfolder", "")
    file_type = audio_info.get("type", "output")

    if not filename:
        return None, "No filename in audio_info"

    # Construir la URL del endpoint /view de ComfyUI
    params = {"filename": filename, "type": file_type}
    if subfolder:
        params["subfolder"] = subfolder

    view_url = f"{COMFYUI_URL}/view?{urllib.parse.urlencode(params)}"
    print(f"Descargando audio desde ComfyUI: {view_url}")

    try:
        audio_response = requests.get(view_url, timeout=120)

        if audio_response.status_code != 200:
            return None, f"ComfyUI /view devolvió HTTP {audio_response.status_code}"

        audio_bytes = audio_response.content
        file_size = len(audio_bytes)
        print(f"Audio descargado: {file_size} bytes ({file_size / 1024 / 1024:.2f} MB)")

        # Verificar que no está vacío
        if file_size < 1000:
            return None, f"Archivo de audio sospechosamente pequeño: {file_size} bytes"

        return audio_bytes, None

    except requests.exceptions.Timeout:
        return None, "Timeout descargando audio de ComfyUI"
    except Exception as e:
        return None, f"Error descargando audio: {str(e)}"


def handler(job):
    job_input = job.get("input", {})

    if not job_input.get("workflow"):
        return {"error": "Missing workflow"}

    workflow = job_input["workflow"]

    # Verificar todas las posibles ubicaciones del modelo
    possible_paths = [
        "/runpod-volume/models/checkpoints/ace_step_1.5_turbo_aio.safetensors",
        "/workspace/models/checkpoints/ace_step_1.5_turbo_aio.safetensors",
        "/comfyui/models/checkpoints/ace_step_1.5_turbo_aio.safetensors",
        "/runpod-volume/models/unet/ace_step_1.5_turbo_aio.safetensors",
        "/workspace/models/unet/ace_step_1.5_turbo_aio.safetensors"
    ]

    model_found = False
    for path in possible_paths:
        if os.path.exists(path):
            print(f"Modelo encontrado en: {path}")
            model_found = True
            break

    if not model_found:
        print("ERROR: Modelo no encontrado en ninguna ubicación estándar")
        log_system_info()
        return {"error": "Modelo ace_step_1.5_turbo_aio.safetensors no encontrado"}

    try:
        # Enviar workflow a ComfyUI
        response = requests.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
            timeout=30
        )

        if response.status_code != 200:
            return {"error": f"ComfyUI error: {response.text}"}

        prompt_data = response.json()
        prompt_id = prompt_data.get("prompt_id")

        if not prompt_id:
            return {"error": "No prompt_id received from ComfyUI"}

        print(f"Job iniciado en ComfyUI: {prompt_id}")

        # Polling del historial de ComfyUI
        max_wait = 600  # 10 minutos máximo
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                return {"error": f"Timeout después de {max_wait}s"}

            history_response = requests.get(
                f"{COMFYUI_URL}/history/{prompt_id}",
                timeout=10
            )

            if history_response.status_code == 200:
                history = history_response.json()

                if prompt_id in history:
                    job_data = history[prompt_id]

                    # Verificar si hubo error en ComfyUI
                    status_str = job_data.get("status", {}).get("status_str", "")
                    if status_str == "error":
                        return {
                            "error": "ComfyUI execution error",
                            "details": job_data.get("status", {})
                        }

                    outputs = job_data.get("outputs", {})
                    print(f"Outputs recibidos de ComfyUI: {json.dumps(outputs, default=str)[:500]}")

                    # Buscar audio en el nodo 8 (SaveAudioMP3)
                    if "8" in outputs:
                        node_output = outputs["8"]

                        if isinstance(node_output, dict) and "audio" in node_output:
                            audio_list = node_output["audio"]

                            if audio_list and len(audio_list) > 0:
                                audio_info = audio_list[0]
                                filename = audio_info.get("filename", "")
                                print(f"Audio encontrado: {json.dumps(audio_info)}")

                                # === CLAVE: Descargar el archivo localmente ===
                                audio_bytes, error = download_audio_from_comfyui(audio_info)

                                if error:
                                    return {
                                        "error": f"Error descargando audio: {error}",
                                        "audio_info": audio_info
                                    }

                                # Codificar en base64 para enviar en la respuesta
                                audio_b64 = base64.b64encode(audio_bytes).decode('utf-8')
                                print(f"Audio codificado en base64: {len(audio_b64)} caracteres")

                                return {
                                    "status": "success",
                                    "audio_base64": audio_b64,
                                    "filename": filename,
                                    "content_type": "audio/mpeg",
                                    "file_size": len(audio_bytes),
                                    "prompt_id": prompt_id
                                }

                    # Si llegamos aquí, no encontramos audio
                    # Verificar si hay otros nodos con output
                    if outputs:
                        return {
                            "error": "No se encontró audio en el nodo 8",
                            "available_outputs": list(outputs.keys()),
                            "outputs_preview": {k: str(v)[:200] for k, v in outputs.items()}
                        }

            time.sleep(2)

    except Exception as e:
        import traceback
        print(f"Exception en handler: {traceback.format_exc()}")
        return {"error": str(e)}


print("Iniciando worker ACE-STEP...")
if not wait_for_comfyui():
    print("WARNING: ComfyUI no respondió a tiempo")

runpod.serverless.start({"handler": handler})

# handler.py
import runpod
import json
import requests
import time
import os
import base64
from pathlib import Path

# URL de ComfyUI local
COMFYUI_URL = "http://127.0.0.1:8188"

def wait_for_comfyui():
    """Espera a que ComfyUI esté listo"""
    max_retries = 30
    for i in range(max_retries):
        try:
            response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
            if response.status_code == 200:
                print("ComfyUI está listo")
                return True
        except:
            print(f"Esperando a ComfyUI... intento {i+1}/{max_retries}")
            time.sleep(2)
    return False

def handler(job):
    """
    Handler para procesar workflows de ACE-STEP 1.5
    Recibe el workflow en job["input"]["workflow"]
    """
    job_input = job.get("input", {})
    
    # Validar que venga el workflow
    if not job_input.get("workflow"):
        return {"error": "Missing 'workflow' in input"}
    
    workflow = job_input["workflow"]
    
    try:
        # Enviar el workflow a ComfyUI
        print(f"Enviando workflow a ComfyUI...")
        prompt_response = requests.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
            timeout=30
        )
        
        if prompt_response.status_code != 200:
            return {
                "error": f"ComfyUI error: {prompt_response.text}",
                "status_code": prompt_response.status_code
            }
        
        prompt_data = prompt_response.json()
        prompt_id = prompt_data.get("prompt_id")
        
        if not prompt_id:
            return {"error": "No se recibió prompt_id de ComfyUI"}
        
        print(f"Job iniciado con prompt_id: {prompt_id}")
        
        # Polling: esperar a que termine el procesamiento
        max_wait = 600  # 10 minutos máximo
        start_time = time.time()
        
        while True:
            # Verificar timeout
            if time.time() - start_time > max_wait:
                return {"error": "Timeout: La generación tardó más de 10 minutos"}
            
            # Consultar historial
            history_response = requests.get(
                f"{COMFYUI_URL}/history/{prompt_id}",
                timeout=10
            )
            
            if history_response.status_code == 200:
                history = history_response.json()
                
                if prompt_id in history:
                    # Job completado
                    job_data = history[prompt_id]
                    
                    # Verificar si hubo errores
                    if job_data.get("status", {}).get("status_str") == "error":
                        return {
                            "error": "ComfyUI workflow error",
                            "details": job_data.get("status", {})
                        }
                    
                    # Extraer outputs
                    outputs = job_data.get("outputs", {})
                    
                    # Buscar el audio generado (nodo 8: SaveAudioMP3)
                    audio_url = extract_audio_from_outputs(outputs)
                    
                    if audio_url:
                        return {
                            "status": "success",
                            "audio_url": audio_url,
                            "prompt_id": prompt_id
                        }
                    else:
                        return {
                            "error": "No se encontró audio en los outputs",
                            "outputs": outputs
                        }
            
            # Esperar antes del siguiente poll
            time.sleep(2)
            
    except requests.exceptions.Timeout:
        return {"error": "Timeout al comunicarse con ComfyUI"}
    except Exception as e:
        return {"error": f"Error inesperado: {str(e)}"}

def extract_audio_from_outputs(outputs):
    """
    Extrae la URL o datos del audio de los outputs de ComfyUI
    Busca específicamente en el nodo 8 (SaveAudioMP3)
    """
    
    # Buscar en el nodo 8 (SaveAudioMP3)
    if "8" in outputs:
        node_output = outputs["8"]
        
        # Puede venir como dict con 'audio'
        if isinstance(node_output, dict) and "audio" in node_output:
            audio_list = node_output["audio"]
            if isinstance(audio_list, list) and len(audio_list) > 0:
                audio_info = audio_list[0]
                
                # Construir URL para descargar el archivo
                filename = audio_info.get("filename")
                subfolder = audio_info.get("subfolder", "")
                type_dir = audio_info.get("type", "output")
                
                if filename:
                    # URL para acceder al archivo vía API de ComfyUI
                    url = f"{COMFYUI_URL}/view?filename={filename}&subfolder={subfolder}&type={type_dir}"
                    return url
    
    # Buscar en cualquier otro nodo que tenga audio
    for node_id, node_output in outputs.items():
        if isinstance(node_output, dict) and "audio" in node_output:
            audio_list = node_output["audio"]
            if isinstance(audio_list, list) and len(audio_list) > 0:
                audio_info = audio_list[0]
                filename = audio_info.get("filename")
                if filename:
                    url = f"{COMFYUI_URL}/view?filename={filename}"
                    return url
    
    return None

# Inicializar ComfyUI al arrancar el worker
print("Iniciando ComfyUI...")
if not wait_for_comfyui():
    print("WARNING: ComfyUI no respondió en el tiempo esperado")

# Iniciar el serverless
runpod.serverless.start({"handler": handler})

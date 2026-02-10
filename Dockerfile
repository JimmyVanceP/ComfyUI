# Dockerfile
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

# Variables de entorno
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    git \
    wget \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Clonar ComfyUI versión 0.12.3 oficial
WORKDIR /comfyui
RUN git clone https://github.com/Comfy-Org/ComfyUI.git . && \
    git checkout v0.12.3

# Instalar dependencias de Python para ComfyUI
RUN pip install --no-cache-dir -r requirements.txt

# Instalar RunPod SDK
RUN pip install --no-cache-dir runpod

# ============================================
# CONFIGURACIÓN PARA NETWORK VOLUME (Serverless)
# ============================================
# Crear archivo de configuración para que ComfyUI busque en /runpod-volume
RUN echo "comfyui:" > /comfyui/extra_model_paths.yaml && \
    echo "  base_path: /runpod-volume" >> /comfyui/extra_model_paths.yaml && \
    echo "  checkpoints: models/checkpoints/" >> /comfyui/extra_model_paths.yaml && \
    echo "  clip: models/clip/" >> /comfyui/extra_model_paths.yaml && \
    echo "  vae: models/vae/" >> /comfyui/extra_model_paths.yaml && \
    echo "  unet: models/unet/" >> /comfyui/extra_model_paths.yaml

# Crear symlinks por si acaso (para compatibilidad)
RUN ln -s /comfyui/models /workspace/models 2>/dev/null || true

# Copiar el handler
COPY handler.py /handler.py

# Crear script de inicio con verificación completa
RUN printf '#!/bin/bash\n\
echo "========================================"\n\
echo "INICIANDO WORKER ACE-STEP"\n\
echo "========================================"\n\
sleep 2\n\
echo "--- Verificando /runpod-volume ---"\n\
if [ -d "/runpod-volume/models/checkpoints" ]; then\n\
    echo "Contenido de /runpod-volume/models/checkpoints:"\n\
    ls -la /runpod-volume/models/checkpoints/\nelse\n\
    echo "/runpod-volume/models/checkpoints no existe o está vacío"\n\
fi\n\
echo "--- Verificando modelo embebido ---"\n\
if [ -f "/comfyui/models/checkpoints/ace_step_1.5_turbo_aio.safetensors" ]; then\n\
    echo "OK: Modelo embebido encontrado"\n\
    ls -lh /comfyui/models/checkpoints/ace_step_1.5_turbo_aio.safetensors\n\
else\n\
    echo "ADVERTENCIA: Modelo embebido no encontrado"\n\
fi\n\
echo "--- Contenido extra_model_paths.yaml ---"\ncat /comfyui/extra_model_paths.yaml\n\
echo "========================================"\n\
echo "Iniciando ComfyUI..."\n\
cd /comfyui && python main.py --listen 0.0.0.0 --port 8188 --preview-method auto &\nsleep 15\n\
echo "Iniciando handler..."\n\
python /handler.py\n' > /start.sh && chmod +x /start.sh

EXPOSE 8188
CMD ["/bin/bash", "/start.sh"]

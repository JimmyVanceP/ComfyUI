# Dockerfile
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

# Variables de entorno
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Activar debug para ver qué encuentra (opcional pero útil)
ENV NETWORK_VOLUME_DEBUG=true

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    git \
    wget \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Clonar ComfyUI versión 0.12.3 oficial (Comfy-Org)
WORKDIR /comfyui
RUN git clone https://github.com/Comfy-Org/ComfyUI.git . && \
    git checkout v0.12.3

# Instalar dependencias de Python para ComfyUI
RUN pip install --no-cache-dir -r requirements.txt

# Instalar RunPod SDK
RUN pip install --no-cache-dir runpod

# ============================================
# CONFIGURACIÓN CORRECTA PARA SERVERLESS
# ============================================
# IMPORTANTE: En Serverless el volumen está en /runpod-volume, no en /workspace
RUN echo "comfyui:" > /comfyui/extra_model_paths.yaml && \
    echo "  base_path: /runpod-volume" >> /comfyui/extra_model_paths.yaml && \
    echo "  checkpoints: models/checkpoints/" >> /comfyui/extra_model_paths.yaml && \
    echo "  clip: models/clip/" >> /comfyui/extra_model_paths.yaml && \
    echo "  clip_vision: models/clip_vision/" >> /comfyui/extra_model_paths.yaml && \
    echo "  configs: models/configs/" >> /comfyui/extra_model_paths.yaml && \
    echo "  controlnet: models/controlnet/" >> /comfyui/extra_model_paths.yaml && \
    echo "  diffusion_models: models/diffusion_models/" >> /comfyui/extra_model_paths.yaml && \
    echo "  embeddings: models/embeddings/" >> /comfyui/extra_model_paths.yaml && \
    echo "  loras: models/loras/" >> /comfyui/extra_model_paths.yaml && \
    echo "  upscale_models: models/upscale_models/" >> /comfyui/extra_model_paths.yaml && \
    echo "  vae: models/vae/" >> /comfyui/extra_model_paths.yaml && \
    echo "  unet: models/unet/" >> /comfyui/extra_model_paths.yaml && \
    echo "  gligen: models/gligen/" >> /comfyui/extra_model_paths.yaml && \
    echo "  hypernetworks: models/hypernetworks/" >> /comfyui/extra_model_paths.yaml && \
    echo "  style_models: models/style_models/" >> /comfyui/extra_model_paths.yaml && \
    echo "  t2i_adapter: models/t2i_adapter/" >> /comfyui/extra_model_paths.yaml

# Crear estructura de directorios en /runpod-volume (por si acaso)
RUN mkdir -p /runpod-volume/models/checkpoints && \
    mkdir -p /runpod-volume/models/unet && \
    mkdir -p /runpod-volume/models/vae && \
    mkdir -p /runpod-volume/models/clip && \
    mkdir -p /runpod-volume/output

# Copiar el handler
COPY handler.py /handler.py

# Crear script de inicio
RUN printf '#!/bin/bash\n\
echo "========================================"\n\
echo "INICIANDO WORKER ACE-STEP"\n\
echo "========================================"\n\
echo "Esperando 3 segundos por si el volumen tarda..."\n\
sleep 3\n\
echo "Verificando montaje de Network Volume..."\n\
if [ -d "/runpod-volume" ]; then\n\
    echo "OK: /runpod-volume existe"\n\
    if [ -d "/runpod-volume/models" ]; then\n\
        echo "OK: /runpod-volume/models existe"\n\
        echo "Contenido de checkpoints:"\n\
        ls -la /runpod-volume/models/checkpoints/ 2>/dev/null || echo "checkpoints vacío o no accesible"\n\
        echo "Contenido de unet:"\n\
        ls -la /runpod-volume/models/unet/ 2>/dev/null || echo "unet vacío o no accesible"\n\
    else\n\
        echo "ADVERTENCIA: /runpod-volume/models no existe"\n\
        echo "Contenido de /runpod-volume:"\n\
        ls -la /runpod-volume/\n\
    fi\n\
else\n\
    echo "ERROR: /runpod-volume no existe - Verifica que el Network Volume esté adjunto al endpoint"\n\
fi\n\
echo "========================================"\n\
echo "Iniciando ComfyUI v0.12.3..."\n\
cd /comfyui && python main.py --listen 0.0.0.0 --port 8188 --preview-method auto &\necho "Esperando 15 segundos..."\nsleep 15\n\
echo "Iniciando handler..."\n\
python /handler.py\n' > /start.sh && chmod +x /start.sh

# Exponer puertos
EXPOSE 8188

# Comando de inicio
CMD ["/bin/bash", "/start.sh"]

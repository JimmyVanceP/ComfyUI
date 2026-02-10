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

# Clonar ComfyUI versión 0.12.3 oficial (Comfy-Org)
WORKDIR /comfyui
RUN git clone https://github.com/Comfy-Org/ComfyUI.git . && \
    git checkout v0.12.3

# Instalar dependencias de Python para ComfyUI
RUN pip install --no-cache-dir -r requirements.txt

# Instalar RunPod SDK
RUN pip install --no-cache-dir runpod

# ============================================
# CONFIGURACIÓN PARA NETWORK VOLUME (Solución 1)
# ============================================
# Crear el archivo de configuración para que ComfyUI busque modelos en /workspace
# en lugar de en /comfyui/models/
RUN echo -e "comfyui:\n  base_path: /workspace\n  checkpoints: models/checkpoints/\n  clip: models/clip/\n  clip_vision: models/clip_vision/\n  configs: models/configs/\n  controlnet: models/controlnet/\n  diffusion_models: models/diffusion_models/\n  embeddings: models/embeddings/\n  loras: models/loras/\n  upscale_models: models/upscale_models/\n  vae: models/vae/\n  unet: models/unet/\n  gligen: models/gligen/\n  hypernetworks: models/hypernetworks/\n  style_models: models/style_models/\n  t2i_adapter: models/t2i_adapter/" > /comfyui/extra_model_paths.yaml

# Crear los directorios en /workspace por si acaso (aunque el Network Volume debería crearlos)
RUN mkdir -p /workspace/models/{checkpoints,clip,clip_vision,configs,controlnet,diffusion_models,embeddings,loras,upscale_models,vae,unet,gligen,hypernetworks,style_models,t2i_adapter}

# Copiar el handler
COPY handler.py /handler.py

# Crear directorio para outputs (también en /workspace para persistencia)
RUN mkdir -p /workspace/output

# Crear script de inicio (CORREGIDO)
RUN printf '#!/bin/bash\n\
echo "Verificando montaje de Network Volume..."\n\
if [ -d "/workspace/models" ]; then\n\
    echo "Network Volume detectado en /workspace/models"\n\
    echo "Contenido de checkpoints:"\n\
    ls -la /workspace/models/checkpoints/ 2>/dev/null || echo "Carpeta checkpoints vacía o no accesible"\n\
else\n\
    echo "ADVERTENCIA: No se detectó Network Volume en /workspace/models"\n\
fi\n\
echo "Iniciando ComfyUI v0.12.3..."\n\
cd /comfyui && python main.py --listen 0.0.0.0 --port 8188 --preview-method auto &\n\
echo "Esperando a que ComfyUI esté listo..."\n\
sleep 15\n\
echo "Iniciando RunPod handler..."\n\
python /handler.py\n' > /start.sh && chmod +x /start.sh

# Exponer puertos
EXPOSE 8188

# Comando de inicio
CMD ["/bin/bash", "/start.sh"]

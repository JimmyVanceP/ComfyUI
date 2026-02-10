# Partimos de una imagen base de RunPod con Python y CUDA
FROM runpod/base:0.4.0-cuda11.8.0

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

# Clonar ComfyUI versión 0.12.3
WORKDIR /comfyui
RUN git clone https://github.com/Comfy-Org/ComfyUI.git . && \
    git checkout v0.12.3

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Instalar el handler de RunPod para serverless
RUN pip install runpod

# Copiar el handler personalizado (necesitarás crear este archivo)
COPY handler.py /handler.py

# Exponer puerto
EXPOSE 8188

# Comando para iniciar
CMD ["python", "/handler.py"]

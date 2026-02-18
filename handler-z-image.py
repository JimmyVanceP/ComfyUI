import base64
import json
import os
import subprocess
import time
import urllib.parse

import requests
import runpod

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

# Expected z-image files used by the workflow shipped in WordPress backend.
EXPECTED_MODELS = {
    "unet": "z_image_turbo_bf16.safetensors",
    "clip": "qwen_3_4b.safetensors",
    "vae": "ae.safetensors",
}


def list_dir(path):
    if not os.path.exists(path):
        return f"{path} (missing)"
    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
    return result.stdout.strip() or f"{path} (empty)"


def check_expected_models():
    base_paths = ["/runpod-volume/models", "/workspace/models", "/comfyui/models"]
    found = {}
    missing = []

    for model_type, filename in EXPECTED_MODELS.items():
        located = None
        for base in base_paths:
            candidate = f"{base}/{model_type}/{filename}"
            if os.path.exists(candidate):
                located = candidate
                break
        if located:
            found[model_type] = located
        else:
            missing.append(f"{model_type}/{filename}")

    return found, missing


def log_startup_diagnostics():
    print("=" * 80)
    print("DEBUG: startup diagnostics")
    print("=" * 80)
    print(list_dir("/runpod-volume"))
    print(list_dir("/runpod-volume/models"))
    print(list_dir("/runpod-volume/models/unet"))
    print(list_dir("/runpod-volume/models/clip"))
    print(list_dir("/runpod-volume/models/vae"))
    print(list_dir("/comfyui/models/unet"))
    print(list_dir("/comfyui/models/clip"))
    print(list_dir("/comfyui/models/vae"))
    print(list_dir("/workspace/models"))

    extra_paths_file = "/comfyui/extra_model_paths.yaml"
    if os.path.exists(extra_paths_file):
        with open(extra_paths_file, "r", encoding="utf-8") as f:
            print("--- extra_model_paths.yaml ---")
            print(f.read())
    else:
        print(f"{extra_paths_file} (missing)")
    print("=" * 80)


def wait_for_comfyui(max_retries=90, delay_seconds=2):
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=5)
            if response.status_code == 200:
                print("ComfyUI is ready.")
                return True
        except Exception:
            pass

        print(f"Waiting for ComfyUI... {attempt}/{max_retries}")
        time.sleep(delay_seconds)

    return False


def extract_first_image_info(outputs, preferred_nodes=None):
    if not isinstance(outputs, dict):
        return None, None

    ordered_nodes = []
    if preferred_nodes:
        ordered_nodes.extend([str(node_id) for node_id in preferred_nodes])
    ordered_nodes.extend([node_id for node_id in outputs.keys() if str(node_id) not in ordered_nodes])

    for node_id in ordered_nodes:
        node_output = outputs.get(node_id)
        if not isinstance(node_output, dict):
            continue

        images = node_output.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                return first, str(node_id)

    return None, None


def download_image_from_comfyui(image_info):
    filename = image_info.get("filename", "")
    subfolder = image_info.get("subfolder", "")
    image_type = image_info.get("type", "output")

    if not filename:
        return None, None, "Missing filename in ComfyUI image output"

    params = {"filename": filename, "type": image_type}
    if subfolder:
        params["subfolder"] = subfolder

    view_url = f"{COMFYUI_URL}/view?{urllib.parse.urlencode(params)}"
    print(f"Downloading image from ComfyUI: {view_url}")

    try:
        response = requests.get(view_url, timeout=120)
    except requests.exceptions.Timeout:
        return None, None, "Timeout downloading image from ComfyUI /view"
    except Exception as exc:
        return None, None, f"Error downloading image from ComfyUI /view: {exc}"

    if response.status_code != 200:
        return None, None, f"ComfyUI /view returned HTTP {response.status_code}"

    image_bytes = response.content
    if not image_bytes or len(image_bytes) < 1000:
        return None, None, f"Downloaded image is too small ({len(image_bytes)} bytes)"

    content_type = response.headers.get("Content-Type", "image/png").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        content_type = "image/png"

    return image_bytes, content_type, None


def handler(job):
    try:
        job_input = job.get("input", {})
        workflow = job_input.get("workflow")
        if not workflow:
            return {"error": "Missing workflow in job.input"}

        preferred_nodes = job_input.get("output_node_ids", ["9"])
        max_wait = int(job_input.get("max_wait", 300))

        response = requests.post(
            f"{COMFYUI_URL}/prompt",
            json={"prompt": workflow},
            timeout=30,
        )
        if response.status_code != 200:
            return {"error": f"ComfyUI /prompt failed: {response.text}"}

        prompt_data = response.json()
        prompt_id = prompt_data.get("prompt_id")
        if not prompt_id:
            return {"error": "No prompt_id returned by ComfyUI"}

        print(f"ComfyUI prompt submitted: {prompt_id}")
        started = time.time()

        while True:
            elapsed = time.time() - started
            if elapsed > max_wait:
                return {"error": f"Timeout after {max_wait}s waiting for ComfyUI", "prompt_id": prompt_id}

            history_response = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=10)
            if history_response.status_code != 200:
                time.sleep(1.5)
                continue

            history = history_response.json()
            if prompt_id not in history:
                time.sleep(1.5)
                continue

            job_data = history[prompt_id]
            status_str = str(job_data.get("status", {}).get("status_str", "")).lower()
            if status_str == "error":
                return {
                    "error": "ComfyUI execution error",
                    "details": job_data.get("status", {}),
                    "prompt_id": prompt_id,
                }

            outputs = job_data.get("outputs", {})
            image_info, image_node_id = extract_first_image_info(outputs, preferred_nodes)

            if image_info:
                image_bytes, content_type, error = download_image_from_comfyui(image_info)
                if error:
                    return {
                        "error": error,
                        "prompt_id": prompt_id,
                        "image_info": image_info,
                    }

                image_b64 = base64.b64encode(image_bytes).decode("utf-8")
                resolved_seed = job_input.get("seed")

                return {
                    "status": "success",
                    "prompt_id": prompt_id,
                    "seed": resolved_seed,
                    "node_id": image_node_id,
                    "filename": image_info.get("filename"),
                    "content_type": content_type,
                    "file_size": len(image_bytes),
                    "image_base64": image_b64,
                }

            # If history exists but no images yet, keep polling until timeout.
            time.sleep(1.5)

    except Exception as exc:
        import traceback

        print("Unhandled handler exception:")
        print(traceback.format_exc())
        return {"error": str(exc)}


print("Starting RunPod image worker (flataipro / ComfyUI)...")
if not wait_for_comfyui():
    print("WARNING: ComfyUI did not become ready before worker start.")

found_models, missing_models = check_expected_models()
if missing_models:
    print(f"WARNING: missing expected models: {missing_models}")
else:
    print(f"All expected models located: {found_models}")

log_startup_diagnostics()
runpod.serverless.start({"handler": handler})

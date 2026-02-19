import base64
import io
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests
import runpod

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

def _read_int_env(key, default, min_value=0):
    try:
        value = int(os.environ.get(key, str(default)))
        if value < min_value:
            return default
        return value
    except Exception:
        return default


# Expected model files for workflows used from the WordPress backend.
EXPECTED_MODEL_GROUPS = {
    "z_image_flataipro": [
        "unet/z_image_turbo_bf16.safetensors",
        "clip/qwen_3_4b.safetensors",
        "vae/ae.safetensors",
    ],
    "flux2_klein9b_edit": [
        "unet/flux-2-klein-9b.safetensors",
        "clip/qwen_3_8b_fp8mixed.safetensors",
        "vae/flux2-vae.safetensors",
        "loras/klein_snofs_v1_1.safetensors",
    ],
}

# Output tuning to reduce transfer size from endpoint -> WordPress frontend.
# You can override in RunPod env vars:
# - OUTPUT_IMAGE_FORMAT=JPEG|WEBP
# - OUTPUT_IMAGE_QUALITY=1..100
OUTPUT_IMAGE_FORMAT = os.environ.get("OUTPUT_IMAGE_FORMAT", "JPEG").strip().upper()
try:
    OUTPUT_IMAGE_QUALITY = int(os.environ.get("OUTPUT_IMAGE_QUALITY", "82"))
except Exception:
    OUTPUT_IMAGE_QUALITY = 82

COMFY_OUTPUT_DIR = os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output").strip() or "/comfyui/output"
COMFY_INPUT_DIR = os.environ.get("COMFY_INPUT_DIR", "/comfyui/input").strip() or "/comfyui/input"
OUTPUT_CLEANUP_MAX_AGE_SECONDS = _read_int_env("OUTPUT_CLEANUP_MAX_AGE_SECONDS", 3600, 1)
OUTPUT_CLEANUP_MIN_INTERVAL_SECONDS = _read_int_env("OUTPUT_CLEANUP_MIN_INTERVAL_SECONDS", 300, 1)
INPUT_IMAGE_MAX_BYTES = _read_int_env("INPUT_IMAGE_MAX_BYTES", 15 * 1024 * 1024, 1024)

LAST_OUTPUT_CLEANUP_TS = 0.0

try:
    from PIL import Image
except Exception:
    Image = None


def list_dir(path):
    if not os.path.exists(path):
        return f"{path} (missing)"
    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
    return result.stdout.strip() or f"{path} (empty)"


def cleanup_old_files_in_dir(base_dir, cutoff_ts):
    stats = {
        "scanned_files": 0,
        "deleted_files": 0,
        "deleted_dirs": 0,
        "deleted_bytes": 0,
        "errors": 0,
    }

    if not os.path.isdir(base_dir):
        return stats

    for root, _, files in os.walk(base_dir):
        for filename in files:
            file_path = os.path.join(root, filename)
            try:
                stats["scanned_files"] += 1
                file_stat = os.stat(file_path)
                if file_stat.st_mtime >= cutoff_ts:
                    continue
                stats["deleted_bytes"] += file_stat.st_size
                os.remove(file_path)
                stats["deleted_files"] += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                stats["errors"] += 1
                print(f"[Cleanup] Failed deleting {file_path}: {exc}")

    # Remove empty subfolders after file cleanup.
    for root, _, _ in os.walk(base_dir, topdown=False):
        if root == base_dir:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
                stats["deleted_dirs"] += 1
        except Exception:
            continue

    return stats


def cleanup_old_comfy_outputs(force=False):
    global LAST_OUTPUT_CLEANUP_TS

    now = time.time()
    if (
        not force
        and (now - LAST_OUTPUT_CLEANUP_TS) < OUTPUT_CLEANUP_MIN_INTERVAL_SECONDS
    ):
        return

    LAST_OUTPUT_CLEANUP_TS = now

    cutoff_ts = now - OUTPUT_CLEANUP_MAX_AGE_SECONDS
    output_stats = cleanup_old_files_in_dir(COMFY_OUTPUT_DIR, cutoff_ts)
    input_stats = cleanup_old_files_in_dir(COMFY_INPUT_DIR, cutoff_ts)

    print(
        "[Cleanup] Periodic output cleanup completed. "
        f"output_dir={COMFY_OUTPUT_DIR}, "
        f"input_dir={COMFY_INPUT_DIR}, "
        f"older_than_s={OUTPUT_CLEANUP_MAX_AGE_SECONDS}, "
        f"scanned_files={output_stats['scanned_files'] + input_stats['scanned_files']}, "
        f"deleted_files={output_stats['deleted_files'] + input_stats['deleted_files']}, "
        f"deleted_dirs={output_stats['deleted_dirs'] + input_stats['deleted_dirs']}, "
        f"freed_mb={(output_stats['deleted_bytes'] + input_stats['deleted_bytes']) / (1024 * 1024):.2f}, "
        f"errors={output_stats['errors'] + input_stats['errors']}"
    )


def check_expected_models():
    base_paths = ["/runpod-volume/models", "/workspace/models", "/comfyui/models"]
    found_by_group = {}
    missing_by_group = {}

    for group_name, relative_paths in EXPECTED_MODEL_GROUPS.items():
        group_found = {}
        group_missing = []
        for relative_path in relative_paths:
            located = None
            for base in base_paths:
                candidate = os.path.join(base, relative_path)
                if os.path.exists(candidate):
                    located = candidate
                    break
            if located:
                group_found[relative_path] = located
            else:
                group_missing.append(relative_path)

        if group_found:
            found_by_group[group_name] = group_found
        if group_missing:
            missing_by_group[group_name] = group_missing

    return found_by_group, missing_by_group


def log_startup_diagnostics():
    print("=" * 80)
    print("DEBUG: startup diagnostics")
    print("=" * 80)
    print(list_dir("/runpod-volume"))
    print(list_dir("/runpod-volume/models"))
    print(list_dir("/runpod-volume/models/unet"))
    print(list_dir("/runpod-volume/models/clip"))
    print(list_dir("/runpod-volume/models/vae"))
    print(list_dir("/runpod-volume/models/loras"))
    print(list_dir("/comfyui/models/unet"))
    print(list_dir("/comfyui/models/clip"))
    print(list_dir("/comfyui/models/vae"))
    print(list_dir("/comfyui/models/loras"))
    print(list_dir(COMFY_INPUT_DIR))
    print(list_dir(COMFY_OUTPUT_DIR))
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


def compress_image_bytes(image_bytes, content_type):
    if not image_bytes or len(image_bytes) < 1000:
        return image_bytes, content_type, None

    if Image is None:
        return image_bytes, content_type, "Pillow not available, skipping compression"

    target_quality = max(1, min(100, int(OUTPUT_IMAGE_QUALITY)))
    target_format = OUTPUT_IMAGE_FORMAT if OUTPUT_IMAGE_FORMAT in {"JPEG", "JPG", "WEBP"} else "JPEG"

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            has_alpha = "A" in img.getbands()
            out = io.BytesIO()

            if target_format in {"JPEG", "JPG"}:
                # JPEG does not support alpha channel.
                if has_alpha:
                    base = Image.new("RGB", img.size, (255, 255, 255))
                    alpha = img.split()[-1]
                    base.paste(img, mask=alpha)
                    img_to_save = base
                else:
                    img_to_save = img.convert("RGB")

                img_to_save.save(
                    out,
                    format="JPEG",
                    quality=target_quality,
                    optimize=True,
                    progressive=True,
                )
                new_content_type = "image/jpeg"
            else:
                # WEBP handles RGB/RGBA and usually compresses better.
                img_to_save = img.convert("RGBA" if has_alpha else "RGB")
                img_to_save.save(
                    out,
                    format="WEBP",
                    quality=target_quality,
                    method=6,
                )
                new_content_type = "image/webp"

            compressed = out.getvalue()
            if not compressed:
                return image_bytes, content_type, "Compression produced empty payload"

            return compressed, new_content_type, None
    except Exception as exc:
        return image_bytes, content_type, f"Compression failed: {exc}"


def _sanitize_input_filename(filename, default_name):
    if not isinstance(filename, str) or not filename.strip():
        return default_name

    safe_name = os.path.basename(filename.strip())
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name).lstrip(".")
    if not safe_name:
        return default_name
    return safe_name[:180]


def _decode_image_data_uri(data_uri):
    if not isinstance(data_uri, str) or not data_uri.strip():
        return None, None, "Missing image data URI"

    match = re.match(
        r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$",
        data_uri.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, None, "Invalid data URI format"

    content_type = match.group(1).lower()
    encoded_payload = re.sub(r"\s+", "", match.group(2))
    try:
        image_bytes = base64.b64decode(encoded_payload, validate=True)
    except Exception:
        return None, None, "Invalid base64 image payload"

    if not image_bytes or len(image_bytes) < 32:
        return None, None, "Decoded image payload is too small"
    if len(image_bytes) > INPUT_IMAGE_MAX_BYTES:
        return None, None, f"Input image exceeds max size ({INPUT_IMAGE_MAX_BYTES} bytes)"

    return image_bytes, content_type, None


def _content_type_to_extension(content_type):
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        "image/bmp": "bmp",
    }
    return mapping.get(str(content_type).lower(), "png")


def prepare_job_input_images(input_images):
    if not input_images:
        return [], None

    if not isinstance(input_images, list):
        return [], "job.input.input_images must be an array"

    try:
        os.makedirs(COMFY_INPUT_DIR, exist_ok=True)
    except Exception as exc:
        return [], f"Failed to create ComfyUI input dir '{COMFY_INPUT_DIR}': {exc}"

    prepared = []
    for index, item in enumerate(input_images):
        if not isinstance(item, dict):
            return [], f"input_images[{index}] must be an object"

        data_uri = (
            item.get("data_uri")
            or item.get("image_data_uri")
            or item.get("imageDataUri")
            or item.get("image")
        )
        image_bytes, content_type, decode_error = _decode_image_data_uri(data_uri)
        if decode_error:
            return [], f"input_images[{index}] {decode_error}"

        extension = _content_type_to_extension(content_type)
        default_name = f"job-input-{int(time.time())}-{index}.{extension}"
        filename = _sanitize_input_filename(item.get("filename"), default_name)
        if "." not in filename:
            filename = f"{filename}.{extension}"

        file_path = os.path.join(COMFY_INPUT_DIR, filename)
        try:
            with open(file_path, "wb") as file_handle:
                file_handle.write(image_bytes)
        except Exception as exc:
            return [], f"Failed writing input image '{filename}': {exc}"

        prepared_item = {
            "filename": filename,
            "path": file_path,
            "bytes": len(image_bytes),
            "content_type": content_type,
            "node_id": item.get("node_id"),
            "node_field": item.get("node_field", "image"),
        }
        prepared.append(prepared_item)
        print(
            "Prepared input image for workflow: "
            f"filename={filename}, bytes={len(image_bytes)}, content_type={content_type}"
        )

    return prepared, None


def inject_input_images_into_workflow(workflow, prepared_images):
    if not isinstance(workflow, dict) or not prepared_images:
        return

    for prepared in prepared_images:
        filename = prepared["filename"]
        target_node_id = prepared.get("node_id")
        target_field = str(prepared.get("node_field") or "image")

        # Explicit node mapping takes priority.
        if target_node_id is not None:
            workflow_node = workflow.get(str(target_node_id))
            if isinstance(workflow_node, dict):
                node_inputs = workflow_node.setdefault("inputs", {})
                if isinstance(node_inputs, dict):
                    node_inputs[target_field] = filename
                    print(
                        "Mapped input image to workflow node: "
                        f"node={target_node_id}, field={target_field}, file={filename}"
                    )
                    continue

        # Fallback: first LoadImage node found in workflow.
        for node_id, workflow_node in workflow.items():
            if not isinstance(workflow_node, dict):
                continue
            if workflow_node.get("class_type") != "LoadImage":
                continue
            node_inputs = workflow_node.setdefault("inputs", {})
            if isinstance(node_inputs, dict):
                node_inputs["image"] = filename
                print(
                    "Mapped input image to first LoadImage node: "
                    f"node={node_id}, file={filename}"
                )
                break


def handler(job):
    try:
        cleanup_old_comfy_outputs()

        job_input = job.get("input", {})
        workflow = job_input.get("workflow")
        if not workflow:
            return {"error": "Missing workflow in job.input"}

        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except Exception as exc:
                return {"error": f"Workflow is not valid JSON: {exc}"}

        if not isinstance(workflow, dict):
            return {"error": "Workflow must be an object/map"}

        prepared_images, prepare_error = prepare_job_input_images(job_input.get("input_images", []))
        if prepare_error:
            return {"error": prepare_error}
        inject_input_images_into_workflow(workflow, prepared_images)

        preferred_nodes = job_input.get("output_node_ids", ["9"])
        if not isinstance(preferred_nodes, list) or not preferred_nodes:
            preferred_nodes = ["9"]
        preferred_nodes = [str(node_id) for node_id in preferred_nodes]

        try:
            max_wait = max(30, int(job_input.get("max_wait", 300)))
        except Exception:
            max_wait = 300

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

                original_size = len(image_bytes)
                compressed_bytes, compressed_type, compression_note = compress_image_bytes(image_bytes, content_type)
                if compression_note:
                    print(f"Compression note: {compression_note}")
                image_bytes = compressed_bytes
                content_type = compressed_type
                print(
                    f"Image size bytes: original={original_size}, final={len(image_bytes)}, "
                    f"format={content_type}, quality={OUTPUT_IMAGE_QUALITY}"
                )

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


print("Starting RunPod image worker (flataipro z-image + flux2 klein9b edit / ComfyUI)...")
if not wait_for_comfyui():
    print("WARNING: ComfyUI did not become ready before worker start.")

found_models, missing_models = check_expected_models()
if missing_models:
    print(f"WARNING: missing expected models by workflow: {missing_models}")
if found_models:
    print(f"Located expected models by workflow: {found_models}")

log_startup_diagnostics()
cleanup_old_comfy_outputs(force=True)
runpod.serverless.start({"handler": handler})

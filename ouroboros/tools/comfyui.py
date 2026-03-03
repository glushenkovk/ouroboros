"""
ComfyUI integration tools for Ouroboros.

Allows submitting workflows to a local ComfyUI instance and retrieving results.
Supports generic workflow execution and preset workflows (face swap, coloring pages).

ComfyUI instance: configurable via COMFYUI_URL env var (default: http://192.168.1.121:8188)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_COMFYUI_URL = "http://192.168.1.121:8188"
_POLL_INTERVAL = 2.0   # seconds between status checks
_MAX_WAIT = 300        # max wait time in seconds (5 min)


def _comfyui_url() -> str:
    return os.environ.get("COMFYUI_URL", _DEFAULT_COMFYUI_URL).rstrip("/")


def _http_post(url: str, data: Dict) -> Dict:
    """Simple HTTP POST with urllib (no extra deps)."""
    import urllib.request
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get(url: str) -> Dict:
    """Simple HTTP GET with urllib."""
    import urllib.request
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(url: str) -> bytes:
    """HTTP GET returning raw bytes (for image download)."""
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def _upload_image(image_path: str = "", image_base64: str = "") -> str:
    """
    Upload an image to ComfyUI /upload/image endpoint.
    Returns the filename as ComfyUI knows it.
    """
    import urllib.request

    if image_path:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        filename = Path(image_path).name
    elif image_base64:
        image_bytes = base64.b64decode(image_base64)
        filename = f"ouroboros_{uuid.uuid4().hex[:8]}.png"
    else:
        raise ValueError("Provide image_path or image_base64")

    # Multipart form upload
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + image_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    url = f"{_comfyui_url()}/upload/image"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["name"]


def _wait_for_result(prompt_id: str) -> Optional[Dict]:
    """Poll ComfyUI history until prompt_id is done. Returns history entry or None on timeout."""
    base = _comfyui_url()
    deadline = time.monotonic() + _MAX_WAIT

    while time.monotonic() < deadline:
        try:
            history = _http_get(f"{base}/history/{prompt_id}")
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    return entry
                if status.get("completed", False):
                    return entry
        except Exception as e:
            log.debug("Polling error: %s", e)
        time.sleep(_POLL_INTERVAL)

    return None


def _extract_images(history_entry: Dict) -> List[str]:
    """
    Extract output images from history entry.
    Returns list of base64-encoded PNG strings.
    """
    base = _comfyui_url()
    images_b64 = []

    outputs = history_entry.get("outputs", {})
    for node_id, node_output in outputs.items():
        for img_info in node_output.get("images", []):
            filename = img_info.get("filename", "")
            subfolder = img_info.get("subfolder", "")
            img_type = img_info.get("type", "output")
            url = f"{base}/view?filename={filename}&subfolder={subfolder}&type={img_type}"
            try:
                img_bytes = _http_get_bytes(url)
                images_b64.append(base64.b64encode(img_bytes).decode("utf-8"))
            except Exception as e:
                log.warning("Failed to download image %s: %s", filename, e)

    return images_b64


# ─── Tool handlers ────────────────────────────────────────────────────────────

def _comfyui_status(ctx: ToolContext) -> str:
    """Check ComfyUI queue and GPU status."""
    base = _comfyui_url()
    try:
        queue = _http_get(f"{base}/queue")
        sysinfo = _http_get(f"{base}/system_stats")

        running = len(queue.get("queue_running", []))
        pending = len(queue.get("queue_pending", []))
        devices = sysinfo.get("devices", [])
        gpu = devices[0] if devices else {}

        lines = [
            f"✅ ComfyUI online at {base}",
            f"Queue: {running} running, {pending} pending",
        ]
        if gpu:
            vram_total = gpu.get("vram_total", 0) // (1024 ** 2)
            vram_free = gpu.get("vram_free", 0) // (1024 ** 2)
            lines.append(
                f"GPU: {gpu.get('name', 'unknown')} — {vram_free}/{vram_total} MB VRAM free"
            )

        return "\n".join(lines)
    except Exception as e:
        return f"❌ ComfyUI not reachable at {base}: {e}"


def _comfyui_run_workflow(
    ctx: ToolContext,
    workflow: str,
    wait: bool = True,
) -> str:
    """
    Submit a workflow JSON to ComfyUI and optionally wait for result.

    Args:
        workflow: ComfyUI API-format workflow as JSON string
        wait: If True, block until generation completes and return images as base64.
              If False, return prompt_id immediately.
    """
    base = _comfyui_url()

    try:
        wf_dict = json.loads(workflow)
    except json.JSONDecodeError as e:
        return f"❌ Invalid workflow JSON: {e}"

    client_id = uuid.uuid4().hex

    try:
        result = _http_post(f"{base}/prompt", {
            "prompt": wf_dict,
            "client_id": client_id,
        })
    except Exception as e:
        return f"❌ Failed to submit workflow: {e}"

    prompt_id = result.get("prompt_id")
    if not prompt_id:
        return f"❌ ComfyUI did not return prompt_id. Response: {result}"

    if not wait:
        return json.dumps({"prompt_id": prompt_id, "status": "submitted"})

    entry = _wait_for_result(prompt_id)
    if entry is None:
        return f"⏳ Timeout after {_MAX_WAIT}s. prompt_id={prompt_id}"

    status = entry.get("status", {})
    if status.get("status_str") == "error":
        msgs = status.get("messages", [])
        return f"❌ ComfyUI error: {msgs}"

    images = _extract_images(entry)
    return json.dumps({
        "prompt_id": prompt_id,
        "images_count": len(images),
        "images": images,
    })


def _comfyui_face_swap(
    ctx: ToolContext,
    target_image_path: str = "",
    target_image_base64: str = "",
    source_face_path: str = "",
    source_face_base64: str = "",
    workflow_path: str = "",
) -> str:
    """
    Run a face swap workflow on ComfyUI.

    Uploads source and target images, injects them into the workflow,
    submits to ComfyUI, waits for result.

    Node injection convention:
    - Node with _meta.title == "TARGET_IMAGE" receives the target image filename
    - Node with _meta.title == "SOURCE_FACE" receives the source face filename
    - Override with env vars COMFYUI_TARGET_NODE_ID / COMFYUI_SOURCE_NODE_ID
    """
    # Find workflow file
    wf_path = (
        workflow_path
        or os.environ.get("COMFYUI_FACE_SWAP_WORKFLOW", "")
    )
    if not wf_path:
        default = Path.home() / "comfyui_workflows" / "face_swap.json"
        if default.exists():
            wf_path = str(default)

    if not wf_path:
        return (
            "❌ No face swap workflow provided.\n"
            "Options:\n"
            "  1. Pass workflow_path='/path/to/workflow.json'\n"
            "  2. Set env var COMFYUI_FACE_SWAP_WORKFLOW=/path/to/workflow.json\n"
            "  3. Place workflow at ~/comfyui_workflows/face_swap.json\n"
            "\nWorkflow must be in ComfyUI API format (Save/Export API in ComfyUI UI).\n"
            "Tag nodes: _meta.title = 'TARGET_IMAGE' and 'SOURCE_FACE'"
        )

    try:
        with open(wf_path) as f:
            wf_dict = json.load(f)
    except Exception as e:
        return f"❌ Failed to load workflow from {wf_path}: {e}"

    # Upload images
    try:
        target_name = _upload_image(target_image_path, target_image_base64)
        source_name = _upload_image(source_face_path, source_face_base64)
    except Exception as e:
        return f"❌ Image upload failed: {e}"

    # Inject filenames into workflow nodes
    target_node = os.environ.get("COMFYUI_TARGET_NODE_ID", "")
    source_node = os.environ.get("COMFYUI_SOURCE_NODE_ID", "")

    injected = 0
    for node_id, node in wf_dict.items():
        meta_title = node.get("_meta", {}).get("title", "").upper()
        inputs = node.get("inputs", {})

        if target_node and node_id == target_node:
            inputs["image"] = target_name
            injected += 1
        elif source_node and node_id == source_node:
            inputs["image"] = source_name
            injected += 1
        elif "TARGET_IMAGE" in meta_title and "image" in inputs:
            inputs["image"] = target_name
            injected += 1
        elif "SOURCE_FACE" in meta_title and "image" in inputs:
            inputs["image"] = source_name
            injected += 1

    status_msg = ""
    if injected < 2:
        status_msg = (
            f"⚠️ Only injected {injected}/2 images into workflow nodes. "
            "Check node _meta.title values or set COMFYUI_TARGET_NODE_ID/COMFYUI_SOURCE_NODE_ID.\n"
            f"Uploaded: target={target_name}, source={source_name}\n"
        )

    result = _comfyui_run_workflow(ctx, json.dumps(wf_dict), wait=True)
    return status_msg + result if status_msg else result


# ─── Tool registry ────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="comfyui_status",
            schema={
                "name": "comfyui_status",
                "description": (
                    "Check ComfyUI queue and GPU status. "
                    "Use before submitting workflows to verify ComfyUI is online."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_comfyui_status,
            timeout_sec=10,
        ),
        ToolEntry(
            name="comfyui_run_workflow",
            schema={
                "name": "comfyui_run_workflow",
                "description": (
                    "Submit any ComfyUI workflow and wait for result. "
                    "Workflow must be in ComfyUI API format (use 'Save (API)' in ComfyUI UI). "
                    "Returns generated images as base64 PNG list."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workflow": {
                            "type": "string",
                            "description": "ComfyUI API-format workflow as JSON string",
                        },
                        "wait": {
                            "type": "boolean",
                            "description": (
                                "If true (default), block until done and return images. "
                                "If false, return prompt_id immediately for async use."
                            ),
                        },
                    },
                    "required": ["workflow"],
                },
            },
            handler=_comfyui_run_workflow,
            timeout_sec=360,
        ),
        ToolEntry(
            name="comfyui_face_swap",
            schema={
                "name": "comfyui_face_swap",
                "description": (
                    "Swap a face in an image using a ComfyUI face-swap workflow. "
                    "Uploads source and target images, injects into workflow, waits for result. "
                    "Requires workflow JSON (API format) via workflow_path or env var COMFYUI_FACE_SWAP_WORKFLOW. "
                    "Node injection: tag LoadImage nodes with _meta.title = 'TARGET_IMAGE' and 'SOURCE_FACE'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_image_path": {
                            "type": "string",
                            "description": "Local path to target image (where the face will be placed)",
                        },
                        "target_image_base64": {
                            "type": "string",
                            "description": "OR base64-encoded target image (PNG)",
                        },
                        "source_face_path": {
                            "type": "string",
                            "description": "Local path to source face image",
                        },
                        "source_face_base64": {
                            "type": "string",
                            "description": "OR base64-encoded source face image (PNG)",
                        },
                        "workflow_path": {
                            "type": "string",
                            "description": (
                                "Path to ComfyUI face-swap workflow JSON (API format). "
                                "Optional if COMFYUI_FACE_SWAP_WORKFLOW env var is set "
                                "or ~/comfyui_workflows/face_swap.json exists."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            handler=_comfyui_face_swap,
            timeout_sec=360,
        ),
    ]

"""
Printables batch generation tools for Ouroboros.

Generates coloring pages, dot marker sheets, tracing pages, and color-by-number
templates using ComfyUI + Flux + coloring book LoRA.

Supports batch generation for creating printables products at scale.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.tools.comfyui import _http_post, _wait_for_result, _extract_images, _comfyui_url
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_PRINTABLES_DIR = "/home/max2/ouroboros_data/printables"

# Subject lists per theme — cycles through when generating batches
THEME_SUBJECTS: Dict[str, List[str]] = {
    "animals": [
        "cat", "dog", "elephant", "giraffe", "lion", "bear", "rabbit",
        "fox", "owl", "penguin", "dolphin", "turtle", "butterfly", "frog",
        "horse", "cow", "pig", "duck", "bee", "ladybug",
    ],
    "dinosaurs": [
        "T-Rex", "triceratops", "brachiosaurus", "stegosaurus", "velociraptor",
        "pterodactyl", "ankylosaurus", "parasaurolophus", "diplodocus", "spinosaurus",
    ],
    "ocean": [
        "whale", "octopus", "starfish", "seahorse", "crab", "jellyfish",
        "clownfish", "shark", "sea turtle", "lobster", "seal", "puffer fish",
    ],
    "alphabet": [f"letter {c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"],
    "numbers": [f"number {n}" for n in range(1, 21)],
    "food": [
        "apple", "banana", "pizza slice", "ice cream cone", "cupcake",
        "strawberry", "watermelon", "corn", "carrot", "donut", "cookie", "lemon",
    ],
    "vehicles": [
        "car", "truck", "airplane", "train", "rocket", "boat", "bicycle",
        "helicopter", "school bus", "fire truck", "ambulance", "tractor",
    ],
    "fantasy": [
        "unicorn", "dragon", "fairy", "mermaid", "castle", "wizard",
        "knight", "phoenix", "griffin", "pegasus", "elf", "magic wand",
    ],
    "seasonal_christmas": [
        "Christmas tree", "Santa Claus", "reindeer", "snowflake", "candy cane",
        "stocking", "elf", "gingerbread man", "ornament", "snowman",
        "wreath", "present", "sleigh", "bell",
    ],
    "seasonal_halloween": [
        "pumpkin", "ghost", "witch", "bat", "black cat", "spider web",
        "skeleton", "haunted house", "cauldron", "owl", "vampire", "moon",
    ],
    "space": [
        "rocket ship", "astronaut", "planet Saturn", "star", "moon", "UFO",
        "alien", "telescope", "comet", "space shuttle", "Mars rover", "black hole",
    ],
    "flowers": [
        "rose", "sunflower", "tulip", "daisy", "lotus", "lily",
        "orchid", "lavender", "cherry blossom", "daffodil", "poppy", "iris",
    ],
    "bugs": [
        "butterfly", "ladybug", "bee", "caterpillar", "grasshopper", "dragonfly",
        "ant", "firefly", "snail", "spider", "beetle", "praying mantis",
    ],
    "fruits": [
        "apple", "banana", "strawberry", "watermelon", "grape", "orange",
        "pineapple", "cherry", "mango", "lemon", "pear", "peach",
    ],
}


def _get_subjects(theme: str) -> List[str]:
    """Get subjects list for a theme. Falls back to generic for unknown themes."""
    if theme in THEME_SUBJECTS:
        return THEME_SUBJECTS[theme]
    # For unknown themes, generate subjects from theme name
    return [f"{theme} item {i}" for i in range(1, 21)]


def _build_prompt(subject: str, content_type: str) -> str:
    """Build a generation prompt for a given subject and content type."""
    if content_type == "coloring_page":
        return (
            f"c0l0ringb00k {subject}, bold and easy style, thick bold black outlines, "
            f"large areas for coloring, white background, simple clean design, "
            f"no shading, no color fills, black and white only"
        )
    elif content_type == "dot_marker":
        return (
            f"{subject}, minimalist bold outline only, large empty circles dot marker template, "
            f"bingo dauber activity sheet, toddler age 2-4, thick black borders, "
            f"white background, very simple shapes, no details inside circles"
        )
    elif content_type == "tracing":
        return (
            f"{subject}, dotted outline trace pattern, dotted lines only, "
            f"handwriting practice template, simple bold shape outline, "
            f"white background, dashed line border to trace"
        )
    elif content_type == "color_by_number":
        return (
            f"{subject}, outlined sections with numbers, color by number activity sheet, "
            f"bold black outlines separating color regions, numbered sections, "
            f"white background, clear region boundaries"
        )
    else:
        return f"c0l0ringb00k {subject}, coloring page, black and white outline, white background"


def _build_flux_lora_workflow(
    prompt: str,
    seed: int,
    filename_prefix: str,
    width: int = 768,
    height: int = 1024,
    steps: int = 20,
    lora_strength: float = 1.0,
) -> Dict:
    """
    Build a ComfyUI API-format workflow for Flux dev + coloring book LoRA.
    Uses the proper Flux pipeline: UNETLoader, DualCLIPLoader, BasicGuider, SamplerCustomAdvanced.
    """
    return {
        "1": {
            "class_type": "UNETLoader",
            "_meta": {"title": "Load Diffusion Model"},
            "inputs": {"unet_name": "flux1-dev.safetensors", "weight_dtype": "fp8_e4m3fn"},
        },
        "2": {
            "class_type": "DualCLIPLoader",
            "_meta": {"title": "DualCLIPLoader"},
            "inputs": {
                "clip_name1": "t5xxl_fp8_e4m3fn.safetensors",
                "clip_name2": "clip_l.safetensors",
                "type": "flux",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "_meta": {"title": "Load VAE"},
            "inputs": {"vae_name": "ae.safetensors"},
        },
        "4": {
            "class_type": "LoraLoader",
            "_meta": {"title": "Load LoRA"},
            "inputs": {
                "model": ["1", 0],
                "clip": ["2", 0],
                "lora_name": "c0l0ringb00k_Flux_v1.safetensors",
                "strength_model": lora_strength,
                "strength_clip": lora_strength,
            },
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "Positive Prompt"},
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "6": {
            "class_type": "FluxGuidance",
            "_meta": {"title": "FluxGuidance"},
            "inputs": {"conditioning": ["5", 0], "guidance": 3.5},
        },
        "7": {
            "class_type": "EmptySD3LatentImage",
            "_meta": {"title": "Empty Latent Image"},
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "8": {
            "class_type": "RandomNoise",
            "_meta": {"title": "Random Noise"},
            "inputs": {"noise_seed": seed},
        },
        "9": {
            "class_type": "BasicGuider",
            "_meta": {"title": "BasicGuider"},
            "inputs": {"model": ["4", 0], "conditioning": ["6", 0]},
        },
        "10": {
            "class_type": "KSamplerSelect",
            "_meta": {"title": "KSamplerSelect"},
            "inputs": {"sampler_name": "deis"},
        },
        "11": {
            "class_type": "BasicScheduler",
            "_meta": {"title": "BasicScheduler"},
            "inputs": {
                "model": ["4", 0],
                "scheduler": "simple",
                "steps": steps,
                "denoise": 1.0,
            },
        },
        "12": {
            "class_type": "SamplerCustomAdvanced",
            "_meta": {"title": "Sampler"},
            "inputs": {
                "noise": ["8", 0],
                "guider": ["9", 0],
                "sampler": ["10", 0],
                "sigmas": ["11", 0],
                "latent_image": ["7", 0],
            },
        },
        "13": {
            "class_type": "VAEDecode",
            "_meta": {"title": "VAE Decode"},
            "inputs": {"samples": ["12", 0], "vae": ["3", 0]},
        },
        "14": {
            "class_type": "SaveImage",
            "_meta": {"title": "Save Image"},
            "inputs": {"images": ["13", 0], "filename_prefix": filename_prefix},
        },
    }


def _generate_printables_batch(
    ctx: ToolContext,
    theme: str,
    count: int = 5,
    content_type: str = "coloring_page",
    output_dir: str = "",
    width: int = 768,
    height: int = 1024,
    steps: int = 20,
    lora_strength: float = 1.0,
) -> str:
    """
    Generate a batch of printables images using ComfyUI + Flux + coloring book LoRA.

    Args:
        theme: Subject theme (e.g. "animals", "dinosaurs", "ocean", "alphabet")
        count: Number of images to generate
        content_type: "coloring_page", "dot_marker", "tracing", or "color_by_number"
        output_dir: Directory to save images (default: /home/max2/ouroboros_data/printables/{theme}_{content_type})
        width: Image width in pixels (default 768)
        height: Image height in pixels (default 1024)
        steps: Sampling steps (default 20, lower = faster but lower quality)
        lora_strength: LoRA strength (default 1.0)
    """
    # Validate content_type
    valid_types = {"coloring_page", "dot_marker", "tracing", "color_by_number"}
    if content_type not in valid_types:
        return f"❌ Invalid content_type '{content_type}'. Valid: {', '.join(valid_types)}"

    # Resolve output directory
    if not output_dir:
        output_dir = f"{_PRINTABLES_DIR}/{theme}_{content_type}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    subjects = _get_subjects(theme)
    generated = []
    failed = []
    start_time = time.monotonic()

    log.info("Starting batch: theme=%s type=%s count=%d -> %s", theme, content_type, count, output_dir)

    for i in range(1, count + 1):
        subject = subjects[(i - 1) % len(subjects)]
        prompt = _build_prompt(subject, content_type)
        seed = random.randint(0, 2**32 - 1)
        base_name = f"{content_type}_{theme}_{i:03d}"
        filename_prefix = f"ouroboros_{base_name}"

        log.info("[%d/%d] Generating: %s (seed=%d)", i, count, subject, seed)

        try:
            workflow = _build_flux_lora_workflow(
                prompt=prompt,
                seed=seed,
                filename_prefix=filename_prefix,
                width=width,
                height=height,
                steps=steps,
                lora_strength=lora_strength,
            )

            # Submit workflow
            import uuid
            client_id = uuid.uuid4().hex
            result = _http_post(f"{_comfyui_url()}/prompt", {
                "prompt": workflow,
                "client_id": client_id,
            })

            prompt_id = result.get("prompt_id")
            if not prompt_id:
                raise ValueError(f"No prompt_id returned: {result}")

            # Wait for result
            entry = _wait_for_result(prompt_id)
            if entry is None:
                raise TimeoutError(f"Timeout waiting for {prompt_id}")

            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {status.get('messages', [])}")

            # Extract and save images
            images_b64 = _extract_images(entry)
            if not images_b64:
                raise ValueError("No images returned from ComfyUI")

            # Save PNG
            png_path = f"{output_dir}/{base_name}.png"
            with open(png_path, "wb") as f:
                f.write(base64.b64decode(images_b64[0]))

            # Save metadata JSON
            meta = {
                "index": i,
                "theme": theme,
                "subject": subject,
                "content_type": content_type,
                "prompt": prompt,
                "seed": seed,
                "width": width,
                "height": height,
                "steps": steps,
                "lora_strength": lora_strength,
                "prompt_id": prompt_id,
                "file": base_name + ".png",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            meta_path = f"{output_dir}/{base_name}.json"
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)

            file_size = Path(png_path).stat().st_size
            generated.append({"file": base_name + ".png", "subject": subject, "size_bytes": file_size})
            log.info("[%d/%d] ✅ Saved: %s (%d KB)", i, count, png_path, file_size // 1024)

        except Exception as e:
            log.warning("[%d/%d] ❌ Failed: %s — %s", i, count, subject, e)
            failed.append({"index": i, "subject": subject, "error": str(e)})

    elapsed = time.monotonic() - start_time

    # Write manifest
    manifest = {
        "theme": theme,
        "content_type": content_type,
        "count_requested": count,
        "count_generated": len(generated),
        "count_failed": len(failed),
        "output_dir": output_dir,
        "elapsed_seconds": round(elapsed, 1),
        "files": generated,
        "errors": failed,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    manifest_path = f"{output_dir}/manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    # Return summary
    lines = [
        f"{'✅' if not failed else '⚠️'} Batch complete: {len(generated)}/{count} generated in {elapsed:.0f}s",
        f"📁 Output: {output_dir}",
        f"🎨 Theme: {theme} | Type: {content_type}",
    ]
    if generated:
        lines.append(f"📄 Files: {', '.join(f['file'] for f in generated[:5])}" +
                     (f" ... +{len(generated)-5} more" if len(generated) > 5 else ""))
    if failed:
        lines.append(f"❌ Failed: {', '.join(f['subject'] for f in failed)}")
    lines.append(f"📋 Manifest: {manifest_path}")

    return "\n".join(lines)


def _assemble_pdf(
    ctx: ToolContext,
    input_dir: str,
    output_path: str = "",
    page_size: str = "A4",
    margin_mm: int = 10,
    title: str = "",
) -> str:
    """
    Assemble all PNG images in input_dir into a PDF.

    Args:
        input_dir: Directory containing PNG files to include
        output_path: Output PDF path (default: input_dir/printables_pack.pdf)
        page_size: "A4" (210x297mm) or "letter" (216x279mm)
        margin_mm: Page margin in millimeters
        title: Optional title page text (skipped if empty)
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        from PIL import Image
    except ImportError as e:
        return f"❌ Missing dependency: {e}. Install: pip install reportlab pillow"

    input_dir = str(input_dir)
    if not Path(input_dir).exists():
        return f"❌ Input directory not found: {input_dir}"

    # Find all PNGs
    png_files = sorted(Path(input_dir).glob("*.png"))
    if not png_files:
        return f"❌ No PNG files found in {input_dir}"

    # Resolve output path
    if not output_path:
        output_path = f"{input_dir}/printables_pack.pdf"

    # Page dimensions
    if page_size.upper() == "LETTER":
        page_w_mm, page_h_mm = 216, 279
    else:  # A4
        page_w_mm, page_h_mm = 210, 297

    page_w = page_w_mm * mm
    page_h = page_h_mm * mm
    margin = margin_mm * mm

    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    page_count = 0

    # Optional title page
    if title:
        c.setFont("Helvetica-Bold", 24)
        c.drawCentredString(page_w / 2, page_h / 2 + 20, title)
        c.setFont("Helvetica", 14)
        c.drawCentredString(page_w / 2, page_h / 2 - 20, f"{len(png_files)} pages")
        c.showPage()
        page_count += 1

    for png_path in png_files:
        try:
            with Image.open(png_path) as img:
                img_w, img_h = img.size

            # Scale to fit usable area, maintaining aspect ratio
            scale = min(usable_w / img_w, usable_h / img_h)
            draw_w = img_w * scale
            draw_h = img_h * scale

            # Center on page
            x = margin + (usable_w - draw_w) / 2
            y = margin + (usable_h - draw_h) / 2

            c.drawImage(str(png_path), x, y, width=draw_w, height=draw_h)
            c.showPage()
            page_count += 1
        except Exception as e:
            log.warning("Skipping %s: %s", png_path.name, e)

    c.save()

    pdf_size = Path(output_path).stat().st_size
    return (
        f"✅ PDF assembled: {output_path}\n"
        f"📄 Pages: {page_count} ({len(png_files)} images" + 
        (f" + 1 title page" if title else "") + ")\n"
        f"💾 Size: {pdf_size // 1024} KB\n"
        f"📐 Page size: {page_size.upper()} ({page_w_mm}x{page_h_mm}mm), margin={margin_mm}mm"
    )


def _list_printables(ctx: ToolContext) -> str:
    """List all printables batches in the data directory."""
    base = Path(_PRINTABLES_DIR)
    if not base.exists():
        return f"📂 No printables yet. Directory: {_PRINTABLES_DIR}"

    batches = []
    for batch_dir in sorted(base.iterdir()):
        if not batch_dir.is_dir():
            continue
        manifest_path = batch_dir / "manifest.json"
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    m = json.load(f)
                batches.append({
                    "dir": batch_dir.name,
                    "theme": m.get("theme", "?"),
                    "content_type": m.get("content_type", "?"),
                    "count": m.get("count_generated", 0),
                    "created_at": m.get("created_at", "?"),
                })
            except Exception:
                # No manifest, count PNGs
                pngs = list(batch_dir.glob("*.png"))
                batches.append({
                    "dir": batch_dir.name,
                    "theme": batch_dir.name,
                    "content_type": "?",
                    "count": len(pngs),
                    "created_at": "?",
                })

    if not batches:
        return f"📂 No batches found in {_PRINTABLES_DIR}"

    lines = [f"📂 Printables in {_PRINTABLES_DIR}:\n"]
    for b in batches:
        lines.append(f"  🎨 {b['dir']}: {b['count']} images ({b['created_at'][:10] if b['created_at'] != '?' else '?'})")

    total = sum(b["count"] for b in batches)
    lines.append(f"\nTotal: {len(batches)} batches, {total} images")
    return "\n".join(lines)


# ─── Tool registry ────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="generate_printables_batch",
            schema={
                "name": "generate_printables_batch",
                "description": (
                    "Generate a batch of printables images (coloring pages, dot marker sheets, "
                    "tracing pages, color-by-number) using ComfyUI + Flux + coloring book LoRA. "
                    "Saves PNG files + metadata to disk. Use for building printables product inventory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "theme": {
                            "type": "string",
                            "description": (
                                "Subject theme. Known themes: animals, dinosaurs, ocean, alphabet, "
                                "numbers, food, vehicles, fantasy, seasonal_christmas, seasonal_halloween, "
                                "space, flowers, bugs, fruits. Custom themes also work."
                            ),
                        },
                        "count": {
                            "type": "integer",
                            "description": "Number of images to generate (default 5). Each takes ~30-60s.",
                        },
                        "content_type": {
                            "type": "string",
                            "enum": ["coloring_page", "dot_marker", "tracing", "color_by_number"],
                            "description": "Type of printable template (default: coloring_page)",
                        },
                        "output_dir": {
                            "type": "string",
                            "description": "Output directory path (default: auto-named by theme+type)",
                        },
                        "width": {
                            "type": "integer",
                            "description": "Image width in pixels (default 768)",
                        },
                        "height": {
                            "type": "integer",
                            "description": "Image height in pixels (default 1024)",
                        },
                        "steps": {
                            "type": "integer",
                            "description": "Sampling steps, 10-30 (default 20). Lower = faster.",
                        },
                        "lora_strength": {
                            "type": "number",
                            "description": "LoRA strength 0.5-1.5 (default 1.0)",
                        },
                    },
                    "required": ["theme"],
                },
            },
            handler=_generate_printables_batch,
            timeout_sec=3600,
        ),
        ToolEntry(
            name="assemble_pdf",
            schema={
                "name": "assemble_pdf",
                "description": (
                    "Assemble all PNG images in a directory into a printable PDF. "
                    "Each image gets its own A4/letter page, centered with margins. "
                    "Use after generate_printables_batch to create the final product file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input_dir": {
                            "type": "string",
                            "description": "Directory containing PNG files",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Output PDF path (default: input_dir/printables_pack.pdf)",
                        },
                        "page_size": {
                            "type": "string",
                            "enum": ["A4", "letter"],
                            "description": "Page size (default A4)",
                        },
                        "margin_mm": {
                            "type": "integer",
                            "description": "Page margin in mm (default 10)",
                        },
                        "title": {
                            "type": "string",
                            "description": "Optional title page text (e.g. 'Animals Coloring Book')",
                        },
                    },
                    "required": ["input_dir"],
                },
            },
            handler=_assemble_pdf,
            timeout_sec=60,
        ),
        ToolEntry(
            name="list_printables",
            schema={
                "name": "list_printables",
                "description": "List all generated printables batches with counts and metadata.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_list_printables,
            timeout_sec=5,
        ),
    ]

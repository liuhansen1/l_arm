"""从项目原有 PyBullet 场景生成前端预览画面及像素拾取图。"""

import json
from pathlib import Path
import struct
import sys
import zlib

import numpy as np
import pybullet as p

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robot.demo import create_scene
from robot.grasp import PandaController


def write_png(path, pixels):
    height, width, channels = pixels.shape
    color_type = {3: 2, 4: 6}[channels]

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    rows = b"".join(b"\0" + row.tobytes() for row in pixels)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def main():
    output = ROOT / "frontend" / "assets"
    output.mkdir(parents=True, exist_ok=True)
    client = p.connect(p.DIRECT)
    try:
        robot, home, bodies = create_scene()
        controller = PandaController(client, robot, home, real_time=False)
        controller.wait(1)
        width, height = 1280, 800
        view = p.computeViewMatrixFromYawPitchRoll(
            [0.3, 0, 0.3], 1.5, 45, -32, 0, 2)
        projection = p.computeProjectionMatrixFOV(48, width / height, 0.02, 4)
        _, _, rgba, _, segmentation = p.getCameraImage(
            width, height, viewMatrix=view, projectionMatrix=projection,
            renderer=p.ER_TINY_RENDERER, lightDirection=[-3, -4, 8],
            shadow=1, flags=p.ER_SEGMENTATION_MASK_OBJECT_AND_LINKINDEX)
        rgba = np.asarray(rgba, dtype=np.uint8).reshape(height, width, 4)
        segmentation = np.asarray(segmentation).reshape(height, width)
        picking = np.zeros((height, width, 3), dtype=np.uint8)
        objects = []
        names = {"red": "红色方块", "green": "绿色方块", "blue": "蓝色方块"}
        for code, (color, body) in enumerate(bodies.items(), 1):
            mask = (segmentation >= 0) & ((segmentation & 0xFFFFFF) == body)
            ys, xs = np.where(mask)
            if not len(xs):
                raise RuntimeError(f"预览相机看不到{names[color]}")
            picking[mask, 0] = code
            objects.append({
                "scene_object_id": f"cube-{color}", "color": color,
                "pick_code": code,
                "position": [round(v, 6) for v in p.getBasePositionAndOrientation(body)[0]],
                "screen_position": [float(xs.mean() / width), float(ys.mean() / height)],
            })
        write_png(output / "scene.png", rgba)
        write_png(output / "picking.png", picking)
        metadata = {
            "frame_id": "panda-preview-1", "width": width, "height": height,
            "image_url": "./assets/scene.png", "picking_url": "./assets/picking.png",
            "coordinate_frame": "world", "unit": "m", "objects": objects,
            "camera": {"view_matrix": view, "projection_matrix": projection},
        }
        (output / "scene.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已生成 {output / 'scene.png'} 与三色方块拾取数据。")
    finally:
        p.disconnect(client)


if __name__ == "__main__":
    main()
